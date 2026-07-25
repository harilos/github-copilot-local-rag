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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--output", default=str(default_onnx_model_dir()))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    os.environ["EMBEDDING_MODEL"] = args.model

    output = Path(args.output).expanduser().resolve()
    model_path = output / "model.onnx"
    if model_path.exists() and not args.force:
        print(f"Ready: {output}")
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

    print(f"Exporting {args.model} to ONNX fp32...")
    model = ORTModelForFeatureExtraction.from_pretrained(args.model, export=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model.save_pretrained(tmp)
    tokenizer.save_pretrained(tmp)

    source_model = tmp / "model.onnx"
    if not source_model.exists():
        candidates = sorted(tmp.glob("*.onnx"))
        if not candidates:
            raise RuntimeError(f"ONNX export did not create a model under {tmp}")
        source_model = candidates[0]

    print("Quantizing ONNX model to INT8...")
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
    print(f"Ready: {output}")


if __name__ == "__main__":
    main()
