from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .jsonl import read_jsonl
from .paths import catalog_path, clean_dir
from .tokenize import (
    canonicalize,
    extract_anchors,
    fts_query_from_tokens,
    identifier_aliases,
    phrase_queries,
    tokenizer_fingerprint,
    tokenize_for_fts,
    tokens_for_fts,
)


SCHEMA_VERSION = 1


def connect() -> sqlite3.Connection:
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_catalog(conn)
    return conn


def init_catalog(conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    conn = conn or sqlite3.connect(str(catalog_path()))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS database_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chunk (
              chunk_uid TEXT PRIMARY KEY,
              doc_id TEXT NOT NULL,
              chunk_index INTEGER,
              source TEXT,
              source_id TEXT,
              source_type TEXT,
              path TEXT,
              uri TEXT,
              title TEXT,
              section_path TEXT,
              language TEXT,
              chunk_hash TEXT,
              content_hash TEXT,
              text_hash TEXT,
              text TEXT NOT NULL,
              embedding_text TEXT,
              metadata_json TEXT NOT NULL,
              visible_from INTEGER NOT NULL DEFAULT 1,
              visible_until INTEGER,
              updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS fts_word USING fts5(
              chunk_uid UNINDEXED,
              path_tokens,
              title_tokens,
              heading_tokens,
              body_tokens
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
              chunk_uid UNINDEXED,
              basename_tokens,
              stem_tokens,
              path_tokens,
              title_tokens,
              heading_tokens
            );

            CREATE TABLE IF NOT EXISTS identifier_occurrence (
              identifier TEXT NOT NULL,
              identifier_casefold TEXT NOT NULL,
              alias TEXT NOT NULL,
              chunk_uid TEXT NOT NULL,
              field TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chunk_doc_id ON chunk(doc_id);
            CREATE INDEX IF NOT EXISTS idx_chunk_path ON chunk(path);
            CREATE INDEX IF NOT EXISTS idx_chunk_source ON chunk(source);
            CREATE INDEX IF NOT EXISTS idx_chunk_hash ON chunk(chunk_hash);
            CREATE INDEX IF NOT EXISTS idx_identifier_casefold ON identifier_occurrence(identifier_casefold);
            CREATE INDEX IF NOT EXISTS idx_identifier_alias ON identifier_occurrence(alias);
            CREATE INDEX IF NOT EXISTS idx_identifier_chunk ON identifier_occurrence(chunk_uid);
            """
        )
        _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        _set_meta(conn, "tokenizer", tokenizer_fingerprint())
        _set_meta(conn, "active_generation", "1")
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def reset_catalog() -> None:
    path = catalog_path()
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
            conn.executescript(
                """
                DROP TABLE IF EXISTS database_meta;
                DROP TABLE IF EXISTS chunk;
                DROP TABLE IF EXISTS identifier_occurrence;
                DROP TABLE IF EXISTS fts_word;
                DROP TABLE IF EXISTS file_fts;
                """
            )
            conn.commit()
        finally:
            conn.close()
    with connect():
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


def counts() -> dict[str, Any]:
    path = catalog_path()
    if not path.exists():
        return {"exists": False, "path": str(path), "chunks": 0, "fts_rows": 0, "identifiers": 0}
    with connect() as conn:
        return {
            "exists": True,
            "path": str(path),
            "chunks": _count(conn, "chunk"),
            "fts_rows": _count(conn, "fts_word"),
            "file_fts_rows": _count(conn, "file_fts"),
            "identifiers": _count(conn, "identifier_occurrence"),
            "schema_version": _get_meta(conn, "schema_version"),
            "tokenizer": _get_meta(conn, "tokenizer"),
            "active_generation": _get_meta(conn, "active_generation"),
        }


def bm25_search(question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
    if not catalog_path().exists():
        return []
    tokens = tokens_for_fts(question, max_tokens=32)
    queries = _query_variants(question, tokens)
    if not queries:
        return []
    rows: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        for query_text in queries:
            for row in _run_fts(conn, "fts_word", query_text, top_k * 2, source):
                item = _row_to_result(row, signal="lexical", score=row["score"])
                current = rows.get(item["id"])
                if current is None or float(item.get("score") or 0) < float(current.get("score") or 0):
                    rows[item["id"]] = item
    ranked = sorted(rows.values(), key=lambda item: float(item.get("score") or 0))
    return _ranked(ranked[:top_k])


def metadata_search(question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
    if not catalog_path().exists():
        return []
    tokens = tokens_for_fts(question, max_tokens=24)
    query_text = fts_query_from_tokens(tokens, operator="OR", max_terms=24)
    if not query_text:
        return []
    with connect() as conn:
        rows = [_row_to_result(row, signal="metadata", score=row["score"]) for row in _run_fts(conn, "file_fts", query_text, top_k, source)]
    return _ranked(rows[:top_k])


def exact_search(question: str, *, top_k: int, source: str = "any") -> list[dict[str, Any]]:
    if not catalog_path().exists():
        return []
    anchors = extract_anchors(question, limit=30)
    if not anchors:
        return []
    cases = [canonicalize(anchor) for anchor in anchors]
    aliases: list[str] = []
    for anchor in anchors:
        aliases.extend(identifier_aliases(anchor))
    aliases = [canonicalize(alias) for alias in aliases]
    with connect() as conn:
        clauses: list[str] = []
        params: list[Any] = []
        if cases:
            clauses.append(f"i.identifier_casefold IN ({','.join('?' for _ in cases)})")
            params.extend(cases)
        if aliases:
            clauses.append(f"i.alias IN ({','.join('?' for _ in aliases)})")
            params.extend(aliases)
        source_sql, source_params = _source_filter("c", source)
        sql = f"""
            SELECT
              c.*,
              COUNT(*) AS match_count,
              MIN(CASE i.field WHEN 'path' THEN 0 WHEN 'title' THEN 1 WHEN 'heading' THEN 2 ELSE 3 END) AS field_rank
            FROM identifier_occurrence i
            JOIN chunk c ON c.chunk_uid = i.chunk_uid
            WHERE ({' OR '.join(clauses)})
              AND c.visible_until IS NULL
              {source_sql}
            GROUP BY c.chunk_uid
            ORDER BY match_count DESC, field_rank ASC
            LIMIT ?
        """
        params.extend(source_params)
        params.append(top_k)
        rows = [_row_to_result(row, signal="exact", score=-float(row["match_count"])) for row in conn.execute(sql, params)]
    return _ranked(rows)


def get_neighbor_rows(chunk_uid: str, *, window: int = 1) -> list[dict[str, Any]]:
    if not catalog_path().exists():
        return []
    with connect() as conn:
        row = conn.execute("SELECT doc_id, chunk_index FROM chunk WHERE chunk_uid = ?", (chunk_uid,)).fetchone()
        if not row or row["chunk_index"] is None:
            return []
        rows = conn.execute(
            """
            SELECT *
            FROM chunk
            WHERE doc_id = ?
              AND chunk_index BETWEEN ? AND ?
              AND visible_until IS NULL
            ORDER BY chunk_index ASC
            """,
            (row["doc_id"], int(row["chunk_index"]) - window, int(row["chunk_index"]) + window),
        ).fetchall()
    return [_row_to_result(item, signal="neighbor", score=0) for item in rows]


def fetch_rows_by_ids(ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    chunk_ids = [str(value) for value in ids if value]
    if not chunk_ids or not catalog_path().exists():
        return {}
    with connect() as conn:
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = conn.execute(f"SELECT * FROM chunk WHERE chunk_uid IN ({placeholders})", chunk_ids).fetchall()
    return {str(row["chunk_uid"]): _row_to_result(row, signal="catalog", score=0) for row in rows}


def _insert_record(conn: sqlite3.Connection, record: dict[str, Any], now: str) -> None:
    meta = dict(record.get("metadata") or {})
    chunk_uid = str(record["id"])
    text = str(record.get("text") or "")
    path = str(meta.get("path") or "")
    title = str(meta.get("title") or path)
    heading = str(meta.get("section_path") or meta.get("chunk_title") or "")
    chunk_index = _int_or_none(meta.get("chunk_index"))
    conn.execute(
        """
        INSERT INTO chunk (
          chunk_uid, doc_id, chunk_index, source, source_id, source_type, path, uri, title, section_path,
          language, chunk_hash, content_hash, text_hash, text, embedding_text, metadata_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk_uid,
            str(record.get("doc_id") or meta.get("doc_id") or ""),
            chunk_index,
            str(meta.get("source") or ""),
            str(meta.get("source_id") or ""),
            str(meta.get("source_type") or ""),
            path,
            str(meta.get("uri") or ""),
            title,
            heading,
            str(meta.get("language") or ""),
            str(meta.get("chunk_hash") or ""),
            str(meta.get("content_hash") or ""),
            str(meta.get("text_hash") or ""),
            text,
            str(record.get("embedding_text") or ""),
            json.dumps(meta, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )
    conn.execute(
        "INSERT INTO fts_word(chunk_uid, path_tokens, title_tokens, heading_tokens, body_tokens) VALUES (?, ?, ?, ?, ?)",
        (
            chunk_uid,
            tokenize_for_fts(path, max_tokens=200),
            tokenize_for_fts(title, max_tokens=100),
            tokenize_for_fts(heading, max_tokens=100),
            tokenize_for_fts(text, max_tokens=2000),
        ),
    )
    basename = Path(path).name
    stem = Path(path).stem
    conn.execute(
        "INSERT INTO file_fts(chunk_uid, basename_tokens, stem_tokens, path_tokens, title_tokens, heading_tokens) VALUES (?, ?, ?, ?, ?, ?)",
        (
            chunk_uid,
            tokenize_for_fts(basename, max_tokens=80),
            tokenize_for_fts(stem, max_tokens=80),
            tokenize_for_fts(path, max_tokens=200),
            tokenize_for_fts(title, max_tokens=100),
            tokenize_for_fts(heading, max_tokens=100),
        ),
    )
    _insert_identifiers(conn, chunk_uid, "path", path, limit=120)
    _insert_identifiers(conn, chunk_uid, "title", title, limit=80)
    _insert_identifiers(conn, chunk_uid, "heading", heading, limit=80)
    _insert_identifiers(conn, chunk_uid, "body", text, limit=500)


def _insert_identifiers(conn: sqlite3.Connection, chunk_uid: str, field: str, text: str, *, limit: int) -> None:
    rows: list[tuple[str, str, str, str, str]] = []
    for identifier in extract_anchors(text, limit=limit):
        identifier_casefold = canonicalize(identifier)
        for alias in identifier_aliases(identifier):
            rows.append((identifier, identifier_casefold, canonicalize(alias), chunk_uid, field))
    if rows:
        conn.executemany(
            "INSERT INTO identifier_occurrence(identifier, identifier_casefold, alias, chunk_uid, field) VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def _delete_chunks(conn: sqlite3.Connection, ids: Iterable[str]) -> int:
    chunk_ids = [str(value) for value in ids if value]
    if not chunk_ids:
        return 0
    for chunk_id in chunk_ids:
        conn.execute("DELETE FROM identifier_occurrence WHERE chunk_uid = ?", (chunk_id,))
        conn.execute("DELETE FROM fts_word WHERE chunk_uid = ?", (chunk_id,))
        conn.execute("DELETE FROM file_fts WHERE chunk_uid = ?", (chunk_id,))
        conn.execute("DELETE FROM chunk WHERE chunk_uid = ?", (chunk_id,))
    return len(chunk_ids)


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
    output: list[str] = []
    seen: set[str] = set()
    for query_text in queries:
        if query_text in seen:
            continue
        seen.add(query_text)
        output.append(query_text)
    return output


def _run_fts(conn: sqlite3.Connection, table: str, query_text: str, top_k: int, source: str) -> list[sqlite3.Row]:
    if table not in {"fts_word", "file_fts"}:
        raise ValueError(table)
    source_sql, source_params = _source_filter("c", source)
    if table == "fts_word":
        score_expr = "bm25(fts_word, 1.6, 2.0, 2.0, 1.0)"
        sql = f"""
            SELECT c.*, {score_expr} AS score
            FROM fts_word
            JOIN chunk c ON c.chunk_uid = fts_word.chunk_uid
            WHERE fts_word MATCH ?
              AND c.visible_until IS NULL
              {source_sql}
            ORDER BY score ASC
            LIMIT ?
        """
    else:
        score_expr = "bm25(file_fts, 2.5, 2.0, 2.0, 1.8, 1.5)"
        sql = f"""
            SELECT c.*, {score_expr} AS score
            FROM file_fts
            JOIN chunk c ON c.chunk_uid = file_fts.chunk_uid
            WHERE file_fts MATCH ?
              AND c.visible_until IS NULL
              {source_sql}
            ORDER BY score ASC
            LIMIT ?
        """
    params = [query_text]
    params.extend(source_params)
    params.append(top_k)
    try:
        return list(conn.execute(sql, params))
    except sqlite3.OperationalError:
        return []


def _row_to_result(row: sqlite3.Row, *, signal: str, score: float) -> dict[str, Any]:
    meta = json.loads(str(row["metadata_json"] or "{}"))
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


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO database_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _get_meta(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM database_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
