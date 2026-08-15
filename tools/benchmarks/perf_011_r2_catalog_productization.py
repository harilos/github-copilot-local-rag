from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


SEED = 1102
DOCX_COUNT = 40
PPTX_COUNT = 20
DOCX_RECORDS = 5
PPTX_RECORDS = 10
TOTAL_RECORDS = 400
UPDATED_DOCX = tuple(range(4))
UPDATED_PPTX = tuple(range(2))
UPDATED_RECORDS = 40


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(raw.encode("utf-8"))


def _canonicalize_openxml(path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".canonical")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as destination:
        for name in sorted(source.namelist()):
            original = source.getinfo(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = original.create_system
            info.external_attr = original.external_attr
            destination.writestr(info, source.read(name))
    os.replace(temporary, path)


def _generate_corpus(initial: Path, updated: Path) -> dict[str, Any]:
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches

    initial.mkdir(parents=True)
    updated.mkdir(parents=True)
    generator = random.Random(SEED)

    def docx(path: Path, index: int, generation: int, fixture_token: int) -> None:
        document = Document()
        document.core_properties.author = "Local RAG PERF-011-R2"
        document.core_properties.created = datetime(2026, 8, 15, tzinfo=timezone.utc)
        document.core_properties.modified = datetime(2026, 8, 15, tzinfo=timezone.utc)
        document.add_heading(f"Catalog operations guide {index:02d}", level=0)
        words = (
            "catalog office update alpha beta gamma 日本語 project "
            f"DOCX{index:02d} generation{generation} seed{fixture_token} RFC{110000 + index}"
        )
        for section in range(DOCX_RECORDS):
            document.add_heading(f"Section {section + 1}", level=1)
            body = " ".join(f"{words} item{section:02d}-{repeat:02d}." for repeat in range(4))
            document.add_paragraph(body)
        table = document.add_table(rows=2, cols=3)
        for row in range(2):
            for column in range(3):
                table.cell(row, column).text = (
                    f"Metric {index:02d}-{row}-{column} generation {generation}"
                )
        document.save(path)
        _canonicalize_openxml(path)

    def pptx(path: Path, index: int, generation: int, fixture_token: int) -> None:
        presentation = Presentation()
        presentation.core_properties.author = "Local RAG PERF-011-R2"
        presentation.core_properties.created = datetime(2026, 8, 15, tzinfo=timezone.utc)
        presentation.core_properties.modified = datetime(2026, 8, 15, tzinfo=timezone.utc)
        presentation.slide_width = Inches(13.333)
        presentation.slide_height = Inches(7.5)
        for slide_index in range(PPTX_RECORDS):
            slide = presentation.slides.add_slide(presentation.slide_layouts[6])
            title = slide.shapes.add_textbox(Inches(0.5), Inches(0.4), Inches(12), Inches(0.7))
            title.text_frame.text = f"Quarterly catalog plan {index:02d} / {slide_index + 1}"
            body = slide.shapes.add_textbox(Inches(0.7), Inches(1.4), Inches(11.5), Inches(4.8))
            frame = body.text_frame
            frame.text = (
                f"PPTX{index:02d} slide {slide_index + 1} generation {generation} "
                f"seed {fixture_token} catalog update 日本語 RFC{120000 + index}."
            )
            for bullet in range(3):
                paragraph = frame.add_paragraph()
                paragraph.level = 0
                paragraph.text = (
                    f"Action {bullet + 1}: validate Office record {index:02d}-{slide_index:02d}."
                )
        presentation.save(path)
        _canonicalize_openxml(path)

    for index in range(DOCX_COUNT):
        name = f"word/catalog-guide-{index:02d}.docx"
        target = initial / name
        target.parent.mkdir(parents=True, exist_ok=True)
        docx(target, index, 0, generator.randrange(1_000_000_000))
    for index in range(PPTX_COUNT):
        name = f"powerpoint/catalog-plan-{index:02d}.pptx"
        target = initial / name
        target.parent.mkdir(parents=True, exist_ok=True)
        pptx(target, index, 0, generator.randrange(1_000_000_000))
    shutil.copytree(initial, updated, dirs_exist_ok=True)
    for index in UPDATED_DOCX:
        docx(
            updated / f"word/catalog-guide-{index:02d}.docx",
            index,
            1,
            generator.randrange(1_000_000_000),
        )
    for index in UPDATED_PPTX:
        pptx(
            updated / f"powerpoint/catalog-plan-{index:02d}.pptx",
            index,
            1,
            generator.randrange(1_000_000_000),
        )

    def hashes(root: Path) -> list[dict[str, Any]]:
        return [
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256_bytes(path.read_bytes()),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]

    initial_hashes = hashes(initial)
    updated_hashes = hashes(updated)
    return {
        "seed": SEED,
        "initial_files": initial_hashes,
        "updated_files": updated_hashes,
        "initial_file_manifest_hash": _canonical_hash(initial_hashes),
        "updated_file_manifest_hash": _canonical_hash(updated_hashes),
    }


def _configure(code_root: Path, output_root: Path, model_dir: Path) -> None:
    package = code_root / ".copilot" / "rag" / "gen_db" / "software_rag_tool"
    sys.path.insert(0, str(package))
    os.environ.update(
        {
            "RAG_OUTPUT_ROOT": str(output_root),
            "CHROMA_DIR_V2": str(output_root / "index" / "chroma"),
            "CHROMA_COLLECTION": "perf011_r2",
            "EMBEDDING_BACKEND": "onnx",
            "EMBEDDING_MODEL": "cl-nagoya/ruri-v3-30m",
            "EMBEDDING_DIMENSION": "256",
            "EMBEDDING_QUANTIZATION": "dynamic-int8",
            "EMBEDDING_ONNX_DIR": str(model_dir),
            "LOCAL_RAG_LEXICAL_TOKENIZER": "sudachi",
            "EMBED_BATCH_SIZE": "8",
            "RAG_ONNX_THREADS": "4",
            "OMP_NUM_THREADS": "4",
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )


def _ordered_records(corpus: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from software_rag_tool.embeddings import get_document_token_budget
    from software_rag_tool.records import build_records_for_file, iter_input_files

    budget = get_document_token_budget()
    records: list[dict[str, Any]] = []
    by_file: dict[str, int] = {}
    token_manifest: list[dict[str, Any]] = []
    for path in iter_input_files(corpus):
        relative = path.relative_to(corpus).as_posix()
        built = build_records_for_file(corpus, path, source_id="perf011-r2")
        by_file[relative] = len(built)
        for item in built:
            token_ids = budget.tokenizer(
                budget.document_prefix + str(item["embedding_text"]),
                add_special_tokens=True,
            )["input_ids"]
            token_manifest.append(
                {
                    "path": relative,
                    "source_id": item["metadata"]["source_id"],
                    "id": item["id"],
                    "text": item["text"],
                    "metadata": item["metadata"],
                    "token_count": len(token_ids),
                    "token_ids": token_ids,
                }
            )
        records.extend(built)
    docx_counts = [count for path, count in by_file.items() if path.endswith(".docx")]
    pptx_counts = [count for path, count in by_file.items() if path.endswith(".pptx")]
    if len(docx_counts) != DOCX_COUNT or set(docx_counts) != {DOCX_RECORDS}:
        raise RuntimeError(f"DOCX fixture count mismatch: {docx_counts}")
    if len(pptx_counts) != PPTX_COUNT or set(pptx_counts) != {PPTX_RECORDS}:
        raise RuntimeError(f"PPTX fixture count mismatch: {pptx_counts}")
    if len(records) != TOTAL_RECORDS:
        raise RuntimeError(f"fixture record count mismatch: {len(records)}")
    return records, {
        "record_count": len(records),
        "docx_records": sum(docx_counts),
        "pptx_records": sum(pptx_counts),
        "per_file_counts": by_file,
        "ordered_record_manifest_hash": _canonical_hash(token_manifest),
    }


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _peak_working_set() -> int:
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return 0
    return int(counters.PeakWorkingSetSize)


@contextmanager
def _catalog_trace(
    catalog: Any,
    samples: dict[str, Any],
    *,
    enabled: bool,
) -> Iterator[None]:
    if not enabled:
        yield
        return
    original = catalog.connect

    def traced(path: Path | None = None):
        manager = original(path)

        class Context:
            def __enter__(self):
                connection = manager.__enter__()
                self.connection = connection
                self.before = connection.total_changes
                if enabled:
                    connection.set_trace_callback(samples["sql"].append)
                return connection

            def __exit__(self, *args: Any):
                samples["total_changes"] += self.connection.total_changes - self.before
                if enabled:
                    self.connection.set_trace_callback(None)
                return manager.__exit__(*args)

        return Context()

    catalog.connect = traced
    try:
        yield
    finally:
        catalog.connect = original


def _timed_call(target: Any, name: str, totals: dict[str, float]) -> tuple[Any, Callable[..., Any]]:
    original = getattr(target, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            totals[name] = totals.get(name, 0.0) + time.perf_counter() - started

    setattr(target, name, wrapped)
    return target, original


def _worker(args: argparse.Namespace) -> int:
    code_root = Path(args.code_root).resolve()
    output_root = Path(args.output_root).resolve()
    corpus = Path(args.corpus).resolve()
    model_dir = Path(args.model_dir).resolve()
    _configure(code_root, output_root, model_dir)
    records, manifest = _ordered_records(corpus)
    expected = json.loads(Path(args.expected_manifest).read_text(encoding="utf-8"))
    if manifest["ordered_record_manifest_hash"] != expected["initial"]["ordered_record_manifest_hash"]:
        raise RuntimeError("baseline/candidate record manifest mismatch")
    if args.cohort == "prepare":
        updated_records, updated_manifest = _ordered_records(Path(args.updated_corpus))
        changed_paths = {
            *(f"word/catalog-guide-{index:02d}.docx" for index in UPDATED_DOCX),
            *(f"powerpoint/catalog-plan-{index:02d}.pptx" for index in UPDATED_PPTX),
        }
        changed = [item for item in updated_records if item["metadata"]["path"].split("/", 1)[-1] in changed_paths]
        old_ids = [item["id"] for item in records if item["metadata"]["path"].split("/", 1)[-1] in changed_paths]
        if len(changed) != UPDATED_RECORDS or len(old_ids) != UPDATED_RECORDS:
            raise RuntimeError(f"update record mismatch: new={len(changed)} old={len(old_ids)}")
        Path(args.records).write_text(json.dumps({"initial": records, "updated": changed, "old_ids": old_ids}, ensure_ascii=False), encoding="utf-8")
        Path(args.output).write_text(json.dumps({"initial": manifest, "updated": updated_manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    from software_rag_tool import catalog, incremental, manifest as product_manifest, store

    prepared = json.loads(Path(args.records).read_text(encoding="utf-8"))
    sql: dict[str, Any] = {"sql": [], "total_changes": 0}
    phases: dict[str, float] = {}
    restored: list[tuple[Any, str, Callable[..., Any]]] = []
    catalog_target = store if args.cohort == "core" else incremental
    vector_target = store if args.cohort == "core" else incremental
    for target, name in ((catalog_target, "upsert_catalog_records"), (vector_target, "upsert_records")):
        owner, original = _timed_call(target, name, phases)
        restored.append((owner, name, original))
    if args.cohort == "e2e":
        owner, original = _timed_call(incremental, "build_records_for_file", phases)
        restored.append((owner, "build_records_for_file", original))

    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    try:
        with _catalog_trace(catalog, sql, enabled=args.trace_probe):
            if args.cohort == "core":
                from software_rag_tool.jsonl import write_jsonl
                from software_rag_tool.paths import clean_dir

                write_jsonl(clean_dir() / "perf011-r2.jsonl", prepared["initial"])
                store.build_index(reset=True)
            else:
                incremental.add_or_update_root(
                    corpus,
                    source_id="perf011-r2",
                    reset_db=True,
                    reset_clean=True,
                )
    finally:
        for owner, name, original in restored:
            setattr(owner, name, original)
    cold_wall = time.perf_counter() - wall_start
    cold_cpu = time.process_time() - cpu_start
    cold_phases = dict(phases)

    if args.cohort == "e2e":
        updated_root = Path(args.updated_corpus)
        for relative in (
            *(f"word/catalog-guide-{index:02d}.docx" for index in UPDATED_DOCX),
            *(f"powerpoint/catalog-plan-{index:02d}.pptx" for index in UPDATED_PPTX),
        ):
            shutil.copy2(updated_root / relative, corpus / relative)

    phases.clear()
    restored.clear()
    catalog_target = store if args.cohort == "core" else incremental
    vector_target = store if args.cohort == "core" else incremental
    for target, name in ((catalog_target, "upsert_catalog_records"), (vector_target, "upsert_records")):
        owner, original = _timed_call(target, name, phases)
        restored.append((owner, name, original))
    if args.cohort == "e2e":
        owner, original = _timed_call(incremental, "build_records_for_file", phases)
        restored.append((owner, "build_records_for_file", original))
    update_wall_start = time.perf_counter()
    update_cpu_start = time.process_time()
    try:
        with _catalog_trace(catalog, sql, enabled=args.trace_probe):
            if args.cohort == "core":
                store.delete_ids(prepared["old_ids"])
                store.upsert_records(prepared["updated"])
                catalog_started = time.perf_counter()
                if args.variant == "candidate":
                    catalog.upsert_records(prepared["updated"], delete_ids=prepared["old_ids"])
                else:
                    catalog.delete_chunks(prepared["old_ids"])
                    catalog.upsert_records(prepared["updated"])
                phases["upsert_catalog_records"] = time.perf_counter() - catalog_started
                product_manifest.write_manifest(store.collection_count())
            else:
                incremental.add_or_update_root(corpus, source_id="perf011-r2")
    finally:
        for owner, name, original in restored:
            setattr(owner, name, original)
    update_wall = time.perf_counter() - update_wall_start
    update_cpu = time.process_time() - update_cpu_start

    manifest_payload = product_manifest.read_manifest()
    embedder = store.get_embedder()
    providers = list(embedder._session.get_providers()) if hasattr(embedder, "_session") else []
    collection = store._get_existing_collection()
    chroma_sqlite = output_root / "index" / "chroma" / "chroma.sqlite3"
    with catalog.connect_readonly(catalog.catalog_path()) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        catalog_count = int(connection.execute("SELECT COUNT(*) FROM chunk").fetchone()[0])
    result = {
        "variant": args.variant,
        "cohort": args.cohort,
        "cold_wall_seconds": cold_wall,
        "cold_cpu_seconds": cold_cpu,
        "update_wall_seconds": update_wall,
        "update_cpu_seconds": update_cpu,
        "cold_catalog_seconds": cold_phases.get("upsert_catalog_records", 0.0),
        "cold_embedding_chroma_seconds": cold_phases.get("upsert_records", 0.0),
        "cold_prepare_seconds": cold_phases.get("build_records_for_file", 0.0),
        "update_catalog_seconds": phases.get("upsert_catalog_records", 0.0),
        "update_embedding_chroma_seconds": phases.get("upsert_records", 0.0),
        "update_prepare_seconds": phases.get("build_records_for_file", 0.0),
        "sql_count": len(sql["sql"]),
        "total_changes": sql["total_changes"],
        "peak_rss_bytes": _peak_working_set(),
        "db_bytes": _tree_bytes(output_root / "index"),
        "wal_bytes": sum(path.stat().st_size for path in output_root.rglob("*-wal")),
        "vector_count": store.collection_count(),
        "catalog_count": catalog_count,
        "manifest_record_count": int(manifest_payload.get("record_count", -1)),
        "manifest": manifest_payload,
        "embedder_class": type(embedder).__name__,
        "onnx_providers": providers,
        "collection_name": store.collection_name(),
        "collection_metadata": dict(collection.metadata or {}) if collection is not None else {},
        "chroma_sqlite_bytes": chroma_sqlite.stat().st_size if chroma_sqlite.exists() else 0,
        "integrity": integrity,
        "foreign_key_errors": foreign_keys,
        "record_manifest_hash": manifest["ordered_record_manifest_hash"],
        "trace_probe": bool(args.trace_probe),
    }
    if not (
        result["vector_count"] == TOTAL_RECORDS
        and result["catalog_count"] == TOTAL_RECORDS
        and result["manifest_record_count"] == TOTAL_RECORDS
        and integrity == "ok"
        and foreign_keys == 0
        and type(embedder).__name__ == "OnnxRuntimeEmbedder"
        and "CPUExecutionProvider" in providers
        and chroma_sqlite.is_file()
        and manifest_payload.get("collection") == "perf011_r2"
        and manifest_payload.get("embedding_model") == "cl-nagoya/ruri-v3-30m"
        and manifest_payload.get("embedding_backend") == "onnx"
        and manifest_payload.get("embedding_dimension") == 256
        and manifest_payload.get("quantization") == "dynamic-int8"
        and collection is not None
        and collection.metadata.get("embedding_model") == "cl-nagoya/ruri-v3-30m"
        and collection.metadata.get("embedding_backend") == "onnx"
        and collection.metadata.get("embedding_dimension") == 256
        and collection.metadata.get("quantization") == "dynamic-int8"
    ):
        raise RuntimeError(f"product gold mismatch: {result}")
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def _p95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"runs": len(samples)}
    numeric = [key for key, value in samples[0].items() if isinstance(value, (int, float)) and not isinstance(value, bool)]
    for key in numeric:
        values = [float(sample[key]) for sample in samples]
        result[f"{key}_p50"] = statistics.median(values)
        result[f"{key}_p95"] = _p95(values)
    return result


def _reduction(baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], key: str) -> float:
    ratios = [1.0 - float(c[key]) / float(b[key]) for b, c in zip(baseline, candidate, strict=True)]
    return statistics.median(ratios)


def _run(args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="perf011-r2-") as temporary:
        root = Path(temporary)
        initial = root / "fixture" / "initial" / "source"
        updated = root / "fixture" / "updated" / "source"
        fixture = _generate_corpus(initial, updated)
        reproducible_initial = root / "fixture-repro" / "initial" / "source"
        reproducible_updated = root / "fixture-repro" / "updated" / "source"
        reproduced = _generate_corpus(reproducible_initial, reproducible_updated)
        if (
            fixture["initial_file_manifest_hash"] != reproduced["initial_file_manifest_hash"]
            or fixture["updated_file_manifest_hash"] != reproduced["updated_file_manifest_hash"]
        ):
            raise RuntimeError("Office fixture generator is not byte reproducible")
        fixture["byte_reproducible_second_generation"] = True
        records_path = root / "records.json"
        preflight_path = root / "preflight.json"
        expected_path = root / "expected.json"
        expected_path.write_text(json.dumps({"initial": {"ordered_record_manifest_hash": "pending"}}), encoding="utf-8")
        prepare_cmd = [
            args.python, str(script), "--worker", "--cohort", "prepare",
            "--variant", "candidate", "--code-root", args.candidate_root,
            "--output-root", str(root / "prepare-db"), "--corpus", str(initial),
            "--updated-corpus", str(updated), "--model-dir", args.model_dir,
            "--records", str(records_path), "--expected-manifest", str(expected_path),
            "--output", str(preflight_path),
        ]
        # Prepare first without the expected-hash check, then freeze its manifest.
        expected_path.write_text(json.dumps({"initial": {"ordered_record_manifest_hash": ""}}), encoding="utf-8")
        prepare_cmd.append("--prepare-unchecked")
        subprocess.run(prepare_cmd, check=True, timeout=480)
        preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
        expected_path.write_text(json.dumps(preflight), encoding="utf-8")
        if preflight["initial"]["record_count"] != TOTAL_RECORDS:
            raise RuntimeError("preflight did not produce 400 records")
        if args.preflight_only:
            output.write_text(
                json.dumps({"fixture": fixture, "preflight": preflight}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps({"preflight": preflight}, ensure_ascii=False))
            return 0

        all_results: dict[str, Any] = {"core": {}, "e2e": {}}
        trace_probes: dict[str, Any] = {}
        for cohort in ("core", "e2e"):
            for variant in ("baseline", "candidate"):
                code_root = args.baseline_root if variant == "baseline" else args.candidate_root
                sample_root = root / "trace" / cohort / variant
                corpus = sample_root / "source"
                shutil.copytree(initial, corpus)
                sample_output = sample_root / "sample.json"
                cmd = [
                    args.python, str(script), "--worker", "--trace-probe",
                    "--cohort", cohort, "--variant", variant,
                    "--code-root", code_root, "--output-root", str(sample_root / "db"),
                    "--corpus", str(corpus), "--updated-corpus", str(updated),
                    "--model-dir", args.model_dir, "--records", str(records_path),
                    "--expected-manifest", str(expected_path), "--output", str(sample_output),
                ]
                completed = subprocess.run(cmd, timeout=480, text=True, capture_output=True)
                if completed.returncode:
                    raise RuntimeError(f"trace worker failed: {cmd}\n{completed.stdout}\n{completed.stderr}")
                sample = json.loads(sample_output.read_text(encoding="utf-8"))
                trace_probes[f"{cohort}_{variant}"] = {
                    "sql_count": sample["sql_count"],
                    "total_changes": sample["total_changes"],
                }
        raw: list[dict[str, Any]] = []
        pair_plan = (("core", 1), ("e2e", 1)) if args.smoke else (("core", 5), ("e2e", 3))
        for cohort, measured_pairs in pair_plan:
            samples = {"baseline": [], "candidate": []}
            for pair in range(measured_pairs + 1):
                order = ("baseline", "candidate") if pair % 2 == 0 else ("candidate", "baseline")
                pair_values: dict[str, dict[str, Any]] = {}
                for variant in order:
                    code_root = args.baseline_root if variant == "baseline" else args.candidate_root
                    sample_root = root / "runs" / cohort / f"{pair}-{variant}"
                    corpus = sample_root / "source"
                    shutil.copytree(initial, corpus)
                    sample_output = sample_root / "sample.json"
                    cmd = [
                        args.python, str(script), "--worker", "--cohort", cohort,
                        "--variant", variant, "--code-root", code_root,
                        "--output-root", str(sample_root / "db"), "--corpus", str(corpus),
                        "--updated-corpus", str(updated), "--model-dir", args.model_dir,
                        "--records", str(records_path), "--expected-manifest", str(expected_path),
                        "--output", str(sample_output),
                    ]
                    completed = subprocess.run(cmd, timeout=480, text=True, capture_output=True)
                    if completed.returncode:
                        raise RuntimeError(f"worker failed: {cmd}\n{completed.stdout}\n{completed.stderr}")
                    sample = json.loads(sample_output.read_text(encoding="utf-8"))
                    sample.update({"pair": pair, "warmup": pair == 0})
                    pair_values[variant] = sample
                if pair:
                    for variant in ("baseline", "candidate"):
                        samples[variant].append(pair_values[variant])
                        raw.append(pair_values[variant])
                if time.monotonic() - started > 2700:
                    raise TimeoutError("combined benchmark exceeded 45 minutes")
            baseline = samples["baseline"]
            candidate = samples["candidate"]
            reductions = {
                key: _reduction(baseline, candidate, key)
                for key in ("cold_wall_seconds", "cold_catalog_seconds", "update_wall_seconds", "update_catalog_seconds")
            }
            all_results[cohort] = {
                "baseline": _summary(baseline),
                "candidate": _summary(candidate),
                "paired_median_reductions": reductions,
            }

        core = all_results["core"]
        e2e = all_results["e2e"]
        gates = {
            "core_catalog_at_least_20_percent": core["paired_median_reductions"]["cold_catalog_seconds"] >= 0.20,
            "core_full_add_at_least_10_percent": core["paired_median_reductions"]["cold_wall_seconds"] >= 0.10,
            "office_e2e_not_slower": e2e["paired_median_reductions"]["cold_wall_seconds"] >= 0.0,
            "core_update_at_least_15_percent": core["paired_median_reductions"]["update_wall_seconds"] >= 0.15,
            "cold_p95_within_10_percent": core["candidate"]["cold_wall_seconds_p95"] <= core["baseline"]["cold_wall_seconds_p95"] * 1.10,
            "update_p95_within_10_percent": core["candidate"]["update_wall_seconds_p95"] <= core["baseline"]["update_wall_seconds_p95"] * 1.10,
            "rss_within_15_percent": max(
                core["candidate"]["peak_rss_bytes_p95"] / core["baseline"]["peak_rss_bytes_p95"],
                e2e["candidate"]["peak_rss_bytes_p95"] / e2e["baseline"]["peak_rss_bytes_p95"],
            ) <= 1.15,
            "db_bytes_within_5_percent": max(
                core["candidate"]["db_bytes_p95"] / core["baseline"]["db_bytes_p95"],
                e2e["candidate"]["db_bytes_p95"] / e2e["baseline"]["db_bytes_p95"],
            ) <= 1.05,
        }
        result = {
            "task": "LRR-PERF-011-R2",
            "fixture": fixture,
            "preflight": preflight,
            "python": args.python,
            "model_dir": args.model_dir,
            "baseline_root": args.baseline_root,
            "candidate_root": args.candidate_root,
            "warmup_pairs": 1,
            "core_measured_pairs": pair_plan[0][1],
            "office_e2e_measured_pairs": pair_plan[1][1],
            "cohorts": all_results,
            "gates": gates,
            "trace_probes_excluded_from_timing": trace_probes,
            "formal": not args.smoke,
            "pass": all(gates.values()),
            "raw_samples": raw,
        }
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"pass": result["pass"], "gates": gates}, ensure_ascii=False))
        return 0 if args.smoke or result["pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--prepare-unchecked", action="store_true")
    parser.add_argument("--trace-probe", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--cohort", choices=("prepare", "core", "e2e"))
    parser.add_argument("--variant", choices=("baseline", "candidate"))
    parser.add_argument("--code-root")
    parser.add_argument("--output-root")
    parser.add_argument("--corpus")
    parser.add_argument("--updated-corpus")
    parser.add_argument("--model-dir")
    parser.add_argument("--records")
    parser.add_argument("--expected-manifest")
    parser.add_argument("--output", required=True)
    parser.add_argument("--python")
    parser.add_argument("--baseline-root")
    parser.add_argument("--candidate-root")
    args = parser.parse_args()
    if args.worker:
        if args.prepare_unchecked:
            # Freeze the generated fixture before baseline/candidate comparisons.
            _configure(Path(args.code_root).resolve(), Path(args.output_root).resolve(), Path(args.model_dir).resolve())
            initial, initial_manifest = _ordered_records(Path(args.corpus).resolve())
            updated, updated_manifest = _ordered_records(Path(args.updated_corpus).resolve())
            changed_paths = {
                *(f"word/catalog-guide-{index:02d}.docx" for index in UPDATED_DOCX),
                *(f"powerpoint/catalog-plan-{index:02d}.pptx" for index in UPDATED_PPTX),
            }
            changed = [item for item in updated if item["metadata"]["path"].split("/", 1)[-1] in changed_paths]
            old_ids = [item["id"] for item in initial if item["metadata"]["path"].split("/", 1)[-1] in changed_paths]
            if len(changed) != UPDATED_RECORDS or len(old_ids) != UPDATED_RECORDS:
                raise RuntimeError(f"update record mismatch: {len(changed)}/{len(old_ids)}")
            Path(args.records).write_text(json.dumps({"initial": initial, "updated": changed, "old_ids": old_ids}, ensure_ascii=False), encoding="utf-8")
            Path(args.output).write_text(json.dumps({"initial": initial_manifest, "updated": updated_manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0
        return _worker(args)
    required = (args.python, args.baseline_root, args.candidate_root, args.model_dir)
    if not all(required):
        parser.error("--python, --baseline-root, --candidate-root, and --model-dir are required")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
