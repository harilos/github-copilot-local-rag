from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .jsonl import read_jsonl
from .paths import catalog_path, clean_dir
from .tokenize import (
    canonicalize,
    extract_anchors,
    fts_query_from_tokens,
    identifier_aliases,
    identifier_match_keys,
    phrase_queries,
    tokenizer_fingerprint,
    tokenize_for_fts,
    tokens_for_fts,
)


SCHEMA_VERSION = 2

FIELD_HEADING = 1
FIELD_BODY = 2

_COMMON_WEAK_ACRONYMS = {
    "ac",
    "acs",
    "rac",
    "racs",
}

_RESULT_COLUMNS = """
  c.chunk_pk,
  c.chunk_uid,
  c.doc_id,
  c.chunk_index,
  c.section_path,
  c.language AS chunk_language,
  c.chunk_hash,
  c.content_hash AS chunk_content_hash,
  c.text_hash,
  c.text,
  c.location_json,
  c.metadata_json AS chunk_metadata_json,
  d.doc_pk,
  d.source,
  d.source_id,
  d.source_type,
  d.path,
  d.uri,
  d.title,
  d.language AS document_language,
  d.content_hash AS document_content_hash,
  d.metadata_json AS document_metadata_json
"""


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = path or catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        init_catalog(conn)
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def connect_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing catalog without creating WAL or schema writes."""
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def _reader(
    connection: sqlite3.Connection | None,
    path: Path,
) -> Any:
    if connection is not None:
        return nullcontext(connection)
    return connect_readonly(path)


def init_catalog(conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    conn = conn or sqlite3.connect(str(catalog_path()))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS database_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        schema = _get_meta(conn, "schema_version")
        if schema and schema != str(SCHEMA_VERSION):
            _drop_catalog_objects(conn)
        elif _table_exists(conn, "chunk") and not _column_exists(conn, "chunk", "chunk_pk"):
            _drop_catalog_objects(conn)

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS database_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS document (
              doc_pk INTEGER PRIMARY KEY,
              doc_id TEXT NOT NULL UNIQUE,
              source TEXT,
              source_id TEXT,
              source_type TEXT,
              path TEXT NOT NULL,
              uri TEXT,
              title TEXT,
              language TEXT,
              content_hash TEXT,
              metadata_json TEXT NOT NULL,
              visible_from INTEGER NOT NULL DEFAULT 1,
              visible_until INTEGER,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chunk (
              chunk_pk INTEGER PRIMARY KEY,
              chunk_uid TEXT NOT NULL UNIQUE,
              doc_pk INTEGER NOT NULL REFERENCES document(doc_pk) ON DELETE CASCADE,
              doc_id TEXT NOT NULL,
              chunk_index INTEGER,
              section_path TEXT,
              language TEXT,
              chunk_hash TEXT,
              content_hash TEXT,
              text_hash TEXT,
              text TEXT NOT NULL,
              location_json TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              visible_from INTEGER NOT NULL DEFAULT 1,
              visible_until INTEGER,
              updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS fts_word USING fts5(
              heading_tokens,
              body_tokens
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
              basename_tokens,
              stem_tokens,
              path_tokens,
              title_tokens
            );

            CREATE TABLE IF NOT EXISTS document_lookup (
              normalized_value TEXT NOT NULL,
              doc_pk INTEGER NOT NULL REFERENCES document(doc_pk) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              raw_value TEXT NOT NULL,
              PRIMARY KEY(normalized_value, doc_pk, kind)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS identifier_term (
              term_id INTEGER PRIMARY KEY,
              canonical_value TEXT NOT NULL UNIQUE,
              kind TEXT NOT NULL,
              document_frequency INTEGER NOT NULL DEFAULT 0,
              flags TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS identifier_alias (
              alias_value TEXT NOT NULL,
              term_id INTEGER NOT NULL REFERENCES identifier_term(term_id) ON DELETE CASCADE,
              match_kind TEXT NOT NULL,
              PRIMARY KEY(alias_value, term_id)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS identifier_posting (
              term_id INTEGER NOT NULL REFERENCES identifier_term(term_id) ON DELETE CASCADE,
              chunk_pk INTEGER NOT NULL REFERENCES chunk(chunk_pk) ON DELETE CASCADE,
              field INTEGER NOT NULL,
              count INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(term_id, chunk_pk, field)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS identifier_suppressed (
              canonical_value TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              suppressed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_document_source ON document(source);
            CREATE INDEX IF NOT EXISTS idx_document_path ON document(path);
            CREATE INDEX IF NOT EXISTS idx_document_hash ON document(content_hash);
            CREATE INDEX IF NOT EXISTS idx_chunk_doc_pk ON chunk(doc_pk);
            CREATE INDEX IF NOT EXISTS idx_chunk_doc_id ON chunk(doc_id);
            CREATE INDEX IF NOT EXISTS idx_chunk_hash ON chunk(chunk_hash);
            CREATE INDEX IF NOT EXISTS idx_posting_chunk ON identifier_posting(chunk_pk);
            """
        )
        _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        _set_meta(conn, "tokenizer", tokenizer_fingerprint())
        _set_meta(conn, "active_generation", "1")
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def reset_catalog(path: Path | None = None) -> None:
    path = path or catalog_path()
    unlink_failed = False
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            try:
                candidate.unlink()
            except PermissionError:
                unlink_failed = True
    if unlink_failed:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            _drop_catalog_objects(conn)
            conn.commit()
        finally:
            conn.close()
    with connect(path):
        pass


def upsert_records(records: list[dict[str, Any]]) -> int:
    if not records:
        return 0
    with connect() as conn:
        ids = [str(record["id"]) for record in records]
        _delete_chunks(conn, ids)
        now = datetime.now(timezone.utc).isoformat()
        for record in records:
            _insert_record(conn, record, now)
        _refresh_identifier_stats(conn)
        _set_meta(conn, "updated_at", now)
        return len(records)


def delete_chunks(ids: Iterable[str]) -> int:
    chunk_ids = [str(value) for value in ids if value]
    if not chunk_ids:
        return 0
    with connect() as conn:
        return _delete_chunks(conn, chunk_ids)


def rebuild_from_clean(reset: bool = True) -> int:
    records: list[dict[str, Any]] = []
    directory = clean_dir()
    if directory.exists():
        for path in sorted(directory.rglob("*.jsonl")):
            records.extend(read_jsonl(path))
    if not records:
        raise RuntimeError(f"No clean jsonl records found under {directory}")
    if reset:
        reset_catalog()
    return upsert_records(records)


def counts(path: Path | None = None) -> dict[str, Any]:
    path = path or catalog_path()
    if not path.exists():
        return {"exists": False, "path": str(path), "chunks": 0, "fts_rows": 0, "identifiers": 0}
    with connect(path) as conn:
        return {
            "exists": True,
            "path": str(path),
            "documents": _count(conn, "document"),
            "chunks": _count(conn, "chunk"),
            "fts_rows": _count(conn, "fts_word"),
            "file_fts_rows": _count(conn, "file_fts"),
            "document_lookup_rows": _count(conn, "document_lookup"),
            "identifier_terms": _count(conn, "identifier_term"),
            "identifier_aliases": _count(conn, "identifier_alias"),
            "identifier_postings": _count(conn, "identifier_posting"),
            "identifiers": _count(conn, "identifier_posting"),
            "schema_version": _get_meta(conn, "schema_version"),
            "tokenizer": _get_meta(conn, "tokenizer"),
            "active_generation": _get_meta(conn, "active_generation"),
        }


def bm25_search(
    question: str,
    *,
    top_k: int,
    source: str = "any",
    path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    path = path or catalog_path()
    if not path.exists():
        return []
    tokens = tokens_for_fts(question, max_tokens=32)
    queries = _query_variants(question, tokens)
    if not queries:
        return []
    rows: dict[str, dict[str, Any]] = {}
    with _reader(conn, path) as reader:
        for query_text in queries:
            for row in _run_fts(reader, "fts_word", query_text, top_k * 2, source):
                item = _row_to_result(row, signal="lexical", score=row["score"])
                current = rows.get(item["id"])
                if current is None or float(item.get("score") or 0) < float(current.get("score") or 0):
                    rows[item["id"]] = item
    ranked = sorted(rows.values(), key=lambda item: float(item.get("score") or 0))
    return _ranked(ranked[:top_k])


def anchor_lexical_search(
    question: str,
    *,
    top_k: int = 1,
    source: str = "any",
    path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return a few low-document-frequency lexical candidates for one Hybrid call."""
    path = path or catalog_path()
    if not path.exists() or top_k <= 0:
        return []
    try:
        with _reader(conn, path) as reader:
            anchors = _informative_anchor_tokens(reader, question, limit=1)
            if not anchors:
                return []
            token, document_df, information_score = anchors[0]
            query_text = fts_query_from_tokens([token], operator="OR", max_terms=1)
            if not query_text:
                return []
            candidates = _run_fts(reader, "fts_word", query_text, max(8, top_k * 4), source)
            rows: list[dict[str, Any]] = []
            for raw in candidates:
                item = _row_to_result(raw, signal="lexical_anchor", score=raw["score"])
                debug = dict(item.get("debug") or {})
                debug["lexical_anchor"] = {
                    "token": token,
                    "document_df": document_df,
                    "information_score": round(information_score, 6),
                }
                item["debug"] = debug
                rows.append(item)
            rows.sort(
                key=lambda item: (
                    _token_position(str(item.get("text") or ""), token),
                    float(item.get("score") or 0),
                )
            )
            return _ranked(rows[:top_k])
    except (sqlite3.Error, ValueError):
        return []


def metadata_search(
    question: str,
    *,
    top_k: int,
    source: str = "any",
    path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    path = path or catalog_path()
    if not path.exists():
        return []
    tokens = tokens_for_fts(question, max_tokens=24)
    query_text = fts_query_from_tokens(tokens, operator="OR", max_terms=24)
    if not query_text:
        return []
    source_sql, source_params = _source_filter("d", source)
    sql = f"""
        SELECT {_RESULT_COLUMNS}, docs.score AS score
        FROM (
          SELECT d.doc_pk, bm25(file_fts, 2.8, 2.4, 2.0, 1.8) AS score
          FROM file_fts
          JOIN document d ON d.doc_pk = file_fts.rowid
          WHERE file_fts MATCH ?
            AND d.visible_until IS NULL
            {source_sql}
          ORDER BY score ASC
          LIMIT ?
        ) docs
        JOIN document d ON d.doc_pk = docs.doc_pk
        JOIN chunk c ON c.chunk_pk = (
          SELECT c2.chunk_pk
          FROM chunk c2
          WHERE c2.doc_pk = d.doc_pk
            AND c2.visible_until IS NULL
          ORDER BY c2.chunk_index ASC, c2.chunk_pk ASC
          LIMIT 1
        )
        ORDER BY docs.score ASC
    """
    params: list[Any] = [query_text, *source_params, top_k]
    with _reader(conn, path) as reader:
        try:
            rows = [_row_to_result(row, signal="metadata", score=row["score"]) for row in reader.execute(sql, params)]
        except sqlite3.OperationalError:
            rows = []
    return _ranked(rows[:top_k])


def exact_search(
    question: str,
    *,
    top_k: int,
    source: str = "any",
    path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    path = path or catalog_path()
    if not path.exists():
        return []
    anchors = extract_anchors(question, limit=30)
    if not anchors:
        return []
    lookup_values = _lookup_values_for_anchors(anchors)
    rows: dict[str, dict[str, Any]] = {}
    with _reader(conn, path) as reader:
        for row in _document_lookup_search(reader, lookup_values, top_k=top_k, source=source):
            item = _row_to_result(row, signal="exact", score=-0.1)
            _set_exact_debug(item, row, match_kind="document_lookup")
            rows[item["id"]] = item
        for row in _identifier_search(reader, lookup_values, top_k=top_k, source=source):
            item = _row_to_result(row, signal="exact", score=-float(row["match_count"]))
            _set_exact_debug(item, row, match_kind="casefold_exact")
            current = rows.get(item["id"])
            if current is None or float(item["score"]) < float(current.get("score") or 0):
                rows[item["id"]] = item
    ranked = sorted(rows.values(), key=lambda item: float(item.get("score") or 0))
    return _ranked(ranked[:top_k])


def get_neighbor_rows(
    chunk_uid: str,
    *,
    window: int = 1,
    path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    path = path or catalog_path()
    if not path.exists():
        return []
    with _reader(conn, path) as reader:
        row = reader.execute("SELECT doc_pk, chunk_index FROM chunk WHERE chunk_uid = ?", (chunk_uid,)).fetchone()
        if not row or row["chunk_index"] is None:
            return []
        rows = reader.execute(
            f"""
            SELECT {_RESULT_COLUMNS}, 0.0 AS score
            FROM chunk c
            JOIN document d ON d.doc_pk = c.doc_pk
            WHERE c.doc_pk = ?
              AND c.chunk_index BETWEEN ? AND ?
              AND c.visible_until IS NULL
              AND d.visible_until IS NULL
            ORDER BY c.chunk_index ASC
            """,
            (row["doc_pk"], int(row["chunk_index"]) - window, int(row["chunk_index"]) + window),
        ).fetchall()
    return [_row_to_result(item, signal="neighbor", score=0) for item in rows]


def fetch_rows_by_ids(
    ids: Iterable[str],
    *,
    path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, Any]]:
    chunk_ids = [str(value) for value in ids if value]
    path = path or catalog_path()
    if not chunk_ids or not path.exists():
        return {}
    with _reader(conn, path) as reader:
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = reader.execute(
            f"""
            SELECT {_RESULT_COLUMNS}, 0.0 AS score
            FROM chunk c
            JOIN document d ON d.doc_pk = c.doc_pk
            WHERE c.chunk_uid IN ({placeholders})
              AND c.visible_until IS NULL
              AND d.visible_until IS NULL
            """,
            chunk_ids,
        ).fetchall()
    return {str(row["chunk_uid"]): _row_to_result(row, signal="catalog", score=0) for row in rows}


def _insert_record(conn: sqlite3.Connection, record: dict[str, Any], now: str) -> None:
    meta = dict(record.get("metadata") or {})
    chunk_uid = str(record["id"])
    text = str(record.get("text") or "")
    heading = str(meta.get("section_path") or meta.get("chunk_title") or "")
    chunk_index = _int_or_none(meta.get("chunk_index"))
    doc_id = _record_doc_id(record, meta)
    doc_pk = _upsert_document(conn, record, meta, doc_id, now)
    chunk_meta = _chunk_metadata(meta)
    cur = conn.execute(
        """
        INSERT INTO chunk (
          chunk_uid, doc_pk, doc_id, chunk_index, section_path, language,
          chunk_hash, content_hash, text_hash, text, location_json, metadata_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk_uid,
            doc_pk,
            doc_id,
            chunk_index,
            heading,
            str(meta.get("language") or ""),
            str(meta.get("chunk_hash") or ""),
            str(meta.get("content_hash") or ""),
            str(meta.get("text_hash") or ""),
            text,
            json.dumps(_location_metadata(meta), ensure_ascii=False, sort_keys=True),
            json.dumps(chunk_meta, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )
    chunk_pk = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO fts_word(rowid, heading_tokens, body_tokens) VALUES (?, ?, ?)",
        (
            chunk_pk,
            tokenize_for_fts(heading, max_tokens=100),
            tokenize_for_fts(text, max_tokens=2000),
        ),
    )
    _insert_identifiers(conn, chunk_pk, FIELD_HEADING, heading, limit=80)
    _insert_identifiers(conn, chunk_pk, FIELD_BODY, text, limit=500)


def _upsert_document(conn: sqlite3.Connection, record: dict[str, Any], meta: dict[str, Any], doc_id: str, now: str) -> int:
    path = str(meta.get("path") or "")
    title = str(meta.get("title") or path)
    doc_meta = _document_metadata(meta)
    conn.execute(
        """
        INSERT INTO document (
          doc_id, source, source_id, source_type, path, uri, title, language,
          content_hash, metadata_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(doc_id) DO UPDATE SET
          source=excluded.source,
          source_id=excluded.source_id,
          source_type=excluded.source_type,
          path=excluded.path,
          uri=excluded.uri,
          title=excluded.title,
          language=excluded.language,
          content_hash=excluded.content_hash,
          metadata_json=excluded.metadata_json,
          visible_until=NULL,
          updated_at=excluded.updated_at
        """,
        (
            doc_id,
            str(meta.get("source") or ""),
            str(meta.get("source_id") or ""),
            str(meta.get("source_type") or ""),
            path,
            str(meta.get("uri") or ""),
            title,
            str(meta.get("language") or ""),
            str(meta.get("content_hash") or ""),
            json.dumps(doc_meta, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )
    row = conn.execute("SELECT doc_pk FROM document WHERE doc_id = ?", (doc_id,)).fetchone()
    doc_pk = int(row["doc_pk"])
    conn.execute("DELETE FROM document_lookup WHERE doc_pk = ?", (doc_pk,))
    conn.execute("DELETE FROM file_fts WHERE rowid = ?", (doc_pk,))
    _insert_document_lookup(conn, doc_pk, path, title)
    conn.execute(
        "INSERT INTO file_fts(rowid, basename_tokens, stem_tokens, path_tokens, title_tokens) VALUES (?, ?, ?, ?, ?)",
        (
            doc_pk,
            tokenize_for_fts(Path(path).name, max_tokens=80),
            tokenize_for_fts(Path(path).stem, max_tokens=80),
            tokenize_for_fts(path, max_tokens=200),
            tokenize_for_fts(title, max_tokens=100),
        ),
    )
    return doc_pk


def _record_doc_id(record: dict[str, Any], meta: dict[str, Any]) -> str:
    explicit = record.get("doc_id") or meta.get("doc_id")
    if explicit:
        return str(explicit)
    source = str(meta.get("source_id") or meta.get("source") or "local")
    path = str(meta.get("path") or record.get("path") or "")
    content_hash = str(meta.get("content_hash") or record.get("content_hash") or "")
    seed = f"{source}:{path}:{content_hash or record.get('id') or ''}"
    return hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()


def _insert_document_lookup(conn: sqlite3.Connection, doc_pk: int, path: str, title: str) -> None:
    values: list[tuple[str, int, str, str]] = []
    for kind, raw in [
        ("path", path),
        ("basename", Path(path).name),
        ("stem", Path(path).stem),
        ("title", title),
    ]:
        raw = str(raw or "").strip()
        if not raw:
            continue
        values.append((canonicalize(raw), doc_pk, kind, raw))
    if values:
        conn.executemany(
            "INSERT OR IGNORE INTO document_lookup(normalized_value, doc_pk, kind, raw_value) VALUES (?, ?, ?, ?)",
            values,
        )


def _insert_identifiers(conn: sqlite3.Connection, chunk_pk: int, field: int, text: str, *, limit: int) -> None:
    postings: Counter[tuple[str, str, int]] = Counter()
    aliases_by_term: dict[str, set[str]] = {}
    kinds: dict[str, str] = {}
    for identifier in extract_anchors(text, limit=limit):
        kind = _identifier_kind(identifier)
        if not kind:
            continue
        canonical = canonicalize(identifier)
        if _is_suppressed_identifier(conn, canonical):
            continue
        postings[(canonical, kind, field)] += 1
        aliases_by_term.setdefault(canonical, set()).update(canonicalize(alias) for alias in identifier_aliases(identifier))
        aliases_by_term[canonical].add(canonical)
        kinds[canonical] = kind

    for canonical, aliases in aliases_by_term.items():
        kind = kinds[canonical]
        term_id = _ensure_identifier_term(conn, canonical, kind)
        conn.executemany(
            "INSERT OR IGNORE INTO identifier_alias(alias_value, term_id, match_kind) VALUES (?, ?, ?)",
            [(alias, term_id, "alias" if alias != canonical else "canonical") for alias in aliases if alias],
        )
        for (term_value, _kind, posting_field), count in postings.items():
            if term_value != canonical:
                continue
            conn.execute(
                """
                INSERT INTO identifier_posting(term_id, chunk_pk, field, count) VALUES (?, ?, ?, ?)
                ON CONFLICT(term_id, chunk_pk, field) DO UPDATE SET count = identifier_posting.count + excluded.count
                """,
                (term_id, chunk_pk, posting_field, count),
            )


def _ensure_identifier_term(conn: sqlite3.Connection, canonical: str, kind: str) -> int:
    if _is_suppressed_identifier(conn, canonical):
        raise ValueError(f"suppressed identifier: {canonical}")
    conn.execute(
        "INSERT OR IGNORE INTO identifier_term(canonical_value, kind) VALUES (?, ?)",
        (canonical, kind),
    )
    row = conn.execute("SELECT term_id FROM identifier_term WHERE canonical_value = ?", (canonical,)).fetchone()
    return int(row["term_id"])


def _delete_chunks(conn: sqlite3.Connection, ids: Iterable[str]) -> int:
    chunk_ids = [str(value) for value in ids if value]
    if not chunk_ids:
        return 0
    affected_doc_pks: set[int] = set()
    for chunk_id in chunk_ids:
        row = conn.execute("SELECT chunk_pk, doc_pk FROM chunk WHERE chunk_uid = ?", (chunk_id,)).fetchone()
        if not row:
            continue
        chunk_pk = int(row["chunk_pk"])
        affected_doc_pks.add(int(row["doc_pk"]))
        conn.execute("DELETE FROM identifier_posting WHERE chunk_pk = ?", (chunk_pk,))
        conn.execute("DELETE FROM fts_word WHERE rowid = ?", (chunk_pk,))
        conn.execute("DELETE FROM chunk WHERE chunk_pk = ?", (chunk_pk,))
    _delete_orphan_documents(conn, affected_doc_pks)
    _delete_orphan_identifier_terms(conn)
    return len(chunk_ids)


def _delete_orphan_documents(conn: sqlite3.Connection, doc_pks: Iterable[int]) -> None:
    for doc_pk in sorted(set(doc_pks)):
        row = conn.execute("SELECT 1 FROM chunk WHERE doc_pk = ? LIMIT 1", (doc_pk,)).fetchone()
        if row:
            continue
        conn.execute("DELETE FROM document_lookup WHERE doc_pk = ?", (doc_pk,))
        conn.execute("DELETE FROM file_fts WHERE rowid = ?", (doc_pk,))
        conn.execute("DELETE FROM document WHERE doc_pk = ?", (doc_pk,))


def _delete_orphan_identifier_terms(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM identifier_term
        WHERE term_id NOT IN (SELECT DISTINCT term_id FROM identifier_posting)
        """
    )


def _refresh_identifier_stats(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE identifier_term
        SET document_frequency = COALESCE((
          SELECT COUNT(DISTINCT c.doc_pk)
          FROM identifier_posting p
          JOIN chunk c ON c.chunk_pk = p.chunk_pk
          WHERE p.term_id = identifier_term.term_id
        ), 0)
        """
    )
    chunk_count = _count(conn, "chunk")
    threshold = max(8, int(chunk_count * 0.02))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO identifier_suppressed(canonical_value, kind, suppressed_at)
        SELECT canonical_value, kind, ?
        FROM identifier_term
        WHERE kind = 'weak_acronym'
          AND document_frequency > ?
        """,
        (now, threshold),
    )
    conn.execute(
        """
        DELETE FROM identifier_term
        WHERE kind = 'weak_acronym'
          AND document_frequency > ?
        """,
        (threshold,),
    )
    _delete_orphan_identifier_terms(conn)


def _document_lookup_search(conn: sqlite3.Connection, values: list[str], *, top_k: int, source: str) -> list[sqlite3.Row]:
    if not values:
        return []
    source_sql, source_params = _source_filter("d", source)
    placeholders = ",".join("?" for _ in values)
    sql = f"""
        SELECT {_RESULT_COLUMNS},
          COUNT(*) AS match_count,
          MIN(CASE dl.kind WHEN 'path' THEN 0 WHEN 'basename' THEN 1 WHEN 'stem' THEN 2 ELSE 3 END) AS field_rank,
          -0.1 AS score
        FROM document_lookup dl
        JOIN document d ON d.doc_pk = dl.doc_pk
        JOIN chunk c ON c.chunk_pk = (
          SELECT c2.chunk_pk
          FROM chunk c2
          WHERE c2.doc_pk = d.doc_pk
            AND c2.visible_until IS NULL
          ORDER BY c2.chunk_index ASC, c2.chunk_pk ASC
          LIMIT 1
        )
        WHERE dl.normalized_value IN ({placeholders})
          AND d.visible_until IS NULL
          {source_sql}
        GROUP BY c.chunk_pk
        ORDER BY match_count DESC, field_rank ASC
        LIMIT ?
    """
    params: list[Any] = [*values, *source_params, top_k]
    return list(conn.execute(sql, params))


def _identifier_search(conn: sqlite3.Connection, values: list[str], *, top_k: int, source: str) -> list[sqlite3.Row]:
    if not values:
        return []
    source_sql, source_params = _source_filter("d", source)
    placeholders = ",".join("?" for _ in values)
    sql = f"""
        SELECT {_RESULT_COLUMNS},
          SUM(p.count) AS match_count,
          MIN(p.field) AS field_rank,
          GROUP_CONCAT(DISTINCT t.canonical_value) AS matched_terms,
          -CAST(SUM(p.count) AS REAL) AS score
        FROM identifier_term t
        JOIN identifier_posting p ON p.term_id = t.term_id
        JOIN chunk c ON c.chunk_pk = p.chunk_pk
        JOIN document d ON d.doc_pk = c.doc_pk
        WHERE t.canonical_value IN ({placeholders})
          AND c.visible_until IS NULL
          AND d.visible_until IS NULL
          {source_sql}
        GROUP BY c.chunk_pk
        ORDER BY match_count DESC, field_rank ASC
        LIMIT ?
    """
    params: list[Any] = [*values, *source_params, top_k]
    return list(conn.execute(sql, params))


def _lookup_values_for_anchors(anchors: list[str]) -> list[str]:
    values: list[str] = []
    for anchor in anchors:
        values.extend(identifier_match_keys(anchor))
    return _unique(value for value in values if value)


def _set_exact_debug(item: dict[str, Any], row: sqlite3.Row, *, match_kind: str) -> None:
    matched_terms = ""
    if "matched_terms" in row.keys():
        matched_terms = str(row["matched_terms"] or "")
    debug = dict(item.get("debug") or {})
    debug["exact_match"] = {
        "match_kind": match_kind,
        "matched_terms": [value for value in matched_terms.split(",") if value],
    }
    item["debug"] = debug


def _query_variants(question: str, tokens: list[str]) -> list[str]:
    queries: list[str] = []
    and_tokens = [token for token in tokens if len(token) >= 3][:8]
    and_query = fts_query_from_tokens(and_tokens, operator="AND", max_terms=8)
    if and_query:
        queries.append(and_query)
    or_query = fts_query_from_tokens(tokens, operator="OR", max_terms=24)
    if or_query:
        queries.append(or_query)
    queries.extend(phrase_queries(question))
    return _unique(queries)


def _informative_anchor_tokens(
    conn: sqlite3.Connection,
    question: str,
    *,
    limit: int,
) -> list[tuple[str, int, float]]:
    tokens = _unique(canonicalize(token) for token in tokens_for_fts(question, max_tokens=32))
    tokens = [
        token
        for token in tokens
        if len(token) >= 2
        and not token.isdigit()
        and any(character.isalnum() for character in token)
    ]
    if not tokens or limit <= 0:
        return []
    query_only = bool(conn.execute("PRAGMA query_only").fetchone()[0])
    if query_only:
        # The catalog file remains protected by mode=ro. Temporarily allow a
        # connection-local TEMP fts5vocab view used only for DF calculation.
        conn.execute("PRAGMA query_only=OFF")
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_word_vocab "
            "USING fts5vocab(main, fts_word, 'row')"
        )
    finally:
        if query_only:
            conn.execute("PRAGMA query_only=ON")
    placeholders = ",".join("?" for _ in tokens)
    rows = conn.execute(
        f"SELECT term, doc FROM temp.fts_word_vocab WHERE term IN ({placeholders})",
        tokens,
    )
    document_count_row = conn.execute(
        """
        SELECT COUNT(DISTINCT d.doc_pk) AS count
        FROM document d
        JOIN chunk c ON c.doc_pk = d.doc_pk
        WHERE d.visible_until IS NULL
          AND c.visible_until IS NULL
        """
    ).fetchone()
    document_count = int(document_count_row["count"] if document_count_row else 0)
    if document_count <= 0:
        return []
    token_order = {token: index for index, token in enumerate(tokens)}
    ranked: list[tuple[str, int, float]] = []
    for row in rows:
        token = str(row["term"])
        query_text = fts_query_from_tokens([token], operator="OR", max_terms=1)
        if not query_text:
            continue
        document_df_row = conn.execute(
            """
            SELECT COUNT(DISTINCT c.doc_pk) AS count
            FROM fts_word
            JOIN chunk c ON c.chunk_pk = fts_word.rowid
            JOIN document d ON d.doc_pk = c.doc_pk
            WHERE fts_word MATCH ?
              AND c.visible_until IS NULL
              AND d.visible_until IS NULL
            """,
            (query_text,),
        ).fetchone()
        document_df = int(document_df_row["count"] if document_df_row else 0)
        rare_document_limit = max(2, int(document_count * 0.05))
        if document_df <= 0 or document_df > rare_document_limit:
            continue
        idf = math.log((document_count + 1) / (document_df + 1))
        length_bonus = 1.0 + min(len(token), 12) / 12.0
        ranked.append((token, document_df, idf * length_bonus))
    ranked.sort(
        key=lambda item: (
            -item[2],
            item[1],
            token_order.get(item[0], len(tokens)),
        )
    )
    return ranked[:limit]


def _token_position(text: str, token: str) -> int:
    position = canonicalize(text).find(canonicalize(token))
    return position if position >= 0 else 1_000_000_000


def _run_fts(conn: sqlite3.Connection, table: str, query_text: str, top_k: int, source: str) -> list[sqlite3.Row]:
    if table != "fts_word":
        raise ValueError(table)
    source_sql, source_params = _source_filter("d", source)
    score_expr = "bm25(fts_word, 2.0, 1.0)"
    sql = f"""
        SELECT {_RESULT_COLUMNS}, {score_expr} AS score
        FROM fts_word
        JOIN chunk c ON c.chunk_pk = fts_word.rowid
        JOIN document d ON d.doc_pk = c.doc_pk
        WHERE fts_word MATCH ?
          AND c.visible_until IS NULL
          AND d.visible_until IS NULL
          {source_sql}
        ORDER BY score ASC
        LIMIT ?
    """
    params: list[Any] = [query_text, *source_params, top_k]
    try:
        return list(conn.execute(sql, params))
    except sqlite3.OperationalError:
        return []


def _row_to_result(row: sqlite3.Row, *, signal: str, score: float) -> dict[str, Any]:
    document_meta = _load_json_object(row["document_metadata_json"])
    chunk_meta = _load_json_object(row["chunk_metadata_json"])
    meta = {
        **document_meta,
        **chunk_meta,
        "source": str(row["source"] or document_meta.get("source") or ""),
        "source_id": str(row["source_id"] or document_meta.get("source_id") or ""),
        "source_type": str(row["source_type"] or document_meta.get("source_type") or ""),
        "path": str(row["path"] or document_meta.get("path") or ""),
        "uri": str(row["uri"] or document_meta.get("uri") or ""),
        "title": str(row["title"] or document_meta.get("title") or ""),
        "language": str(row["chunk_language"] or row["document_language"] or document_meta.get("language") or ""),
        "section_path": str(row["section_path"] or chunk_meta.get("section_path") or ""),
        "chunk_index": row["chunk_index"],
        "chunk_hash": str(row["chunk_hash"] or chunk_meta.get("chunk_hash") or ""),
        "text_hash": str(row["text_hash"] or chunk_meta.get("text_hash") or ""),
        "content_hash": str(row["document_content_hash"] or row["chunk_content_hash"] or document_meta.get("content_hash") or ""),
    }
    return {
        "rank": 0,
        "id": str(row["chunk_uid"]),
        "distance": None,
        "score": score,
        "text": str(row["text"] or ""),
        "metadata": meta,
        "signals": [signal],
    }


def _ranked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _source_filter(alias: str, source: str) -> tuple[str, list[Any]]:
    if source == "any":
        return "", []
    return f"AND {alias}.source = ?", [source]


def _document_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    keys = ["source", "source_id", "source_type", "path", "uri", "title", "language", "root", "content_hash"]
    return {key: meta.get(key) for key in keys if _has_metadata_value(meta.get(key))}


def _chunk_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    document_keys = {"source", "source_id", "source_type", "path", "uri", "title", "language", "root", "content_hash"}
    keys = [
        "chunk_title",
        "section_path",
        "chunk_index",
        "chunk_hash",
        "text_hash",
        "chunker_version",
        "page",
        "slide",
        "lines",
    ]
    output = {key: meta.get(key) for key in keys if _has_metadata_value(meta.get(key))}
    for key, value in meta.items():
        if key in document_keys or key in output or not _has_metadata_value(value):
            continue
        output[key] = value
    return output


def _location_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    keys = ["section_path", "chunk_title", "chunk_index", "page", "slide", "lines"]
    return {key: meta.get(key) for key in keys if _has_metadata_value(meta.get(key))}


def _has_metadata_value(value: Any) -> bool:
    return value is not None and value != ""


def _identifier_kind(identifier: str) -> str:
    value = identifier.strip()
    canonical = canonicalize(value)
    if not value or canonical in _COMMON_WEAK_ACRONYMS:
        return ""
    if re.fullmatch(r"https?://[^\s)>\]}]+", value):
        return "url"
    if re.fullmatch(r"/[A-Za-z0-9_./:-]{2,}", value):
        return "path"
    if re.fullmatch(r"[A-Za-z0-9_.:/-]+\.(?:md|txt|log|pdf|docx?|pptx?|xlsx|json|ya?ml|toml|ini|py|js|jsx|ts|tsx|java|go|rs|cs|sql)", value, re.I):
        return "file"
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return "uuid"
    if re.fullmatch(r"[0-9a-fA-F]{12,}", value) and re.search(r"[a-fA-F]", value):
        return "hex"
    if re.search(r"[A-Za-z]", value) and re.search(r"\d", value):
        return "alpha_numeric"
    if re.search(r"[_:#@]", value):
        return "symbol"
    if re.search(r"[A-Za-z_][A-Za-z0-9_]*(?:[.:/-][A-Za-z0-9_]+)+", value):
        return "qualified"
    if re.fullmatch(r"[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+", value):
        return "constant"
    if re.fullmatch(r"[A-Z]?[a-z]+(?:[A-Z][A-Za-z0-9]+){1,}", value):
        return "camel"
    if re.fullmatch(r"[A-Z]{2,}s?", value) and 2 <= len(value) <= 12:
        return "weak_acronym"
    return ""


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO database_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _get_meta(conn: sqlite3.Connection, key: str) -> str:
    try:
        row = conn.execute("SELECT value FROM database_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row["value"]) if row else ""


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def _drop_catalog_objects(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS identifier_suppressed;
        DROP TABLE IF EXISTS identifier_posting;
        DROP TABLE IF EXISTS identifier_alias;
        DROP TABLE IF EXISTS identifier_term;
        DROP TABLE IF EXISTS identifier_occurrence;
        DROP TABLE IF EXISTS document_lookup;
        DROP TABLE IF EXISTS fts_word;
        DROP TABLE IF EXISTS file_fts;
        DROP TABLE IF EXISTS chunk;
        DROP TABLE IF EXISTS document;
        DROP TABLE IF EXISTS database_meta;
        """
    )


def _is_suppressed_identifier(conn: sqlite3.Connection, canonical: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM identifier_suppressed WHERE canonical_value = ?", (canonical,)).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return bool(row)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.OperationalError:
        return False


def _load_json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        value = str(value).strip()
        if not value:
            continue
        key = canonicalize(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output
