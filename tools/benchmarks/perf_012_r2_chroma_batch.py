from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from perf_011_r2_catalog_productization import (
    TOTAL_RECORDS,
    _generate_corpus,
    _ordered_records,
)


COLLECTION = "perf012_r2"
FALSE_ANCHORS = ("RFC999999", "catalog-guide-99.docx")
QUALITY_QUERIES = ("RFC110000", "catalog office update alpha")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _configure(code_root: Path, output_root: Path, model_dir: Path, collection: str) -> None:
    package = code_root / ".copilot" / "rag" / "gen_db" / "software_rag_tool"
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))
    os.environ.update(
        {
            "RAG_OUTPUT_ROOT": str(output_root),
            "CHROMA_DIR_V2": str(output_root / "index" / "chroma"),
            "CHROMA_COLLECTION": collection,
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
    os.environ.pop("EMBED_WRITE_BATCH_SIZE", None)


def _working_set() -> int:
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
        wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    return int(counters.WorkingSetSize)


class RssSampler:
    def __init__(self) -> None:
        self.baseline = 0
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RssSampler":
        self.baseline = self.peak = _working_set()

        def sample() -> None:
            while not self._stop.wait(0.005):
                self.peak = max(self.peak, _working_set())

        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.peak = max(self.peak, _working_set())
        self._stop.set()
        assert self._thread is not None
        self._thread.join(timeout=2)


class CollectionProxy:
    def __init__(self, collection: Any, calls: list[int]) -> None:
        self._collection = collection
        self._calls = calls

    def upsert(self, *, ids: Any, **kwargs: Any) -> Any:
        self._calls.append(len(ids))
        return self._collection.upsert(ids=ids, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collection, name)


def _instrument_chroma(store: Any) -> tuple[list[int], Callable[[], None]]:
    calls: list[int] = []
    original = store._get_or_create_collection

    def get_collection() -> CollectionProxy:
        return CollectionProxy(original(), calls)

    store._get_or_create_collection = get_collection

    def restore() -> None:
        store._get_or_create_collection = original

    return calls, restore


def _time_function(owner: Any, name: str, totals: dict[str, float]) -> Callable[[], None]:
    original = getattr(owner, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            totals[name] = totals.get(name, 0.0) + time.perf_counter() - started

    setattr(owner, name, wrapped)
    return lambda: setattr(owner, name, original)


def _clear_chroma_cache() -> None:
    try:
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except (ImportError, AttributeError):
        pass


def _vector_gold(store: Any) -> tuple[dict[str, Any], list[list[float]]]:
    collection = store._get_existing_collection()
    if collection is None:
        raise RuntimeError("measured Chroma collection is missing")
    payload = collection.get(include=["documents", "metadatas", "embeddings"])
    ids = [str(value) for value in payload.get("ids") or []]
    documents = list(payload.get("documents") or [])
    metadatas = list(payload.get("metadatas") or [])
    embeddings = payload.get("embeddings")
    vectors = embeddings.tolist() if hasattr(embeddings, "tolist") else list(embeddings or [])
    if not (len(ids) == len(documents) == len(metadatas) == len(vectors) == TOTAL_RECORDS):
        raise RuntimeError("Chroma result alignment mismatch")
    rows = sorted(zip(ids, documents, metadatas, vectors, strict=True), key=lambda item: item[0])
    identity = [{"id": rid, "document": doc, "metadata": meta} for rid, doc, meta, _ in rows]
    ordered_vectors = [[float(value) for value in vector] for _, _, _, vector in rows]
    return {
        "identity_hash": _canonical_hash(identity),
        "vector_hash": _canonical_hash(ordered_vectors),
        "ids": [row[0] for row in rows],
    }, ordered_vectors


def _quality_gold(catalog: Any, store: Any) -> dict[str, Any]:
    false_anchors = []
    for query in FALSE_ANCHORS:
        exact_rows = catalog.exact_search(query, top_k=1000, source="any")
        hybrid_rows = store.query(query, top_k=5, source="any", explain=True)
        exact_signals = sum(1 for row in hybrid_rows if "exact" in (row.get("signals") or []))
        false_anchors.append(
            {
                "query": query,
                "exact_candidate_count": len(exact_rows),
                "exact_signal_count": exact_signals,
                "negative_exact_pass": len(exact_rows) == 0 and exact_signals == 0,
            }
        )
    retrieval = []
    for query in QUALITY_QUERIES:
        rows = store.query(query, top_k=5, source="any", explain=True)
        retrieval.append(
            {
                "query": query,
                "ids": [str(row.get("id")) for row in rows],
                "signals": [list(row.get("signals") or []) for row in rows],
            }
        )
    return {"false_anchors": false_anchors, "retrieval": retrieval}


def _worker(args: argparse.Namespace) -> int:
    code_root = Path(args.code_root).resolve()
    output_root = Path(args.output_root).resolve()
    model_dir = Path(args.model_dir).resolve()
    records = json.loads(Path(args.records).read_text(encoding="utf-8"))
    if len(records) != TOTAL_RECORDS:
        raise RuntimeError(f"expected {TOTAL_RECORDS} records, got {len(records)}")
    warm_root = output_root.parent / "warmup"
    _configure(code_root, warm_root, model_dir, f"{COLLECTION}_warmup")
    from software_rag_tool import catalog, incremental, manifest as product_manifest, store

    store.upsert_records(records[:8])
    _clear_chroma_cache()
    _configure(code_root, output_root, model_dir, COLLECTION)
    write_calls, restore_collection = _instrument_chroma(store)
    phases: dict[str, float] = {}
    restore_timing = _time_function(
        store if args.cohort == "core" else incremental,
        "upsert_records",
        phases,
    )
    if args.cohort == "e2e":
        corpus = Path(args.corpus).resolve()
    cpu_started = time.process_time()
    wall_started = time.perf_counter()
    try:
        with RssSampler() as rss:
            if args.cohort == "core":
                from software_rag_tool.jsonl import write_jsonl
                from software_rag_tool.paths import clean_dir

                write_jsonl(clean_dir() / "perf012-r2.jsonl", records)
                store.build_index(reset=True)
            else:
                incremental.add_or_update_root(
                    corpus,
                    source_id="perf012-r2",
                    reset_db=True,
                    reset_clean=True,
                )
        wall_seconds = time.perf_counter() - wall_started
        cpu_seconds = time.process_time() - cpu_started
    finally:
        restore_timing()
        restore_collection()
    manifest = product_manifest.read_manifest()
    vector_gold, vectors = _vector_gold(store)
    quality = _quality_gold(catalog, store)
    embedder = store.get_embedder()
    providers = list(embedder._session.get_providers()) if hasattr(embedder, "_session") else []
    collection = store._get_existing_collection()
    if collection is None:
        raise RuntimeError("collection disappeared after measured ADD")
    expected_manifest = {
        "record_count": TOTAL_RECORDS,
        "collection": COLLECTION,
        "embedding_model": "cl-nagoya/ruri-v3-30m",
        "embedding_backend": "onnx",
        "embedding_dimension": 256,
        "quantization": "dynamic-int8",
    }
    manifest_ok = all(manifest.get(key) == value for key, value in expected_manifest.items())
    metadata_ok = all(
        collection.metadata.get(key) == value
        for key, value in expected_manifest.items()
        if key not in {"record_count", "collection"}
    )
    result = {
        "variant": args.variant,
        "cohort": args.cohort,
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "vector_phase_seconds": phases.get("upsert_records", 0.0),
        "rss_baseline_bytes": rss.baseline,
        "rss_peak_bytes": rss.peak,
        "rss_delta_bytes": max(0, rss.peak - rss.baseline),
        "db_bytes": _tree_bytes(output_root / "index"),
        "write_calls": len(write_calls),
        "write_batch_sizes": write_calls,
        "manifest": manifest,
        "manifest_ok": manifest_ok,
        "collection_metadata": dict(collection.metadata or {}),
        "collection_metadata_ok": metadata_ok,
        "embedder_class": type(embedder).__name__,
        "onnx_providers": providers,
        "vector_gold": vector_gold,
        "quality": quality,
        "false_anchor_count": sum(
            not item["negative_exact_pass"] for item in quality["false_anchors"]
        ),
    }
    if not (
        manifest_ok
        and metadata_ok
        and type(embedder).__name__ == "OnnxRuntimeEmbedder"
        and "CPUExecutionProvider" in providers
        and len(vector_gold["ids"]) == TOTAL_RECORDS
        and result["false_anchor_count"] == 0
        and len(write_calls) > 0
    ):
        raise RuntimeError(f"product gold mismatch: {result}")
    Path(args.vectors).write_text(json.dumps(vectors, separators=(",", ":")), encoding="utf-8")
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def _p95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"runs": len(samples)}
    for key in (
        "wall_seconds", "cpu_seconds", "vector_phase_seconds", "rss_peak_bytes",
        "rss_delta_bytes", "db_bytes", "write_calls",
    ):
        values = [float(sample[key]) for sample in samples]
        result[f"{key}_p50"] = statistics.median(values)
        result[f"{key}_p95"] = _p95(values)
    return result


def _paired_reduction(
    baseline: list[dict[str, Any]], candidate: list[dict[str, Any]], key: str
) -> float:
    return statistics.median(
        1.0 - float(c[key]) / float(b[key])
        for b, c in zip(baseline, candidate, strict=True)
    )


def _compare_vectors(baseline_path: Path, candidate_path: Path) -> dict[str, float]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if len(baseline) != len(candidate):
        raise RuntimeError("vector row count differs")
    minimum_cosine = 1.0
    maximum_absolute = 0.0
    for left, right in zip(baseline, candidate, strict=True):
        if len(left) != len(right) or not all(math.isfinite(value) for value in [*left, *right]):
            raise RuntimeError("invalid vector dimension or non-finite value")
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        minimum_cosine = min(minimum_cosine, dot / (left_norm * right_norm))
        maximum_absolute = max(
            maximum_absolute,
            max(abs(a - b) for a, b in zip(left, right, strict=True)),
        )
    return {"minimum_cosine": minimum_cosine, "maximum_absolute_difference": maximum_absolute}


def _git(root: str, *args: str) -> str:
    return subprocess.check_output(["git", "-C", root, *args], text=True).strip()


def _run(args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="perf012-r2-") as temporary:
        root = Path(temporary)
        initial = root / "fixture" / "initial" / "source"
        updated = root / "fixture" / "updated" / "source"
        fixture = _generate_corpus(initial, updated)
        reproduced_initial = root / "fixture-repro" / "initial" / "source"
        reproduced_updated = root / "fixture-repro" / "updated" / "source"
        reproduced = _generate_corpus(reproduced_initial, reproduced_updated)
        fixture["byte_reproducible_second_generation"] = (
            fixture["initial_file_manifest_hash"] == reproduced["initial_file_manifest_hash"]
            and fixture["updated_file_manifest_hash"] == reproduced["updated_file_manifest_hash"]
        )
        if not fixture["byte_reproducible_second_generation"]:
            raise RuntimeError("Office fixture is not byte reproducible")
        package = Path(args.candidate_root) / ".copilot" / "rag" / "gen_db" / "software_rag_tool"
        sys.path.insert(0, str(package))
        _configure(Path(args.candidate_root), root / "prepare", Path(args.model_dir), "prepare")
        records, record_manifest = _ordered_records(initial)
        records_path = root / "records.json"
        records_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        if args.preflight_only:
            output.write_text(
                json.dumps({"fixture": fixture, "record_manifest": record_manifest}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return 0
        raw: list[dict[str, Any]] = []
        cohorts: dict[str, Any] = {}
        vector_comparisons: list[dict[str, Any]] = []
        plan = ("baseline", "candidate"), ("candidate", "baseline"), ("baseline", "candidate")
        for cohort in ("core", "e2e"):
            samples: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
            for pair, order in enumerate(plan, start=1):
                pair_paths: dict[str, Path] = {}
                pair_values: dict[str, dict[str, Any]] = {}
                for variant in order:
                    code_root = args.baseline_root if variant == "baseline" else args.candidate_root
                    sample_root = root / "runs" / cohort / f"{pair}-{variant}"
                    corpus = sample_root / "source"
                    shutil.copytree(initial, corpus)
                    sample_output = sample_root / "sample.json"
                    vectors_output = sample_root / "vectors.json"
                    command = [
                        args.python, str(script), "--worker", "--cohort", cohort,
                        "--variant", variant, "--code-root", code_root,
                        "--output-root", str(sample_root / "db"), "--corpus", str(corpus),
                        "--model-dir", args.model_dir, "--records", str(records_path),
                        "--vectors", str(vectors_output), "--output", str(sample_output),
                    ]
                    remaining = 2700.0 - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("combined benchmark exceeded 45 minutes")
                    completed = subprocess.run(
                        command,
                        timeout=min(480.0, remaining),
                        text=True,
                        capture_output=True,
                    )
                    if completed.returncode:
                        raise RuntimeError(
                            f"worker failed ({completed.returncode}): {command}\n"
                            f"stdout={completed.stdout}\nstderr={completed.stderr}"
                        )
                    sample = json.loads(sample_output.read_text(encoding="utf-8"))
                    sample.update({"pair": pair, "command": command})
                    samples[variant].append(sample)
                    pair_values[variant] = sample
                    pair_paths[variant] = vectors_output
                    raw.append(sample)
                if pair_values["baseline"]["vector_gold"]["identity_hash"] != pair_values["candidate"]["vector_gold"]["identity_hash"]:
                    raise RuntimeError("baseline/candidate ID, document, or metadata differs")
                if pair_values["baseline"]["quality"] != pair_values["candidate"]["quality"]:
                    raise RuntimeError("frozen retrieval quality differs")
                comparison = _compare_vectors(pair_paths["baseline"], pair_paths["candidate"])
                comparison.update({"cohort": cohort, "pair": pair})
                vector_comparisons.append(comparison)
                if time.monotonic() - started > 2700:
                    raise TimeoutError("combined benchmark exceeded 45 minutes")
            baseline = samples["baseline"]
            candidate = samples["candidate"]
            cohorts[cohort] = {
                "baseline": _summary(baseline),
                "candidate": _summary(candidate),
                "paired_median_reductions": {
                    key: _paired_reduction(baseline, candidate, key)
                    for key in ("wall_seconds", "vector_phase_seconds")
                },
            }
        core, e2e = cohorts["core"], cohorts["e2e"]
        gates = {
            "core_full_add_at_least_15_percent": core["paired_median_reductions"]["wall_seconds"] >= 0.15,
            "office_e2e_not_slower": e2e["paired_median_reductions"]["wall_seconds"] >= 0.0,
            "p95_within_10_percent": max(
                core["candidate"]["wall_seconds_p95"] / core["baseline"]["wall_seconds_p95"],
                e2e["candidate"]["wall_seconds_p95"] / e2e["baseline"]["wall_seconds_p95"],
            ) <= 1.10,
            "rss_within_15_percent": max(
                core["candidate"]["rss_peak_bytes_p95"] / core["baseline"]["rss_peak_bytes_p95"],
                e2e["candidate"]["rss_peak_bytes_p95"] / e2e["baseline"]["rss_peak_bytes_p95"],
            ) <= 1.15,
            "db_bytes_within_5_percent": max(
                core["candidate"]["db_bytes_p95"] / core["baseline"]["db_bytes_p95"],
                e2e["candidate"]["db_bytes_p95"] / e2e["baseline"]["db_bytes_p95"],
            ) <= 1.05,
            "core_write_calls_50_to_at_most_4": (
                core["baseline"]["write_calls_p50"] == 50
                and core["candidate"]["write_calls_p50"] <= 4
            ),
            "vectors_equivalent": all(
                item["minimum_cosine"] >= 0.999999
                and item["maximum_absolute_difference"] <= 1e-6
                for item in vector_comparisons
            ),
            "all_product_and_quality_gold": all(
                sample["manifest_ok"]
                and sample["collection_metadata_ok"]
                and sample["false_anchor_count"] == 0
                for sample in raw
            ),
        }
        baseline_commit = _git(args.baseline_root, "rev-parse", "HEAD")
        candidate_commit = _git(args.candidate_root, "rev-parse", "HEAD")
        result = {
            "task": "LRR-PERF-012-R2",
            "formal": True,
            "fixture": fixture,
            "record_manifest": record_manifest,
            "manifest": {
                "seed": fixture["seed"],
                "generator_sha256": _sha256(Path(__file__).with_name("perf_011_r2_catalog_productization.py")),
                "driver_sha256": _sha256(script),
                "model_sha256": _sha256(Path(args.model_dir) / "model.onnx"),
                "python": args.python,
                "python_version": subprocess.check_output([args.python, "--version"], text=True).strip(),
                "baseline_commit": baseline_commit,
                "candidate_commit": candidate_commit,
                "candidate_diff_sha256": hashlib.sha256(
                    subprocess.check_output(
                        [
                            "git", "-C", args.candidate_root, "diff", "--binary",
                            baseline_commit, candidate_commit,
                        ]
                    )
                ).hexdigest(),
                "pair_order": [list(value) for value in plan],
            },
            "cohorts": cohorts,
            "vector_comparisons": vector_comparisons,
            "gates": gates,
            "pass": all(gates.values()),
            "total_elapsed_seconds": time.monotonic() - started,
            "raw_samples": raw,
        }
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"pass": result["pass"], "gates": gates}, ensure_ascii=False))
        return 0 if result["pass"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--cohort", choices=("core", "e2e"))
    parser.add_argument("--variant", choices=("baseline", "candidate"))
    parser.add_argument("--code-root")
    parser.add_argument("--output-root")
    parser.add_argument("--corpus")
    parser.add_argument("--model-dir")
    parser.add_argument("--records")
    parser.add_argument("--vectors")
    parser.add_argument("--output", required=True)
    parser.add_argument("--python")
    parser.add_argument("--baseline-root")
    parser.add_argument("--candidate-root")
    args = parser.parse_args()
    if args.worker:
        return _worker(args)
    required = (args.python, args.baseline_root, args.candidate_root, args.model_dir)
    if not all(required):
        parser.error("--python, --baseline-root, --candidate-root, and --model-dir are required")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
