from __future__ import annotations

import argparse
import inspect
import json
import math
import multiprocessing
import os
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable


RAG_ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool import catalog, store
from software_rag_tool.document_extensions import install_document_extension_runtime
from software_rag_tool.embeddings import get_document_token_budget
from software_rag_tool.extractors import (
    SUPPORTED_EXTENSIONS,
    _extract_xlsx,
    extract_document,
)
from software_rag_tool.ingestion_workers import (
    DoclingExtractionPool,
    choose_worker_plan,
)
from software_rag_tool.pipeline import build_pipeline_contract
from software_rag_tool.records import (
    build_records_for_file,
    chunker_config,
    iter_input_files,
)
from software_rag_tool.retrieval import hybrid_query
from software_rag_tool.structured_extraction import (
    ExtractionResult,
    StructureBlock,
    extract_document_structure,
    extract_legacy_office_structure,
    extract_native_markdown,
    extract_plain_text,
)


CONDITIONS = ("legacy", "native_markdown", "docling")
OFFICE_EXTENSIONS = frozenset({".pdf", ".docx", ".pptx"})
LEGACY_OFFICE_EXTENSIONS = frozenset({".doc", ".ppt"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--issues-catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument(
        "--worker-counts",
        type=int,
        nargs="+",
        default=[1, 2, 3, 4],
    )
    parser.add_argument("--worker-repeats", type=int, default=3)
    parser.add_argument("--worker-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--workers", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()

    _configure_environment(args.model_dir)
    install_document_extension_runtime()
    if args.worker_child:
        print(
            json.dumps(
                _run_worker_child(args.corpus, args.workers),
                ensure_ascii=False,
            )
        )
        return

    args.output.mkdir(parents=True, exist_ok=True)
    gold = _load_jsonl(args.gold)
    issue_root = args.output / "_evaluation_inputs" / "fizzbuzz-issues"
    _materialize_issues(args.issues_catalog, issue_root)
    roots = (args.corpus.resolve(), issue_root.resolve())
    conditions: dict[str, Any] = {}
    for condition in CONDITIONS:
        conditions[condition] = _evaluate_condition(
            condition,
            roots=roots,
            gold=gold,
            output=args.output / condition,
        )

    docling_output = args.output / "docling"
    _activate_index_environment(docling_output, "phase2_docling")
    small_to_big = _score_queries(gold, small_to_big=True)
    conditions["docling"]["small_to_big"] = small_to_big
    conditions["docling"]["small_to_big_default_enabled"] = bool(
        inspect.signature(hybrid_query)
        .parameters["small_to_big"]
        .default
    )
    conditions["native_markdown"]["paired_vs_legacy"] = _paired_comparison(
        conditions["legacy"]["retrieval"],
        conditions["native_markdown"]["retrieval"],
    )
    conditions["docling"]["paired_vs_legacy"] = _paired_comparison(
        conditions["legacy"]["retrieval"],
        small_to_big,
    )

    worker_benchmark = [
        _run_worker_series(
            corpus=args.corpus,
            workers=workers,
            model_dir=args.model_dir,
            repeats=args.worker_repeats,
        )
        for workers in args.worker_counts
    ]
    report = {
        "schema": "local-rag.phase2-structured-eval.v1",
        "corpus": str(args.corpus.resolve()),
        "corpus_commit": _git_revision(args.corpus),
        "gold": str(args.gold.resolve()),
        "gold_cases": len(gold),
        "model_dir": str(args.model_dir.resolve()),
        "conditions": conditions,
        "worker_benchmark": worker_benchmark,
        "runtime": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "logical_cpus": os.cpu_count(),
            "docling_start_method": "spawn",
            "parent_only_store_writes": True,
        },
    }
    output_path = args.output / "phase2-structured-eval.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"report": str(output_path)}, ensure_ascii=False))


def _configure_environment(model_dir: Path) -> None:
    os.environ["EMBEDDING_ONNX_DIR"] = str(model_dir.resolve())
    os.environ["EMBEDDING_BACKEND"] = "onnx-int8"
    os.environ["EMBEDDING_MODEL"] = "cl-nagoya/ruri-v3-30m"
    os.environ["EMBED_BATCH_SIZE"] = "32"
    os.environ["RAG_ONNX_THREADS"] = "4"
    os.environ["OMP_NUM_THREADS"] = "4"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.pop("LOCAL_RAG_LEXICAL_TOKENIZER", None)


def _evaluate_condition(
    condition: str,
    *,
    roots: tuple[Path, ...],
    gold: list[dict[str, Any]],
    output: Path,
) -> dict[str, Any]:
    _activate_index_environment(output, f"phase2_{condition}")
    store.reset_collection()
    catalog.reset_catalog()
    token_budget = get_document_token_budget()
    config = chunker_config(
        chunk_max_chars=1400,
        chunk_overlap=160,
        document_token_budget=token_budget,
    )
    pipeline = build_pipeline_contract(
        chunker=config,
        backend_policy=f"phase2-eval-{condition}",
    )
    records: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    backends: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    format_seconds: Counter[str] = Counter()
    format_files: Counter[str] = Counter()
    started = time.perf_counter()
    for root in roots:
        for path in iter_input_files(root):
            extraction_started = time.perf_counter()
            result = _extract_condition(path, condition)
            extension = path.suffix.lower().lstrip(".") or "unknown"
            format_seconds[extension] += (
                time.perf_counter() - extraction_started
            )
            format_files[extension] += 1
            statuses[result.status] += 1
            origins[result.structure_origin] += 1
            backends[result.backend] += 1
            if not result.is_indexed:
                failures.append(
                    {
                        "path": str(path.relative_to(root)),
                        "status": result.status,
                        "reason": result.reason,
                    }
                )
                continue
            try:
                built = build_records_for_file(
                    root,
                    path,
                    source_id=("issues" if path.parts[-2:-1] == ("issues",) else "corpus"),
                    document_token_budget=token_budget,
                    extraction_result=result,
                    pipeline_contract=pipeline,
                )
            except Exception as exc:
                statuses["record_error"] += 1
                failures.append(
                    {
                        "path": str(path.relative_to(root)),
                        "status": "record_error",
                        "reason": f"{type(exc).__name__}:{exc}",
                    }
                )
                continue
            records.extend(built)
    extraction_seconds = time.perf_counter() - started

    index_started = time.perf_counter()
    for start in range(0, len(records), 64):
        batch = records[start : start + 64]
        store.upsert_records(batch)
        catalog.upsert_records(batch)
    index_seconds = time.perf_counter() - index_started
    scores = _score_queries(gold, small_to_big=False)
    retention = _anchor_retention(gold, records)
    return {
        "files": sum(statuses.values()),
        "records": len(records),
        "statuses": dict(sorted(statuses.items())),
        "structure_origins": dict(sorted(origins.items())),
        "backends": dict(sorted(backends.items())),
        "failures": failures[:25],
        "extraction_seconds": round(extraction_seconds, 3),
        "format_extraction": {
            extension: {
                "files": format_files[extension],
                "seconds": round(format_seconds[extension], 4),
                "mean_seconds": round(
                    format_seconds[extension]
                    / max(1, format_files[extension]),
                    5,
                ),
            }
            for extension in sorted(format_files)
        },
        "index_seconds": round(index_seconds, 3),
        "index_bytes": _directory_size(output),
        "anchor_retention": retention,
        "retrieval": scores,
        "pipeline_fingerprint": pipeline["fingerprint"],
    }


def _activate_index_environment(output: Path, collection: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    os.environ["RAG_OUTPUT_ROOT"] = str(output.resolve())
    os.environ["RAG_DB_NAME"] = collection
    os.environ["CHROMA_COLLECTION"] = collection
    os.environ.pop("CHROMA_DIR_V2", None)


def _extract_condition(path: Path, condition: str) -> ExtractionResult:
    extension = path.suffix.lower()
    if condition == "docling":
        return extract_document(path, backend_policy="auto", docling_threads=4)
    if condition == "native_markdown" and extension == ".md":
        return extract_native_markdown(path)
    return _legacy_flat_extract(path)


def _legacy_flat_extract(path: Path) -> ExtractionResult:
    extension = path.suffix.lower()
    if extension in OFFICE_EXTENSIONS:
        return extract_legacy_office_structure(path)
    if extension in LEGACY_OFFICE_EXTENSIONS:
        return extract_document(path, backend_policy="legacy")
    if extension == ".xlsx":
        try:
            sections = _extract_xlsx(
                path,
                chunk_max_chars=10_000_000,
                chunk_overlap=0,
            )
        except Exception as exc:
            return ExtractionResult(
                status="extract_error",
                backend="legacy-openpyxl",
                backend_version="phase1",
                source_format="xlsx",
                structure_origin="legacy_flat",
                retryable=True,
                reason=f"legacy_xlsx_{type(exc).__name__}",
            )
        blocks = tuple(
            StructureBlock(
                title=section.title,
                text=section.text,
                kind="sheet",
                structure_id=f"legacy-xlsx-{index:04d}",
                parent_section_id=f"legacy-xlsx-{index:04d}",
                sheet=section.title,
                preserve_layout=True,
            )
            for index, section in enumerate(sections, start=1)
            if section.text.strip()
        )
        return ExtractionResult(
            status="indexed" if blocks else "zero_text",
            backend="legacy-openpyxl",
            backend_version="phase1",
            source_format="xlsx",
            structure_origin="legacy_flat",
            blocks=blocks,
            retryable=not blocks,
            reason="" if blocks else "empty_workbook",
        )
    result = extract_plain_text(path)
    if not result.is_indexed:
        return result
    block = StructureBlock(
        title=path.name,
        text="\n".join(value.text for value in result.blocks),
        kind="document",
        structure_id="legacy-flat-0001",
        parent_section_id="legacy-flat-0001",
        preserve_layout=result.blocks[0].preserve_layout,
    )
    return replace(
        result,
        structure_origin="legacy_flat",
        blocks=(block,),
    )


def _score_queries(
    gold: list[dict[str, Any]],
    *,
    small_to_big: bool,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    query_timings: list[float] = []
    started = time.perf_counter()
    for case in gold:
        query_started = time.perf_counter()
        emitted_results = hybrid_query(
            str(case["query_text"]),
            top_k=8,
            budget_tokens=1200,
            small_to_big=small_to_big,
        )
        query_timings.append(time.perf_counter() - query_started)
        candidate_results = hybrid_query(
            str(case["query_text"]),
            top_k=50,
            budget_tokens=None,
            small_to_big=False,
        )
        dense_results = hybrid_query(
            str(case["query_text"]),
            top_k=50,
            budget_tokens=None,
            use_dense=True,
            use_lexical=False,
            small_to_big=False,
        )
        row = _score_case(case, emitted_results)
        row["candidate_recall_at_30"] = _group_recall(
            case,
            candidate_results[:30],
        )
        row["candidate_recall_at_50"] = _group_recall(
            case,
            candidate_results[:50],
        )
        row["dense_recall_at_30"] = _group_recall(
            case,
            dense_results[:30],
        )
        row["dense_recall_at_50"] = _group_recall(
            case,
            dense_results[:50],
        )
        rows.append(row)
    elapsed = time.perf_counter() - started
    answerable = [row for row in rows if row["answerable"]]
    exact_rows = [
        row for row in answerable if "-EX-" in str(row["query_id"])
    ]
    return {
        "recall_at_8": round(_mean(row["recall_at_8"] for row in answerable), 4),
        "candidate_recall_at_30": round(
            _mean(row["candidate_recall_at_30"] for row in answerable),
            4,
        ),
        "candidate_recall_at_50": round(
            _mean(row["candidate_recall_at_50"] for row in answerable),
            4,
        ),
        "dense_recall_at_30": round(
            _mean(row["dense_recall_at_30"] for row in answerable),
            4,
        ),
        "dense_recall_at_50": round(
            _mean(row["dense_recall_at_50"] for row in answerable),
            4,
        ),
        "exact_must_hit_at_8": round(
            _mean(row["recall_at_8"] for row in exact_rows),
            4,
        ),
        "emitted_evidence_hit_at_8": round(
            _mean(float(row["recall_at_8"] > 0) for row in answerable),
            4,
        ),
        "mrr_at_8": round(_mean(row["reciprocal_rank"] for row in answerable), 4),
        "ndcg_at_8": round(_mean(row["ndcg_at_8"] for row in answerable), 4),
        "answerable_cases": len(answerable),
        "query_seconds": round(elapsed, 3),
        "mean_query_seconds": round(elapsed / max(1, len(rows)), 4),
        "emitted_query_p50_seconds": round(
            _percentile(query_timings, 0.50),
            4,
        ),
        "emitted_query_p95_seconds": round(
            _percentile(query_timings, 0.95),
            4,
        ),
        "cases": rows,
    }


def _score_case(
    case: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    groups = list(case.get("required_evidence_groups") or [])
    anchors = {
        str(anchor.get("anchor_id")): anchor
        for anchor in case.get("evidence_anchor") or []
    }
    group_hits: list[bool] = []
    relevant_ranks: list[int] = []
    relevance: list[int] = []
    credited_groups: set[int] = set()
    for rank, row in enumerate(results, start=1):
        hit = any(_row_matches_anchor(row, anchor) for anchor in anchors.values())
        newly_hit_groups = {
            index
            for index, group in enumerate(groups)
            if index not in credited_groups
            and any(
                anchors.get(str(value.get("anchor_id"))) is not None
                and _row_matches_anchor(
                    row,
                    anchors[str(value.get("anchor_id"))],
                )
                for value in group.get("alternatives") or []
            )
        }
        relevance.append(1 if newly_hit_groups else 0)
        credited_groups.update(newly_hit_groups)
        if hit:
            relevant_ranks.append(rank)
    for group in groups:
        alternatives = [
            anchors.get(str(value.get("anchor_id")))
            for value in group.get("alternatives") or []
        ]
        group_hits.append(
            any(
                anchor is not None
                and any(_row_matches_anchor(row, anchor) for row in results)
                for anchor in alternatives
            )
        )
    answerable = str(case.get("answerability")) == "answerable"
    recall = (
        sum(group_hits) / len(group_hits)
        if group_hits
        else (0.0 if answerable else 1.0)
    )
    dcg = sum(value / math.log2(index + 2) for index, value in enumerate(relevance))
    ideal_count = min(len(groups), len(relevance))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
    return {
        "query_id": case.get("query_id"),
        "answerable": answerable,
        "recall_at_8": recall,
        "reciprocal_rank": 1.0 / relevant_ranks[0] if relevant_ranks else 0.0,
        "ndcg_at_8": dcg / ideal if ideal else (0.0 if answerable else 1.0),
        "group_hits": group_hits,
        "relevant_ranks": relevant_ranks,
        "top_paths": [
            str((row.get("metadata") or {}).get("path") or "")
            for row in results[:3]
        ],
    }


def _row_matches_anchor(row: dict[str, Any], anchor: dict[str, Any]) -> bool:
    metadata = row.get("metadata") or {}
    actual_path = str(metadata.get("path") or "").replace("\\", "/").casefold()
    expected_path = str(anchor.get("source_relpath") or "").replace("\\", "/").casefold()
    if expected_path and not (
        actual_path == expected_path or actual_path.endswith("/" + expected_path)
    ):
        return False
    haystack = "\n".join(
        str(row.get(key) or "")
        for key in (
            "text",
            "context_before",
            "context_after",
            "parent_section_context",
            "matched_excerpt",
        )
    )
    return _normalize_span(str(anchor.get("span_text") or "")) in _normalize_span(haystack)


def _group_recall(
    case: dict[str, Any],
    results: list[dict[str, Any]],
) -> float:
    groups = list(case.get("required_evidence_groups") or [])
    anchors = {
        str(anchor.get("anchor_id")): anchor
        for anchor in case.get("evidence_anchor") or []
    }
    if not groups:
        return 0.0 if str(case.get("answerability")) == "answerable" else 1.0
    hits = 0
    for group in groups:
        alternatives = [
            anchors.get(str(value.get("anchor_id")))
            for value in group.get("alternatives") or []
        ]
        if any(
            anchor is not None
            and any(_row_matches_anchor(row, anchor) for row in results)
            for anchor in alternatives
        ):
            hits += 1
    return hits / len(groups)


def _anchor_retention(
    gold: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    anchors = [
        anchor
        for case in gold
        for anchor in case.get("evidence_anchor") or []
    ]
    retained = [
        anchor
        for anchor in anchors
        if any(_row_matches_anchor(record, anchor) for record in records)
    ]
    unique = {
        (
            str(anchor.get("source_relpath")),
            str(anchor.get("anchor_id")),
            str(anchor.get("span_text")),
        )
        for anchor in anchors
    }
    retained_unique = {
        (
            str(anchor.get("source_relpath")),
            str(anchor.get("anchor_id")),
            str(anchor.get("span_text")),
        )
        for anchor in retained
    }
    return {
        "occurrences": len(retained),
        "occurrences_total": len(anchors),
        "unique": len(retained_unique),
        "unique_total": len(unique),
        "ratio": round(len(retained_unique) / max(1, len(unique)), 4),
    }


def _run_worker_benchmark(
    *,
    corpus: Path,
    workers: int,
    model_dir: Path,
) -> dict[str, Any]:
    try:
        import psutil
    except ModuleNotFoundError:
        psutil = None
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--corpus",
        str(corpus.resolve()),
        "--gold",
        str(Path(__file__).resolve()),
        "--issues-catalog",
        str(Path(__file__).resolve()),
        "--output",
        tempfile.gettempdir(),
        "--model-dir",
        str(model_dir.resolve()),
        "--worker-child",
        "--workers",
        str(workers),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )
    peak_rss = 0
    while process.poll() is None:
        if psutil is not None:
            try:
                root = psutil.Process(process.pid)
                family = [root, *root.children(recursive=True)]
                peak_rss = max(
                    peak_rss,
                    sum(value.memory_info().rss for value in family),
                )
            except psutil.Error:
                pass
        time.sleep(0.05)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            f"worker benchmark {workers} failed: {stderr[-2000:]}"
        )
    payload = json.loads(stdout.splitlines()[-1])
    payload["peak_rss_bytes"] = max(
        peak_rss,
        int(payload.get("resource_peak_rss_bytes") or 0),
    )
    payload["stderr_tail"] = stderr[-1000:]
    return payload


def _run_worker_series(
    *,
    corpus: Path,
    workers: int,
    model_dir: Path,
    repeats: int,
) -> dict[str, Any]:
    if repeats <= 0:
        raise ValueError("worker repeats must be positive")
    samples = [
        _run_worker_benchmark(
            corpus=corpus,
            workers=workers,
            model_dir=model_dir,
        )
        for _ in range(repeats)
    ]
    seconds = [float(sample["seconds"]) for sample in samples]
    throughput = [float(sample["files_per_second"]) for sample in samples]
    first = samples[0]
    return {
        "workers": first["workers"],
        "threads_per_worker": first["threads_per_worker"],
        "logical_cpus": first["logical_cpus"],
        "files": first["files"],
        "repeats": repeats,
        "median_seconds": round(statistics.median(seconds), 3),
        "median_files_per_second": round(statistics.median(throughput), 3),
        "peak_rss_bytes": max(int(sample["peak_rss_bytes"]) for sample in samples),
        "statuses": first["statuses"],
        "active_children_after_shutdown": max(
            int(sample["active_children_after_shutdown"])
            for sample in samples
        ),
        "samples": samples,
    }


def _run_worker_child(corpus: Path, workers: int) -> dict[str, Any]:
    paths = sorted(
        path
        for path in corpus.rglob("*")
        if path.is_file() and path.suffix.lower() in {".docx", ".pptx"}
    )
    plan = choose_worker_plan(len(paths), requested_workers=workers)
    started = time.perf_counter()
    with DoclingExtractionPool(plan) as pool:
        results = pool.extract(paths, backend_policy="docling")
    elapsed = time.perf_counter() - started
    statuses = Counter(result.status for result in results.values())
    resource_peak = _resource_peak_rss_bytes()
    return {
        "workers": plan.workers,
        "threads_per_worker": plan.threads_per_worker,
        "logical_cpus": plan.logical_cpus,
        "files": len(paths),
        "seconds": round(elapsed, 3),
        "files_per_second": round(len(paths) / max(elapsed, 1e-9), 3),
        "statuses": dict(sorted(statuses.items())),
        "active_children_after_shutdown": len(multiprocessing.active_children()),
        "resource_peak_rss_bytes": resource_peak,
    }


def _resource_peak_rss_bytes() -> int:
    # Linux reports KiB; macOS reports bytes.  Child max RSS is not an
    # aggregate across workers, so the parent-side psutil sampler remains the
    # preferred value and this is a deterministic lower-bound fallback.
    own = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    children = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    multiplier = 1 if sys.platform == "darwin" else 1024
    return (own + children) * multiplier


def _materialize_issues(catalog_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(catalog_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT d.path, c.chunk_index, c.text
            FROM chunk c
            JOIN document d ON d.doc_pk = c.doc_pk
            WHERE d.path LIKE '%/issues/%.md'
            ORDER BY d.path, c.chunk_index
            """
        ).fetchall()
    finally:
        connection.close()
    grouped: dict[str, list[str]] = {}
    for row in rows:
        name = Path(str(row["path"])).name
        grouped.setdefault(name, []).append(str(row["text"]))
    issue_dir = target / "issues"
    issue_dir.mkdir(parents=True, exist_ok=True)
    for stale in issue_dir.glob("*.md"):
        stale.unlink()
    for name, values in grouped.items():
        (issue_dir / name).write_text("\n\n".join(values), encoding="utf-8")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _paired_comparison(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, int]:
    baseline_rows = {
        str(row["query_id"]): row
        for row in baseline.get("cases") or []
        if row.get("answerable")
    }
    candidate_rows = {
        str(row["query_id"]): row
        for row in candidate.get("cases") or []
        if row.get("answerable")
    }
    counts = Counter()
    for query_id in sorted(set(baseline_rows) & set(candidate_rows)):
        before = baseline_rows[query_id]
        after = candidate_rows[query_id]
        before_value = (
            float(before["recall_at_8"]),
            float(before["reciprocal_rank"]),
            float(before["ndcg_at_8"]),
        )
        after_value = (
            float(after["recall_at_8"]),
            float(after["reciprocal_rank"]),
            float(after["ndcg_at_8"]),
        )
        counts[
            "win" if after_value > before_value else "loss" if after_value < before_value else "tie"
        ] += 1
    return {key: counts[key] for key in ("win", "loss", "tie")}


def _directory_size(path: Path) -> int:
    total = 0
    for value in path.rglob("*"):
        if value.is_file():
            try:
                total += value.stat().st_size
            except OSError:
                continue
    return total


def _percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _normalize_span(value: str) -> str:
    return " ".join(value.split()).casefold()


def _mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return sum(materialized) / max(1, len(materialized))


def _git_revision(path: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


if __name__ == "__main__":
    main()
