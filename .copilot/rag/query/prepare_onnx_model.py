from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

RAG_ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOT = RAG_ROOT / "gen_db" / "software_rag_tool"
sys.path.insert(0, str(TOOL_ROOT))

from software_rag_tool.config import DEFAULT_EMBEDDING_MODEL, default_onnx_model_dir
from software_rag_tool.embeddings import embedding_fingerprint
from software_rag_tool.network import (
    NetworkConfigError,
    add_network_arguments,
    apply_network_environment,
    redact_text,
    resolve_network_configuration,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--output", default=str(default_onnx_model_dir()))
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--resolved-network-token",
        help=argparse.SUPPRESS,
    )
    add_network_arguments(parser)
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    model_path = output / "model.onnx"
    needs_external_network = args.force or not model_path.exists()
    try:
        network = resolve_network_configuration(
            cli_proxy=args.proxy,
            cli_ca_bundle=args.ca_bundle,
            cli_no_proxy=args.no_proxy,
            network_config=args.network_config,
            ignore_network_config=args.ignore_network_config,
            external_operation=needs_external_network,
            inherited_route_token=args.resolved_network_token,
        )
    except NetworkConfigError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_kind": exc.kind,
                    "error": redact_text(str(exc)),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
    apply_network_environment(network)
    for warning in network.warnings:
        print(warning, file=sys.stderr)
    os.environ["EMBEDDING_MODEL"] = args.model

    if model_path.exists() and not args.force:
        print(f"Ready: {output}", file=sys.stderr)
        return

    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
        from optimum.onnxruntime import ORTModelForFeatureExtraction
        from transformers import AutoTokenizer
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing ONNX export dependencies. Run python ~/.copilot/rag/query/setup.py first."
        ) from exc

    tmp = output.parent / f"{output.name}.fp32-tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    if output.exists() and args.force:
        shutil.rmtree(output)
    tmp.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)

    print(f"Exporting {args.model} to ONNX fp32...", file=sys.stderr)
    model = ORTModelForFeatureExtraction.from_pretrained(args.model, export=True)
    tokenizer = _load_tokenizer(AutoTokenizer, args.model)
    model.save_pretrained(tmp)
    tokenizer.save_pretrained(tmp)

    source_model = tmp / "model.onnx"
    if not source_model.exists():
        candidates = sorted(tmp.glob("*.onnx"))
        if not candidates:
            raise RuntimeError(f"ONNX export did not create a model under {tmp}")
        source_model = candidates[0]

    print("Quantizing ONNX model to INT8...", file=sys.stderr)
    quantize_dynamic(str(source_model), str(output / "model.onnx"), weight_type=QuantType.QInt8)

    for path in tmp.iterdir():
        if path.name.endswith(".onnx"):
            continue
        target = output / path.name
        if path.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)

    manifest = {
        "schema": "local-rag.onnx-model.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_model": args.model,
        "output": str(output),
        **embedding_fingerprint(),
    }
    (output / "MODEL_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(tmp)
    print(f"Ready: {output}", file=sys.stderr)


def _load_tokenizer(auto_tokenizer: object, model: str) -> object:
    try:
        return auto_tokenizer.from_pretrained(model, fix_mistral_regex=True)  # type: ignore[attr-defined]
    except TypeError:
        try:
            return auto_tokenizer.from_pretrained(model, fix_mistral_regex=False)  # type: ignore[attr-defined]
        except TypeError:
            return auto_tokenizer.from_pretrained(model)  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
