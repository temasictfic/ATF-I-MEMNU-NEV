"""Export the fine-tuned HF checkpoint to ONNX and dynamic INT8 quantize it.

Writes two directories next to the checkpoint:
  models/finetuned-onnx/       <- fp32 ONNX
  models/finetuned-onnx-int8/  <- INT8 ONNX (production target)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_FINETUNED = Path("backend/training/models/finetuned")
DEFAULT_ONNX_FP32 = Path("backend/training/models/finetuned-onnx")
DEFAULT_ONNX_INT8 = Path("backend/training/models/finetuned-onnx-int8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_FINETUNED)
    parser.add_argument("--onnx-dir", type=Path, default=DEFAULT_ONNX_FP32)
    parser.add_argument("--int8-dir", type=Path, default=DEFAULT_ONNX_INT8)
    parser.add_argument(
        "--quant-preset",
        choices=["avx512_vnni", "avx512", "avx2", "arm64"],
        default="avx512_vnni",
        help="Quantization instruction-set preset; fall back to avx2 on older CPUs.",
    )
    args = parser.parse_args()

    if not args.checkpoint.exists():
        print(f"error: checkpoint {args.checkpoint} does not exist", file=sys.stderr)
        return 2

    from optimum.onnxruntime import ORTModelForTokenClassification, ORTQuantizer
    from optimum.onnxruntime.configuration import AutoQuantizationConfig
    from transformers import AutoTokenizer

    print(f"Loading {args.checkpoint} and exporting to ONNX fp32...")
    model = ORTModelForTokenClassification.from_pretrained(args.checkpoint, export=True)
    tokenizer = AutoTokenizer.from_pretrained(args.checkpoint)
    args.onnx_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.onnx_dir)
    tokenizer.save_pretrained(args.onnx_dir)
    print(f"  wrote {args.onnx_dir}")

    print(f"Quantizing with dynamic INT8 ({args.quant_preset})...")
    quantizer = ORTQuantizer.from_pretrained(args.onnx_dir)
    preset_fn = getattr(AutoQuantizationConfig, args.quant_preset)
    qconfig = preset_fn(is_static=False, per_channel=True)
    args.int8_dir.mkdir(parents=True, exist_ok=True)
    quantizer.quantize(save_dir=str(args.int8_dir), quantization_config=qconfig)
    tokenizer.save_pretrained(args.int8_dir)
    print(f"  wrote {args.int8_dir}")

    def dir_size_mb(p: Path) -> float:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / (1024 * 1024)

    print()
    print(f"checkpoint (HF):   {dir_size_mb(args.checkpoint):.1f} MB")
    print(f"onnx fp32:         {dir_size_mb(args.onnx_dir):.1f} MB")
    print(f"onnx int8:         {dir_size_mb(args.int8_dir):.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
