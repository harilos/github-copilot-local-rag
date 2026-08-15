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
from urllib.parse import quote

from .jsonl import read_jsonl
from .paths import catalog_path, clean_dir
from .tokenize import (
    canonicalize,
    extract_anchors,
    fts_query_from_tokens,
    identifier_aliases,
    identifier_match_keys,
    phrase_queries,
    supported_unicode_filename_anchor,
    tokenizer_fingerprint,
    tokenize_for_fts,
    tokens_for_fts,
    require_index_tokenizer,
    TokenizerFingerprintError,
)


SCHEMA_VERSION = 2
_WRITE_NORMALIZATION_META = "write_amplification_normalized_v1"

FIELD_HEADING = 1
FIELD_BODY = 2

_COMMON_WEAK_ACRONYMS = {
    "ac",
    "acs",
    "rac",
    "racs",
}


class CatalogSearchError(RuntimeError):
    """Bounded query-time catalog failure with its public lane identity."""

    lane = "catalog"

    def __init__(self) -> None:
        super().__init__(f"{self.lane}_catalog_search_failed")


class ExactSearchError(CatalogSearchError):
    lane = "exact"


class LexicalSearchError(CatalogSearchError):
    lane = "lexical"


class MetadataSearchError(CatalogSearchError):
    lane = "metadata"

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


def readonly_uri(path: Path) -> str:
    """Return an immutable read-only SQLite URI that cannot create sidecars."""
    resolved = path.resolve()
    resolved_text = str(resolved)
    query = "?mode=ro&immutable=1"
    if resolved_text.startswith("\\\\"):
        # sqlite rejects a file URI with a non-empty authority, including
        # ``file://server/share``.  Four leading slashes keep a Windows UNC
        # path in the URI path component and preserve read-only mode.
        unc_path = resolved_text.lstrip("\\").replace("\\", "/")
        return "file:////" + quote(unc_path, safe="/:") + query
    return resolved.as_uri() + query


@contextmanager
def connect_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an existing catalog without creating WAL or schema writes."""
    conn = sqlite3.connect(readonly_uri(path), uri=True)
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
        elif _table_exists(conn, "chunk"):
            stored_tokenizer = _get_meta(conn, "tokenizer")
            current_tokenizer = tokenizer_fingerprint()
            if stored_tokenizer != current_tokenizer:
                raise TokenizerFingerprintError(
                    "lexical_catalog_tokenizer_fingerprint_mismatch"
                )

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
            CREATE INDEX IF NOT EXISTS idx_document_source_id ON document(source_id);
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


def upsert_records(
    records: list[dict[str, Any]],
    *,
    delete_ids: Iterable[str] | None = None,
) -> int:
    replacement_ids = [str(value) for value in (delete_ids or ()) if value]
    if not records and not replacement_ids:
        return 0
    if records:
        require_index_tokenizer()
    with connect() as conn:
        if records and not _get_meta(conn, _WRITE_NORMALIZATION_META):
            _refresh_identifier_stats(conn)
            _set_meta(conn, _WRITE_NORMALIZATION_META, "1")
        incoming_ids = [str(record["id"]) for record in records]
        _stage_catalog_write(conn, [*replacement_ids, *incoming_ids])
        _delete_staged_chunks(conn)
        now = datetime.now(timezone.utc).isoformat()
        document_records: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for record in records:
            meta = dict(record.get("metadata") or {})
            document_records[_record_doc_id(record, meta)] = (record, meta)
        document_pks = {
            doc_id: _upsert_document(conn, record, meta, doc_id, now)
            for doc_id, (record, meta) in document_records.items()
        }
        for record in records:
            _insert_record_with_document_pks(conn, record, document_pks, now)
        _stage_inserted_identifier_terms(conn)
        _refresh_staged_identifier_stats(conn)
        _set_meta(conn, "updated_at", now)
        return len(records)


def delete_chunks(ids: Iterable[str]) -> int:
    chunk_ids = [str(value) for value in ids if value]
    if not chunk_ids:
        return 0
    with connect() as conn:
        return _delete_chunks(conn, chunk_ids)


def source_chunk_ids(source_id: str) -> list[str]:
    """Return every chunk ID owned by one exact Source."""
    value = str(source_id or "")
    if not value or not catalog_path().is_file():
        return []
    with connect_readonly(catalog_path()) as conn:
        rows = conn.execute(
            """
            SELECT c.chunk_uid
            FROM chunk c
            JOIN document d ON d.doc_pk = c.doc_pk
            WHERE d.source_id = ?
            ORDER BY c.chunk_uid
            """,
            (value,),
        ).fetchall()
    return [str(row["chunk_uid"]) for row in rows]


def ensure_source_delete_index() -> None:
    """Install the additive Source lookup index before the read-only plan."""
    if not catalog_path().is_file():
        return
    with connect():
        pass


def delete_source_documents(source_id: str) -> dict[str, int]:
    """Delete one exact Source with a fixed number of set-based SQL statements."""
    value = str(source_id or "")
    if not value:
        raise ValueError("source_id is required")
    if not catalog_path().is_file():
        return {"documents": 0, "chunks": 0}
    with connect() as conn:
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS source_delete_documents (
              doc_pk INTEGER PRIMARY KEY
            )
            """
        )
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS source_delete_chunks (
              chunk_pk INTEGER PRIMARY KEY
            )
            """
        )
        conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS source_delete_terms (
              term_id INTEGER PRIMARY KEY
            )
            """
        )
        conn.execute("DELETE FROM source_delete_documents")
        conn.execute("DELETE FROM source_delete_chunks")
        conn.execute("DELETE FROM source_delete_terms")
        conn.execute(
            """
            INSERT INTO source_delete_documents(doc_pk)
            SELECT doc_pk FROM document WHERE source_id = ?
            """,
            (value,),
        )
        document_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM source_delete_documents"
            ).fetchone()[0]
        )
        if not document_count:
            return {"documents": 0, "chunks": 0}
        conn.execute(
            """
            INSERT INTO source_delete_chunks(chunk_pk)
            SELECT c.chunk_pk
            FROM chunk c
            JOIN source_delete_documents d ON d.doc_pk = c.doc_pk
            """
        )
        chunk_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM source_delete_chunks"
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO source_delete_terms(term_id)
            SELECT DISTINCT p.term_id
            FROM identifier_posting p
            JOIN source_delete_chunks c ON c.chunk_pk = p.chunk_pk
            """
        )
        conn.execute(
            """
            DELETE FROM identifier_posting
            WHERE chunk_pk IN (SELECT chunk_pk FROM source_delete_chunks)
            """
        )
        conn.execute(
            """
            DELETE FROM fts_word
            WHERE rowid IN (SELECT chunk_pk FROM source_delete_chunks)
            """
        )
        conn.execute(
            """
            DELETE FROM chunk
            WHERE chunk_pk IN (SELECT chu÷¯t¶‰žËkºwµç]!8€Á…Ñ œQ!8€À]!8€‰…Í•¹…µ”œQ!8€Ä]!8€ÍÑ•´œQ!8€È1M€Ì9¤L™¥•±‘}É…¹¬°4(€€€€€€€€€€´À¸ÄLÍ½É”4(€€€€€€€I=4‘½Õµ•¹Ñ}±½½­ÕÀ‘°4(€€€€€€€)=%8‘½Õµ•¹Ð=8¹‘½}Á¬€ô‘°¹‘½}Á¬4(€€€€€€€)=%8¡Õ¹¬Œ=8Œ¹¡Õ¹­}Á¬€ô€ 4(€€€€€€€€€M1PŒÈ¹¡Õ¹­}Á¬4(€€€€€€€€€I=4¡Õ¹¬ŒÈ4(€€€€€€€€€]!IŒÈ¹‘½}Á¬€ô¹‘½}Á¬4(€€€€€€€€€€€9ŒÈ¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€=IH	dŒÈ¹¡Õ¹­}¥¹‘•àM°ŒÈ¹¡Õ¹­}Á¬M4(€€€€€€€€€1%5%P€Ä4(€€€€€€€€¤4(€€€€€€€]!I‘°¹¹½Éµ…±¥é•‘}Ù…±Õ”%8€¡íÁ±…•¡½±‘•ÉÍô¤4(€€€€€€€€€9¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€íÍ½ÕÉ•}ÍÅ±ô4(€€€€€€€I=U@	dŒ¹¡Õ¹­}Á¬4(€€€€€€€=IH	dµ…Ñ¡}½Õ¹ÐM°™¥•±‘}É…¹¬M4(€€€€€€€1%5%P€ü4(€€€€ˆˆˆ4(€€€Á…É…µÌè±¥ÍÑm¹åt€ôl©Ù…±Õ•Ì°€©Í½ÕÉ•}Á…É…µÌ°Ñ½Á}­t4(€€€É•ÑÕÉ¸±¥ÍÐ¡½¹¸¹•á•ÕÑ”¡ÍÅ°°Á…É…µÌ¤¤4(4(4)‘•˜}¥‘•¹Ñ¥™¥•É}Í•…É ¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°Ù…±Õ•Ìè±¥ÍÑmÍÑÉt°€¨°Ñ½Á}¬è¥¹Ð°Í½ÕÉ”èÍÑÈ¤€´ø±¥ÍÑmÍÅ±¥Ñ”Ì¹I½Ýtè4(€€€¥˜¹½ÐÙ…±Õ•Ìè4(€€€€€€€É•ÑÕÉ¸mt4(€€€Í½ÕÉ•}ÍÅ°°Í½ÕÉ•}Á…É…µÌ€ô}Í½ÕÉ•}™¥±Ñ•È ‰ˆ°Í½ÕÉ”¤4(€€€Á±…•¡½±‘•ÉÌ€ô€ˆ°ˆ¹©½¥¸ ˆüˆ™½È|¥¸Ù…±Õ•Ì¤4(€€€ÍÅ°€ô˜ˆˆˆ4(€€€€€€€M1Pí}IMU1Q}=1U59Mô°4(€€€€€€€€€MU4¡À¹½Õ¹Ð¤Lµ…Ñ¡}½Õ¹Ð°4(€€€€€€€€€5%8¡À¹™¥•±¤L™¥•±‘}É…¹¬°4(€€€€€€€€€I=UA}=9P¡%MQ%9PÐ¹…¹½¹¥…±}Ù…±Õ”¤Lµ…Ñ¡•‘}Ñ•ÉµÌ°4(€€€€€€€€€€µMP¡MU4¡À¹½Õ¹Ð¤LI0¤LÍ½É”4(€€€€€€€I=4¥‘•¹Ñ¥™¥•É}Ñ•É´Ð4(€€€€€€€)=%8¥‘•¹Ñ¥™¥•É}Á½ÍÑ¥¹œÀ=8À¹Ñ•Éµ}¥€ôÐ¹Ñ•Éµ}¥4(€€€€€€€)=%8¡Õ¹¬Œ=8Œ¹¡Õ¹­}Á¬€ôÀ¹¡Õ¹­}Á¬4(€€€€€€€)=%8‘½Õµ•¹Ð=8¹‘½}Á¬€ôŒ¹‘½}Á¬4(€€€€€€€]!IÐ¹…¹½¹¥…±}Ù…±Õ”%8€¡íÁ±…•¡½±‘•ÉÍô¤4(€€€€€€€€€9Œ¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€9¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€íÍ½ÕÉ•}ÍÅ±ô4(€€€€€€€I=U@	dŒ¹¡Õ¹­}Á¬4(€€€€€€€=IH	dµ…Ñ¡}½Õ¹ÐM°™¥•±‘}É…¹¬M4(€€€€€€€1%5%P€ü4(€€€€ˆˆˆ4(€€€Á…É…µÌè±¥ÍÑm¹åt€ôl©Ù…±Õ•Ì°€©Í½ÕÉ•}Á…É…µÌ°Ñ½Á}­t4(€€€É•ÑÕÉ¸±¥ÍÐ¡½¹¸¹•á•ÕÑ”¡ÍÅ°°Á…É…µÌ¤¤4(4(4)‘•˜}±½½­ÕÁ}Ù…±Õ•Í}™½É}…¹¡½ÉÌ¡…¹¡½ÉÌè±¥ÍÑmÍÑÉt¤€´ø±¥ÍÑmÍÑÉtè4(€€€Ù…±Õ•Ìè±¥ÍÑmÍÑÉt€ômt4(€€€™½È…¹¡½È¥¸…¹¡½ÉÌè4(€€€€€€€Ù…±Õ•Ì¹•áÑ•¹¡¥‘•¹Ñ¥™¥•É}µ…Ñ¡}­•åÌ¡…¹¡½È¤¤4(€€€É•ÑÕÉ¸}Õ¹¥ÅÕ”¡Ù…±Õ”™½ÈÙ…±Õ”¥¸Ù…±Õ•Ì¥˜Ù…±Õ”¤4(4(4)‘•˜}Í•Ñ}•á…Ñ}‘•‰Õœ¡¥Ñ•´è‘¥ÑmÍÑÈ°¹åt°É½ÜèÍÅ±¥Ñ”Ì¹I½Ü°€¨°µ…Ñ¡}­¥¹èÍÑÈ¤€´ø9½¹”è4(€€€µ…Ñ¡•‘}Ñ•ÉµÌ€ô€ˆˆ4(€€€¥˜€‰µ…Ñ¡•‘}Ñ•ÉµÌˆ¥¸É½Ü¹­•åÌ ¤è4(€€€€€€€µ…Ñ¡•‘}Ñ•ÉµÌ€ôÍÑÈ¡É½Ýl‰µ…Ñ¡•‘}Ñ•ÉµÌ‰t½È€ˆˆ¤4(€€€‘•‰Õœ€ô‘¥Ð¡¥Ñ•´¹•Ð ‰‘•‰Õœˆ¤½Èíô¤4(€€€‘•‰Õl‰•á…Ñ}µ…Ñ ‰t€ôì4(€€€€€€€€‰µ…Ñ¡}­¥¹ˆèµ…Ñ¡}­¥¹°4(€€€€€€€€‰µ…Ñ¡•‘}Ñ•ÉµÌˆèmÙ…±Õ”™½ÈÙ…±Õ”¥¸µ…Ñ¡•‘}Ñ•ÉµÌ¹ÍÁ±¥Ð ˆ°ˆ¤¥˜Ù…±Õ•t°4(€€€ô4(€€€¥Ñ•µl‰‘•‰Õœ‰t€ô‘•‰Õœ4(4(4)‘•˜}ÅÕ•Éå}Ù…É¥…¹ÑÌ¡ÅÕ•ÍÑ¥½¸èÍÑÈ°Ñ½­•¹Ìè±¥ÍÑmÍÑÉt¤€´ø±¥ÍÑmÍÑÉtè4(€€€ÅÕ•É¥•Ìè±¥ÍÑmÍÑÉt€ômt4(€€€…¹‘}Ñ½­•¹Ì€ômÑ½­•¸™½ÈÑ½­•¸¥¸Ñ½­•¹Ì¥˜±•¸¡Ñ½­•¸¤€øô€Íulèát4(€€€…¹‘}ÅÕ•Éä€ô™ÑÍ}ÅÕ•Éå}™É½µ}Ñ½­•¹Ì¡…¹‘}Ñ½­•¹Ì°½Á•É…Ñ½Èô‰9ˆ°µ…á}Ñ•ÉµÌôà¤4(€€€¥˜…¹‘}ÅÕ•Éäè4(€€€€€€€ÅÕ•É¥•Ì¹…ÁÁ•¹¡…¹‘}ÅÕ•Éä¤4(€€€½É}ÅÕ•Éä€ô™ÑÍ}ÅÕ•Éå}™É½µ}Ñ½­•¹Ì¡Ñ½­•¹Ì°½Á•É…Ñ½Èô‰=Hˆ°µ…á}Ñ•ÉµÌôÈÐ¤4(€€€¥˜½É}ÅÕ•Éäè4(€€€€€€€ÅÕ•É¥•Ì¹…ÁÁ•¹¡½É}ÅÕ•Éä¤4(€€€ÅÕ•É¥•Ì¹•áÑ•¹¡Á¡É…Í•}ÅÕ•É¥•Ì¡ÅÕ•ÍÑ¥½¸¤¤4(€€€É•ÑÕÉ¸}Õ¹¥ÅÕ”¡ÅÕ•É¥•Ì¤4(4(4)‘•˜}¥¹™½Éµ…Ñ¥Ù•}…¹¡½É}Ñ½­•¹Ì 4(€€€½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°4(€€€ÅÕ•ÍÑ¥½¸èÍÑÈ°4(€€€€¨°4(€€€±¥µ¥Ðè¥¹Ð°4(¤€´ø±¥ÍÑmÑÕÁ±•mÍÑÈ°¥¹Ð°™±½…Ñutè4(€€€Ñ½­•¹Ì€ô}Õ¹¥ÅÕ”¡…¹½¹¥…±¥é”¡Ñ½­•¸¤™½ÈÑ½­•¸¥¸Ñ½­•¹Í}™½É}™ÑÌ¡ÅÕ•ÍÑ¥½¸°µ…á}Ñ½­•¹ÌôÌÈ¤¤4(€€€Ñ½­•¹Ì€ôl4(€€€€€€€Ñ½­•¸4(€€€€€€€™½ÈÑ½­•¸¥¸Ñ½­•¹Ì4(€€€€€€€¥˜±•¸¡Ñ½­•¸¤€øô€È4(€€€€€€€…¹¹½ÐÑ½­•¸¹¥Í‘¥¥Ð ¤4(€€€€€€€…¹…¹ä¡¡…É…Ñ•È¹¥Í…±¹Õ´ ¤™½È¡…É…Ñ•È¥¸Ñ½­•¸¤4(€€€t4(€€€¥˜¹½ÐÑ½­•¹Ì½È±¥µ¥Ð€ðô€Àè4(€€€€€€€É•ÑÕÉ¸mt4(€€€ÅÕ•Éå}½¹±ä€ô‰½½°¡½¹¸¹•á•ÕÑ” ‰AI5ÅÕ•Éå}½¹±äˆ¤¹™•Ñ¡½¹” ¥lÁt¤4(€€€¥˜ÅÕ•Éå}½¹±äè4(€€€€€€€€ŒQ¡”…Ñ…±½œ™¥±”É•µ…¥¹ÌÁÉ½Ñ•Ñ•‰äµ½‘”õÉ¼¸Q•µÁ½É…É¥±ä…±±½Ü„4(€€€€€€€€Œ½¹¹•Ñ¥½¸µ±½…°Q5@™ÑÌÕÙ½…ˆÙ¥•ÜÕÍ•½¹±ä™½È…±Õ±…Ñ¥½¸¸4(€€€€€€€½¹¸¹•á•ÕÑ” ‰AI5ÅÕ•Éå}½¹±äõ=ˆ¤4(€€€ÑÉäè4(€€€€€€€½¹¸¹•á•ÕÑ” 4(€€€€€€€€€€€€‰IQY%IQU0Q	1%9=Pa%MQLÑ•µÀ¹™ÑÍ}Ý½É‘}Ù½…ˆ€ˆ4(€€€€€€€€€€€€‰UM%9™ÑÌÕÙ½…ˆ¡µ…¥¸°™ÑÍ}Ý½É°€É½Üœ¤ˆ4(€€€€€€€€¤4(€€€™¥¹…±±äè4(€€€€€€€¥˜ÅÕ•Éå}½¹±äè4(€€€€€€€€€€€½¹¸¹•á•ÕÑ” ‰AI5ÅÕ•Éå}½¹±äõ=8ˆ¤4(€€€Á±…•¡½±‘•ÉÌ€ô€ˆ°ˆ¹©½¥¸ ˆüˆ™½È|¥¸Ñ½­•¹Ì¤4(€€€É½ÝÌ€ô½¹¸¹•á•ÕÑ” 4(€€€€€€€˜‰M1PÑ•É´°‘½ŒI=4Ñ•µÀ¹™ÑÍ}Ý½É‘}Ù½…ˆ]!IÑ•É´%8€¡íÁ±…•¡½±‘•ÉÍô¤ˆ°4(€€€€€€€Ñ½­•¹Ì°4(€€€€¤4(€€€‘½Õµ•¹Ñ}½Õ¹Ñ}É½Ü€ô½¹¸¹•á•ÕÑ” 4(€€€€€€€€ˆˆˆ4(€€€€€€€M1P=U9P¡%MQ%9P¹‘½}Á¬¤L½Õ¹Ð4(€€€€€€€I=4‘½Õµ•¹Ð4(€€€€€€€)=%8¡Õ¹¬Œ=8Œ¹‘½}Á¬€ô¹‘½}Á¬4(€€€€€€€]!I¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€9Œ¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€ˆˆˆ4(€€€€¤¹™•Ñ¡½¹” ¤4(€€€‘½Õµ•¹Ñ}½Õ¹Ð€ô¥¹Ð¡‘½Õµ•¹Ñ}½Õ¹Ñ}É½Ýl‰½Õ¹Ð‰t¥˜‘½Õµ•¹Ñ}½Õ¹Ñ}É½Ü•±Í”€À¤4(€€€¥˜‘½Õµ•¹Ñ}½Õ¹Ð€ðô€Àè4(€€€€€€€É•ÑÕÉ¸mt4(€€€Ñ½­•¹}½É‘•È€ôíÑ½­•¸è¥¹‘•à™½È¥¹‘•à°Ñ½­•¸¥¸•¹Õµ•É…Ñ”¡Ñ½­•¹Ì¥ô4(€€€É…¹­•è±¥ÍÑmÑÕÁ±•mÍÑÈ°¥¹Ð°™±½…Ñut€ômt4(€€€™½ÈÉ½Ü¥¸É½ÝÌè4(€€€€€€€Ñ½­•¸€ôÍÑÈ¡É½Ýl‰Ñ•É´‰t¤4(€€€€€€€ÅÕ•Éå}Ñ•áÐ€ô™ÑÍ}ÅÕ•Éå}™É½µ}Ñ½­•¹Ì¡mÑ½­•¹t°½Á•É…Ñ½Èô‰=Hˆ°µ…á}Ñ•ÉµÌôÄ¤4(€€€€€€€¥˜¹½ÐÅÕ•Éå}Ñ•áÐè4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€‘½Õµ•¹Ñ}‘™}É½Ü€ô½¹¸¹•á•ÕÑ” 4(€€€€€€€€€€€€ˆˆˆ4(€€€€€€€€€€€M1P=U9P¡%MQ%9PŒ¹‘½}Á¬¤L½Õ¹Ð4(€€€€€€€€€€€I=4™ÑÍ}Ý½É4(€€€€€€€€€€€)=%8¡Õ¹¬Œ=8Œ¹¡Õ¹­}Á¬€ô™ÑÍ}Ý½É¹É½Ý¥4(€€€€€€€€€€€)=%8‘½Õµ•¹Ð=8¹‘½}Á¬€ôŒ¹‘½}Á¬4(€€€€€€€€€€€]!I™ÑÍ}Ý½É5Q €ü4(€€€€€€€€€€€€€9Œ¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€€€€€9¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€€€€ˆˆˆ°4(€€€€€€€€€€€€¡ÅÕ•Éå}Ñ•áÐ°¤°4(€€€€€€€€¤¹™•Ñ¡½¹” ¤4(€€€€€€€‘½Õµ•¹Ñ}‘˜€ô¥¹Ð¡‘½Õµ•¹Ñ}‘™}É½Ýl‰½Õ¹Ð‰t¥˜‘½Õµ•¹Ñ}‘™}É½Ü•±Í”€À¤4(€€€€€€€É…É•}‘½Õµ•¹Ñ}±¥µ¥Ð€ôµ…à È°¥¹Ð¡‘½Õµ•¹Ñ}½Õ¹Ð€¨€À¸ÀÔ¤¤4(€€€€€€€¥˜‘½Õµ•¹Ñ}‘˜€ðô€À½È‘½Õµ•¹Ñ}‘˜€øÉ…É•}‘½Õµ•¹Ñ}±¥µ¥Ðè4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€¥‘˜€ôµ…Ñ ¹±½œ ¡‘½Õµ•¹Ñ}½Õ¹Ð€¬€Ä¤€¼€¡‘½Õµ•¹Ñ}‘˜€¬€Ä¤¤4(€€€€€€€±•¹Ñ¡}‰½¹ÕÌ€ô€Ä¸À€¬µ¥¸¡±•¸¡Ñ½­•¸¤°€ÄÈ¤€¼€ÄÈ¸À4(€€€€€€€É…¹­•¹…ÁÁ•¹ ¡Ñ½­•¸°‘½Õµ•¹Ñ}‘˜°¥‘˜€¨±•¹Ñ¡}‰½¹ÕÌ¤¤4(€€€É…¹­•¹Í½ÉÐ 4(€€€€€€€­•äõ±…µ‰‘„¥Ñ•´è€ 4(€€€€€€€€€€€€µ¥Ñ•µlÉt°4(€€€€€€€€€€€¥Ñ•µlÅt°4(€€€€€€€€€€€Ñ½­•¹}½É‘•È¹•Ð¡¥Ñ•µlÁt°±•¸¡Ñ½­•¹Ì¤¤°4(€€€€€€€€¤4(€€€€¤4(€€€É•ÑÕÉ¸É…¹­•‘lé±¥µ¥Ñt4(4(4)‘•˜}Ñ½­•¹}Á½Í¥Ñ¥½¸¡Ñ•áÐèÍÑÈ°Ñ½­•¸èÍÑÈ¤€´ø¥¹Ðè4(€€€Á½Í¥Ñ¥½¸€ô…¹½¹¥…±¥é”¡Ñ•áÐ¤¹™¥¹¡…¹½¹¥…±¥é”¡Ñ½­•¸¤¤4(€€€É•ÑÕÉ¸Á½Í¥Ñ¥½¸¥˜Á½Í¥Ñ¥½¸€øô€À•±Í”€Å|ÀÀÁ|ÀÀÁ|ÀÀÀ4(4(4)‘•˜}ÉÕ¹}™ÑÌ¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°Ñ…‰±”èÍÑÈ°ÅÕ•Éå}Ñ•áÐèÍÑÈ°Ñ½Á}¬è¥¹Ð°Í½ÕÉ”èÍÑÈ¤€´ø±¥ÍÑmÍÅ±¥Ñ”Ì¹I½Ýtè4(€€€¥˜Ñ…‰±”€„ô€‰™ÑÍ}Ý½Éˆè4(€€€€€€€É…¥Í”Y…±Õ•ÉÉ½È¡Ñ…‰±”¤4(€€€Í½ÕÉ•}ÍÅ°°Í½ÕÉ•}Á…É…µÌ€ô}Í½ÕÉ•}™¥±Ñ•È ‰ˆ°Í½ÕÉ”¤4(€€€Í½É•}•áÁÈ€ô€‰‰´ÈÔ¡™ÑÍ}Ý½É°€È¸À°€Ä¸À¤ˆ4(€€€ÍÅ°€ô˜ˆˆˆ4(€€€€€€€M1Pí}IMU1Q}=1U59Mô°íÍ½É•}•áÁÉôLÍ½É”4(€€€€€€€I=4™ÑÍ}Ý½É4(€€€€€€€)=%8¡Õ¹¬Œ=8Œ¹¡Õ¹­}Á¬€ô™ÑÍ}Ý½É¹É½Ý¥4(€€€€€€€)=%8‘½Õµ•¹Ð=8¹‘½}Á¬€ôŒ¹‘½}Á¬4(€€€€€€€]!I™ÑÍ}Ý½É5Q €ü4(€€€€€€€€€9Œ¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€9¹Ù¥Í¥‰±•}Õ¹Ñ¥°%L9U104(€€€€€€€€€íÍ½ÕÉ•}ÍÅ±ô4(€€€€€€€=IH	dÍ½É”M4(€€€€€€€1%5%P€ü4(€€€€ˆˆˆ4(€€€Á…É…µÌè±¥ÍÑm¹åt€ômÅÕ•Éå}Ñ•áÐ°€©Í½ÕÉ•}Á…É…µÌ°Ñ½Á}­t4(€€€ÑÉäè4(€€€€€€€É•ÑÕÉ¸±¥ÍÐ¡½¹¸¹•á•ÕÑ”¡ÍÅ°°Á…É…µÌ¤¤4(€€€•á•ÁÐÍÅ±¥Ñ”Ì¹=Á•É…Ñ¥½¹…±ÉÉ½È…Ì•áŒè4(€€€€€€€¥˜}¥Í}Í…™•}™ÑÍ}ÅÕ•Éå}Íå¹Ñ…á}•ÉÉ½È¡•áŒ¤è4(€€€€€€€€€€€É•ÑÕÉ¸mt4(€€€€€€€É…¥Í”4(4(4)‘•˜}¥Í}Í…™•}™ÑÍ}ÅÕ•Éå}Íå¹Ñ…á}•ÉÉ½È¡•áŒèÍÅ±¥Ñ”Ì¹=Á•É…Ñ¥½¹…±ÉÉ½È¤€´ø‰½½°è4(€€€€ˆˆ‰I•½¹¥é”½¹±äQLÔÁ…ÉÍ•È•ÉÉ½ÉÌ…ÐÑ¡”5Q •á•ÕÑ¥½¸‰½Õ¹‘…Éä¸ˆˆˆ4(€€€¥˜•Ñ…ÑÑÈ¡•áŒ°€‰ÍÅ±¥Ñ•}•ÉÉ½É½‘”ˆ°9½¹”¤€„ôÍÅ±¥Ñ”Ì¹ME1%Q}II=Hè4(€€€€€€€É•ÑÕÉ¸…±Í”4(€€€¥˜•Ñ…ÑÑÈ¡•áŒ°€‰ÍÅ±¥Ñ•}•ÉÉ½É¹…µ”ˆ°9½¹”¤€„ô€‰ME1%Q}II=Hˆè4(€€€€€€€É•ÑÕÉ¸…±Í”4(€€€‘•Ñ…¥°€ôÍÑÈ¡•áŒ¤4(€€€É•ÑÕÉ¸‘•Ñ…¥°€ôô€‰Õ¹Ñ•Éµ¥¹…Ñ•ÍÑÉ¥¹œˆ½È‘•Ñ…¥°¹ÍÑ…ÉÑÍÝ¥Ñ  4(€€€€€€€€‰™ÑÌÔèÍå¹Ñ…à•ÉÉ½È¹•…È€ˆ4(€€€€¤4(4(4)‘•˜}É½Ý}Ñ½}É•ÍÕ±Ð¡É½ÜèÍÅ±¥Ñ”Ì¹I½Ü°€¨°Í¥¹…°èÍÑÈ°Í½É”è™±½…Ð¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€‘½Õµ•¹Ñ}µ•Ñ„€ô}±½…‘}©Í½¹}½‰©•Ð¡É½Ýl‰‘½Õµ•¹Ñ}µ•Ñ…‘…Ñ…}©Í½¸‰t¤4(€€€¡Õ¹­}µ•Ñ„€ô}±½…‘}©Í½¹}½‰©•Ð¡É½Ýl‰¡Õ¹­}µ•Ñ…‘…Ñ…}©Í½¸‰t¤4(€€€µ•Ñ„€ôì4(€€€€€€€€¨©‘½Õµ•¹Ñ}µ•Ñ„°4(€€€€€€€€¨©¡Õ¹­}µ•Ñ„°4(€€€€€€€€‰Í½ÕÉ”ˆèÍÑÈ¡É½Ýl‰Í½ÕÉ”‰t½È‘½Õµ•¹Ñ}µ•Ñ„¹•Ð ‰Í½ÕÉ”ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰Í½ÕÉ•}¥ˆèÍÑÈ¡É½Ýl‰Í½ÕÉ•}¥‰t½È‘½Õµ•¹Ñ}µ•Ñ„¹•Ð ‰Í½ÕÉ•}¥ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰Í½ÕÉ•}ÑåÁ”ˆèÍÑÈ¡É½Ýl‰Í½ÕÉ•}ÑåÁ”‰t½È‘½Õµ•¹Ñ}µ•Ñ„¹•Ð ‰Í½ÕÉ•}ÑåÁ”ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰Á…Ñ ˆèÍÑÈ¡É½Ýl‰Á…Ñ ‰t½È‘½Õµ•¹Ñ}µ•Ñ„¹•Ð ‰Á…Ñ ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰ÕÉ¤ˆèÍÑÈ¡É½Ýl‰ÕÉ¤‰t½È‘½Õµ•¹Ñ}µ•Ñ„¹•Ð ‰ÕÉ¤ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰Ñ¥Ñ±”ˆèÍÑÈ¡É½Ýl‰Ñ¥Ñ±”‰t½È‘½Õµ•¹Ñ}µ•Ñ„¹•Ð ‰Ñ¥Ñ±”ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰±…¹Õ…”ˆèÍÑÈ¡É½Ýl‰¡Õ¹­}±…¹Õ…”‰t½ÈÉ½Ýl‰‘½Õµ•¹Ñ}±…¹Õ…”‰t½È‘½Õµ•¹Ñ}µ•Ñ„¹•Ð ‰±…¹Õ…”ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰Í•Ñ¥½¹}Á…Ñ ˆèÍÑÈ¡É½Ýl‰Í•Ñ¥½¹}Á…Ñ ‰t½È¡Õ¹­}µ•Ñ„¹•Ð ‰Í•Ñ¥½¹}Á…Ñ ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰¡Õ¹­}¥¹‘•àˆèÉ½Ýl‰¡Õ¹­}¥¹‘•à‰t°4(€€€€€€€€‰‘½}¥ˆèÍÑÈ¡É½Ýl‰‘½}¥‰t½È¡Õ¹­}µ•Ñ„¹•Ð ‰‘½}¥ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰¡Õ¹­}¡…Í ˆèÍÑÈ¡É½Ýl‰¡Õ¹­}¡…Í ‰t½È¡Õ¹­}µ•Ñ„¹•Ð ‰¡Õ¹­}¡…Í ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰Ñ•áÑ}¡…Í ˆèÍÑÈ¡É½Ýl‰Ñ•áÑ}¡…Í ‰t½È¡Õ¹­}µ•Ñ„¹•Ð ‰Ñ•áÑ}¡…Í ˆ¤½È€ˆˆ¤°4(€€€€€€€€‰½¹Ñ•¹Ñ}¡…Í ˆèÍÑÈ¡É½Ýl‰‘½Õµ•¹Ñ}½¹Ñ•¹Ñ}¡…Í ‰t½ÈÉ½Ýl‰¡Õ¹­}½¹Ñ•¹Ñ}¡…Í ‰t½È‘½Õµ•¹Ñ}µ•Ñ„¹•Ð ‰½¹Ñ•¹Ñ}¡…Í ˆ¤½È€ˆˆ¤°4(€€€ô4(€€€É•ÑÕÉ¸ì4(€€€€€€€€‰É…¹¬ˆè€À°4(€€€€€€€€‰¥ˆèÍÑÈ¡É½Ýl‰¡Õ¹­}Õ¥‰t¤°4(€€€€€€€€‰‘¥ÍÑ…¹”ˆè9½¹”°4(€€€€€€€€‰Í½É”ˆèÍ½É”°4(€€€€€€€€‰Ñ•áÐˆèÍÑÈ¡É½Ýl‰Ñ•áÐ‰t½È€ˆˆ¤°4(€€€€€€€€‰µ•Ñ…‘…Ñ„ˆèµ•Ñ„°4(€€€€€€€€‰Í¥¹…±ÌˆèmÍ¥¹…±t°4(€€€ô4(4(4)‘•˜}É…¹­•¡É½ÝÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè4(€€€™½ÈÉ…¹¬°É½Ü¥¸•¹Õµ•É…Ñ”¡É½ÝÌ°ÍÑ…ÉÐôÄ¤è4(€€€€€€€É½Ýl‰É…¹¬‰t€ôÉ…¹¬4(€€€É•ÑÕÉ¸É½ÝÌ4(4(4)‘•˜}Í½ÕÉ•}™¥±Ñ•È¡…±¥…ÌèÍÑÈ°Í½ÕÉ”èÍÑÈ¤€´øÑÕÁ±•mÍÑÈ°±¥ÍÑm¹åutè4(€€€¥˜Í½ÕÉ”€ôô€‰…¹äˆè4(€€€€€€€É•ÑÕÉ¸€ˆˆ°mt4(€€€É•ÑÕÉ¸˜‰9í…±¥…Íô¹Í½ÕÉ”€ô€üˆ°mÍ½ÕÉ•t4(4(4)‘•˜}‘½Õµ•¹Ñ}µ•Ñ…‘…Ñ„¡µ•Ñ„è‘¥ÑmÍÑÈ°¹åt¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€­•åÌ€ôl‰Í½ÕÉ”ˆ°€‰Í½ÕÉ•}¥ˆ°€‰Í½ÕÉ•}ÑåÁ”ˆ°€‰Á…Ñ ˆ°€‰ÕÉ¤ˆ°€‰Ñ¥Ñ±”ˆ°€‰±…¹Õ…”ˆ°€‰É½½Ðˆ°€‰½¹Ñ•¹Ñ}¡…Í ‰t4(€€€É•ÑÕÉ¸í­•äèµ•Ñ„¹•Ð¡­•ä¤™½È­•ä¥¸­•åÌ¥˜}¡…Í}µ•Ñ…‘…Ñ…}Ù…±Õ”¡µ•Ñ„¹•Ð¡­•ä¤¥ô4(4(4)‘•˜}¡Õ¹­}µ•Ñ…‘…Ñ„¡µ•Ñ„è‘¥ÑmÍÑÈ°¹åt¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€‘½Õµ•¹Ñ}­•åÌ€ôì‰Í½ÕÉ”ˆ°€‰Í½ÕÉ•}¥ˆ°€‰Í½ÕÉ•}ÑåÁ”ˆ°€‰Á…Ñ ˆ°€‰ÕÉ¤ˆ°€‰Ñ¥Ñ±”ˆ°€‰±…¹Õ…”ˆ°€‰É½½Ðˆ°€‰½¹Ñ•¹Ñ}¡…Í ‰ô4(€€€­•åÌ€ôl4(€€€€€€€€‰¡Õ¹­}Ñ¥Ñ±”ˆ°4(€€€€€€€€‰Í•Ñ¥½¹}Á…Ñ ˆ°4(€€€€€€€€‰¡Õ¹­}¥¹‘•àˆ°4(€€€€€€€€‰¡Õ¹­}¡…Í ˆ°4(€€€€€€€€‰Ñ•áÑ}¡…Í ˆ°4(€€€€€€€€‰¡Õ¹­•É}Ù•ÉÍ¥½¸ˆ°4(€€€€€€€€‰Á…”ˆ°4(€€€€€€€€‰Í±¥‘”ˆ°4(€€€€€€€€‰±¥¹•Ìˆ°4(€€€t4(€€€½ÕÑÁÕÐ€ôí­•äèµ•Ñ„¹•Ð¡­•ä¤™½È­•ä¥¸­•åÌ¥˜}¡…Í}µ•Ñ…‘…Ñ…}Ù…±Õ”¡µ•Ñ„¹•Ð¡­•ä¤¥ô4(€€€™½È­•ä°Ù…±Õ”¥¸µ•Ñ„¹¥Ñ•µÌ ¤è4(€€€€€€€¥˜­•ä¥¸‘½Õµ•¹Ñ}­•åÌ½È­•ä¥¸½ÕÑÁÕÐ½È¹½Ð}¡…Í}µ•Ñ…‘…Ñ…}Ù…±Õ”¡Ù…±Õ”¤è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€½ÕÑÁÕÑm­•åt€ôÙ…±Õ”4(€€€É•ÑÕÉ¸½ÕÑÁÕÐ4(4(4)‘•˜}±½…Ñ¥½¹}µ•Ñ…‘…Ñ„¡µ•Ñ„è‘¥ÑmÍÑÈ°¹åt¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€­•åÌ€ôl‰Í•Ñ¥½¹}Á…Ñ ˆ°€‰¡Õ¹­}Ñ¥Ñ±”ˆ°€‰¡Õ¹­}¥¹‘•àˆ°€‰Á…”ˆ°€‰Í±¥‘”ˆ°€‰±¥¹•Ì‰t4(€€€É•ÑÕÉ¸í­•äèµ•Ñ„¹•Ð¡­•ä¤™½È­•ä¥¸­•åÌ¥˜}¡…Í}µ•Ñ…‘…Ñ…}Ù…±Õ”¡µ•Ñ„¹•Ð¡­•ä¤¥ô4(4(4)‘•˜}¡…Í}µ•Ñ…‘…Ñ…}Ù…±Õ”¡Ù…±Õ”è¹ä¤€´ø‰½½°è4(€€€É•ÑÕÉ¸Ù…±Õ”¥Ì¹½Ð9½¹”…¹Ù…±Õ”€„ô€ˆˆ4(4(4)‘•˜}¥‘•¹Ñ¥™¥•É}­¥¹¡¥‘•¹Ñ¥™¥•ÈèÍÑÈ¤€´øÍÑÈè4(€€€Ù…±Õ”€ô¥‘•¹Ñ¥™¥•È¹ÍÑÉ¥À ¤4(€€€…¹½¹¥…°€ô…¹½¹¥…±¥é”¡Ù…±Õ”¤4(€€€¥˜¹½ÐÙ…±Õ”½È…¹½¹¥…°¥¸}=55=9}]-}I=9e5Lè4(€€€€€€€É•ÑÕÉ¸€ˆˆ4(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰¡ÑÑÁÌüè¼½myqÌ¤ùquõt¬ˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰ÕÉ°ˆ4(€€€¥˜É”¹™Õ±±µ…Ñ ¡Èˆ½mµi„µèÀ´å|¸¼èµuìÈ±ôˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰Á…Ñ ˆ4(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰mµi„µèÀ´å|¸è¼µt­p¸ üéµ‘ñÑáÑñ±½ñÁ‘™ñ‘½àýñÁÁÑàýñá±Íáñ©Í½¹ñå„ýµ±ñÑ½µ±ñ¥¹¥ñÁåñ©Íñ©ÍáñÑÍñÑÍáñ©…Ù…ñ½ñÉÍñÍñÍÅ°¤ˆ°Ù…±Õ”°É”¹$¤è4(€€€€€€€É•ÑÕÉ¸€‰™¥±”ˆ4(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰lÀ´å„µ™µuìáôµlÀ´å„µ™µuìÑôµlÀ´å„µ™µuìÑôµlÀ´å„µ™µuìÑôµlÀ´å„µ™µuìÄÉôˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰ÕÕ¥ˆ4(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰lÀ´å„µ™µuìÄÈ±ôˆ°Ù…±Õ”¤…¹É”¹Í•…É ¡È‰m„µ™µtˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰¡•àˆ4(€€€¥˜É”¹Í•…É ¡È‰mµi„µétˆ°Ù…±Õ”¤…¹É”¹Í•…É ¡È‰qˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰…±Á¡…}¹Õµ•É¥Œˆ4(€€€¥˜É”¹Í•…É ¡È‰m|ètˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰Íåµ‰½°ˆ4(€€€¥˜É”¹Í•…É ¡È‰mµi„µé}umµi„µèÀ´å}t¨ üél¸è¼µumµi„µèÀ´å}t¬¤¬ˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰ÅÕ…±¥™¥•ˆ4(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰mµiumµhÀ´åt¬ üé}mµhÀ´åt¬¤¬ˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰½¹ÍÑ…¹Ðˆ4(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰mµitým„µét¬ üémµiumµi„µèÀ´åt¬¥ìÄ±ôˆ°Ù…±Õ”¤è4(€€€€€€€É•ÑÕÉ¸€‰…µ•°ˆ4(€€€¥˜É”¹™Õ±±µ…Ñ ¡È‰mµiuìÈ±õÌüˆ°Ù…±Õ”¤…¹€È€ðô±•¸¡Ù…±Õ”¤€ðô€ÄÈè4(€€€€€€€É•ÑÕÉ¸€‰Ý•…­}…É½¹å´ˆ4(€€€É•ÑÕÉ¸€ˆˆ4(4(4)‘•˜}Í•Ñ}µ•Ñ„¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°­•äèÍÑÈ°Ù…±Õ”èÍÑÈ¤€´ø9½¹”è4(€€€½¹¸¹•á•ÕÑ” 4(€€€€€€€€‰%9MIP%9Q<‘…Ñ…‰…Í•}µ•Ñ„¡­•ä°Ù…±Õ”¤Y1UL ü°€ü¤=8=91%P¡­•ä¤<UAQMPÙ…±Õ”õ•á±Õ‘•¹Ù…±Õ”ˆ°4(€€€€€€€€¡­•ä°Ù…±Õ”¤°4(€€€€¤4(4(4)‘•˜}•Ñ}µ•Ñ„¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°­•äèÍÑÈ¤€´øÍÑÈè4(€€€ÑÉäè4(€€€€€€€É½Ü€ô½¹¸¹•á•ÕÑ” ‰M1PÙ…±Õ”I=4‘…Ñ…‰…Í•}µ•Ñ„]!I­•ä€ô€üˆ°€¡­•ä°¤¤¹™•Ñ¡½¹” ¤4(€€€•á•ÁÐÍÅ±¥Ñ”Ì¹=Á•É…Ñ¥½¹…±ÉÉ½Èè4(€€€€€€€É•ÑÕÉ¸€ˆˆ4(€€€É•ÑÕÉ¸ÍÑÈ¡É½Ýl‰Ù…±Õ”‰t¤¥˜É½Ü•±Í”€ˆˆ4(4(4)‘•˜}½Õ¹Ð¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°Ñ…‰±”èÍÑÈ¤€´ø¥¹Ðè4(€€€ÑÉäè4(€€€€€€€É•ÑÕÉ¸¥¹Ð¡½¹¸¹•á•ÕÑ”¡˜‰M1P=U9P ¨¤I=4íÑ…‰±•ôˆ¤¹™•Ñ¡½¹” ¥lÁt¤4(€€€•á•ÁÐÍÅ±¥Ñ”Ì¹=Á•É…Ñ¥½¹…±ÉÉ½Èè4(€€€€€€€É•ÑÕÉ¸€À4(4(4)‘•˜}‘É½Á}…Ñ…±½}½‰©•ÑÌ¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸¤€´ø9½¹”è4(€€€½¹¸¹•á•ÕÑ•ÍÉ¥ÁÐ 4(€€€€€€€€ˆˆˆ4(€€€€€€€I=@Q	1%a%MQL¥‘•¹Ñ¥™¥•É}ÍÕÁÁÉ•ÍÍ•ì4(€€€€€€€I=@Q	1%a%MQL¥‘•¹Ñ¥™¥•É}Á½ÍÑ¥¹œì4(€€€€€€€I=@Q	1%a%MQL¥‘•¹Ñ¥™¥•É}…±¥…Ìì4(€€€€€€€I=@Q	1%a%MQL¥‘•¹Ñ¥™¥•É}Ñ•É´ì4(€€€€€€€I=@Q	1%a%MQL¥‘•¹Ñ¥™¥•É}½ÕÉÉ•¹”ì4(€€€€€€€I=@Q	1%a%MQL‘½Õµ•¹Ñ}±½½­ÕÀì4(€€€€€€€I=@Q	1%a%MQL™ÑÍ}Ý½Éì4(€€€€€€€I=@Q	1%a%MQL™¥±•}™ÑÌì4(€€€€€€€I=@Q	1%a%MQL¡Õ¹¬ì4(€€€€€€€I=@Q	1%a%MQL‘½Õµ•¹Ðì4(€€€€€€€I=@Q	1%a%MQL‘…Ñ…‰…Í•}µ•Ñ„ì4(€€€€€€€€ˆˆˆ4(€€€€¤4(4(4)‘•˜}¥Í}ÍÕÁÁÉ•ÍÍ•‘}¥‘•¹Ñ¥™¥•È¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°…¹½¹¥…°èÍÑÈ¤€´ø‰½½°è4(€€€ÑÉäè4(€€€€€€€É½Ü€ô½¹¸¹•á•ÕÑ” ‰M1P€ÄI=4¥‘•¹Ñ¥™¥•É}ÍÕÁÁÉ•ÍÍ•]!I…¹½¹¥…±}Ù…±Õ”€ô€üˆ°€¡…¹½¹¥…°°¤¤¹™•Ñ¡½¹” ¤4(€€€•á•ÁÐÍÅ±¥Ñ”Ì¹=Á•É…Ñ¥½¹…±ÉÉ½Èè4(€€€€€€€É•ÑÕÉ¸…±Í”4(€€€É•ÑÕÉ¸‰½½°¡É½Ü¤4(4(4)‘•˜}Ñ…‰±•}•á¥ÍÑÌ¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°Ñ…‰±”èÍÑÈ¤€´ø‰½½°è4(€€€É½Ü€ô½¹¸¹•á•ÕÑ” ‰M1P€ÄI=4ÍÅ±¥Ñ•}µ…ÍÑ•È]!IÑåÁ”€ô€Ñ…‰±”œ9¹…µ”€ô€üˆ°€¡Ñ…‰±”°¤¤¹™•Ñ¡½¹” ¤4(€€€É•ÑÕÉ¸‰½½°¡É½Ü¤4(4(4)‘•˜}½±Õµ¹}•á¥ÍÑÌ¡½¹¸èÍÅ±¥Ñ”Ì¹½¹¹•Ñ¥½¸°Ñ…‰±”èÍÑÈ°½±Õµ¸èÍÑÈ¤€´ø‰½½°è4(€€€ÑÉäè4(€€€€€€€É•ÑÕÉ¸…¹ä¡É½Ýl‰¹…µ”‰t€ôô½±Õµ¸™½ÈÉ½Ü¥¸½¹¸¹•á•ÕÑ”¡˜‰AI5Ñ…‰±•}¥¹™¼¡íÑ…‰±•ô¤ˆ¤¤4(€€€•á•ÁÐÍÅ±¥Ñ”Ì¹=Á•É…Ñ¥½¹…±ÉÉ½Èè4(€€€€€€€É•ÑÕÉ¸…±Í”4(4(4)‘•˜}±½…‘}©Í½¹}½‰©•Ð¡Ù…±Õ”è¹ä¤€´ø‘¥ÑmÍÑÈ°¹åtè4(€€€ÑÉäè4(€€€€€€€Á…ÉÍ•€ô©Í½¸¹±½…‘Ì¡ÍÑÈ¡Ù…±Õ”½È€‰íôˆ¤¤4(€€€•á•ÁÐ©Í½¸¹)M=9•½‘•ÉÉ½Èè4(€€€€€€€É•ÑÕÉ¸íô4(€€€É•ÑÕÉ¸Á…ÉÍ•¥˜¥Í¥¹ÍÑ…¹”¡Á…ÉÍ•°‘¥Ð¤•±Í”íô4(4(4)‘•˜}¥¹Ñ}½É}¹½¹”¡Ù…±Õ”è¹ä¤€´ø¥¹Ðð9½¹”è4(€€€ÑÉäè4(€€€€€€€É•ÑÕÉ¸¥¹Ð¡Ù…±Õ”¤4(€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è4(€€€€€€€É•ÑÕÉ¸9½¹”4(4(4)‘•˜}Õ¹¥ÅÕ”¡Ù…±Õ•Ìè%Ñ•É…‰±•mÍÑÉt¤€´ø±¥ÍÑmÍÑÉtè4(€€€Í••¸èÍ•ÑmÍÑÉt€ôÍ•Ð ¤4(€€€½ÕÑÁÕÐè±¥ÍÑmÍÑÉt€ômt4(€€€™½ÈÙ…±Õ”¥¸Ù…±Õ•Ìè4(€€€€€€€Ù…±Õ”€ôÍÑÈ¡Ù…±Õ”¤¹ÍÑÉ¥À ¤4(€€€€€€€¥˜¹½ÐÙ…±Õ”è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€­•ä€ô…¹½¹¥…±¥é”¡Ù…±Õ”¤4(€€€€€€€¥˜­•ä¥¸Í••¸è4(€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€Í••¸¹…‘¡­•ä¤4(€€€€€€€½ÕÑÁÕÐ¹…ÁÁ•¹¡Ù…±Õ”¤4(€€€É•ÑÕÉ¸½ÕÑÁÕÐ4