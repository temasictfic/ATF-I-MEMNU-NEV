"""Singleton manager for the bundled citation-parser NER model.

Loads the bundled fine-tuned INT8 ONNX model at startup using ONNX Runtime
and the `tokenizers` library directly — no transformers, no optimum, no
torch. The resulting pipeline object is callable with raw text and returns
a list of entity dicts matching HuggingFace's "simple" aggregation output
(keys: entity_group, score, start, end), so ner_extractor.py needs no
changes.

Execution provider is auto-detected:
  - CUDA (NVIDIA GPUs)
  - CPU otherwise
"""

import asyncio
import concurrent.futures
import json
import logging
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

_pipeline: Any | None = None
_pipeline_lock = asyncio.Lock()
_loading = False
_load_error: str | None = None
# Set to True once `preload_pipeline()` has finished its attempts (whether
# it succeeded or not). The `/api/ready` endpoint uses this to decide when
# the backend is safe to receive real traffic — we wait for preload to
# either populate the pipeline or commit to regex fallback.
_preload_complete = False

# RoBERTa's positional embedding is 514 (512 content + 2 special). Keep the
# tokenizer capped at 512 total so input_ids never exceed the model window.
_MAX_SEQ_LEN = 512

# Single-worker executor used by ner_extractor to serialize every inference
# call through one thread. The default asyncio executor has many workers, so
# concurrent verify batches would call `session.run()` in parallel — and
# one-at-a-time inference is all a single GPU can actually do anyway. The
# model is small (~30-80 ms/call on CPU) so serializing doesn't bottleneck
# interactive use.
_inference_executor: concurrent.futures.ThreadPoolExecutor | None = None


def get_inference_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Return the dedicated single-thread executor for NER inference.

    Created lazily. Keeping it module-global means every call to the model
    is serialized behind one worker thread.
    """
    global _inference_executor
    if _inference_executor is None:
        _inference_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ner-inference"
        )
    return _inference_executor


def is_model_ready() -> bool:
    return _pipeline is not None


def is_loading() -> bool:
    return _loading


def get_load_error() -> str | None:
    return _load_error


def is_preload_complete() -> bool:
    """True once `preload_pipeline()` has finished trying.

    Use this (not `is_model_ready()`) for readiness probes — it returns
    True both when preload succeeded and when it gave up and committed
    to the regex fallback.
    """
    return _preload_complete


def get_pipeline_sync() -> Any | None:
    """Return the pipeline if already loaded, None otherwise. Non-blocking."""
    return _pipeline


def _detect_ort_provider() -> str:
    """Detect the best available ONNX Runtime execution provider."""
    try:
        import onnxruntime as ort

        available = ort.get_available_providers()
        logger.info("ONNX Runtime available providers: %s", available)
        if "CUDAExecutionProvider" in available:
            logger.info("GPU detected: using CUDA execution provider")
            return "CUDAExecutionProvider"
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not query ONNX Runtime providers: %s", exc)
    logger.info("No GPU provider found; using CPU execution provider")
    return "CPUExecutionProvider"


def _softmax(x):
    import numpy as np

    x_max = np.max(x, axis=-1, keepdims=True)
    e = np.exp(x - x_max)
    return e / np.sum(e, axis=-1, keepdims=True)


class NerPipeline:
    """Thin replacement for `transformers.pipeline("ner", aggregation_strategy="simple")`.

    Callable with a single string. Returns a list of entity dicts with the
    same shape HF emits: ``{"entity_group", "score", "start", "end"}``.
    """

    def __init__(self, session, tokenizer, id2label: dict[int, str]):
        self.session = session
        self.tokenizer = tokenizer
        self.id2label = id2label
        self._input_names = {i.name for i in session.get_inputs()}

    def __call__(self, text: str) -> list[dict]:
        import numpy as np

        enc = self.tokenizer.encode(text)

        input_ids = np.array([enc.ids], dtype=np.int64)
        attention_mask = np.array([enc.attention_mask], dtype=np.int64)

        feeds: dict[str, Any] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        logits = self.session.run(None, feeds)[0]  # [1, seq, num_labels]
        probs = _softmax(logits[0])  # [seq, num_labels]
        label_ids = np.argmax(probs, axis=-1)  # [seq]
        scores = probs[np.arange(len(label_ids)), label_ids]  # [seq]

        tokens: list[dict] = []
        for label_id, score, offset, special in zip(
            label_ids, scores, enc.offsets, enc.special_tokens_mask
        ):
            if special:
                continue
            start, end = int(offset[0]), int(offset[1])
            if start == end:
                # Zero-width tokens (e.g. leading space on RoBERTa prefix-space
                # encoders) carry no text span — skip.
                continue
            tokens.append({
                "label": self.id2label[int(label_id)],
                "score": float(score),
                "start": start,
                "end": end,
            })

        return _aggregate_simple(tokens)


def _aggregate_simple(tokens: list[dict]) -> list[dict]:
    """Merge consecutive BIO-labeled tokens into entity spans.

    Mirrors HuggingFace's ``aggregation_strategy="simple"``: consecutive
    tokens sharing the same entity type are merged; a new B- marker (or a
    type change) starts a fresh span. Entity score is the mean of member
    token probabilities.
    """
    groups: list[dict] = []
    current: dict | None = None

    for tok in tokens:
        label = tok["label"]
        if label == "O":
            if current is not None:
                groups.append(current)
                current = None
            continue

        if label.startswith(("B-", "I-")):
            prefix = label[0]
            entity_type = label[2:]
        else:
            prefix = ""
            entity_type = label

        if (
            current is not None
            and entity_type == current["_type"]
            and prefix != "B"
        ):
            current["_scores"].append(tok["score"])
            current["end"] = tok["end"]
        else:
            if current is not None:
                groups.append(current)
            current = {
                "_type": entity_type,
                "_scores": [tok["score"]],
                "start": tok["start"],
                "end": tok["end"],
            }

    if current is not None:
        groups.append(current)

    return [
        {
            "entity_group": g["_type"],
            "score": sum(g["_scores"]) / len(g["_scores"]),
            "start": g["start"],
            "end": g["end"],
        }
        for g in groups
    ]


def _load_pipeline_sync() -> Any:
    """Load the bundled ONNX NER pipeline (blocking, call from executor)."""
    import onnxruntime as ort
    from tokenizers import Tokenizer

    local_path = settings.ner_local_model_path
    if not local_path or not Path(local_path).exists():
        raise RuntimeError(
            f"Bundled NER model not found at {local_path!r}. "
            "Ensure the model is included in the application package."
        )

    path = Path(local_path)
    onnx_files = sorted(path.glob("*.onnx"))
    if not onnx_files:
        raise RuntimeError(f"No .onnx files found in {path}")
    onnx_file = onnx_files[0]

    config_path = path / "config.json"
    if not config_path.exists():
        raise RuntimeError(f"config.json missing in {path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    id2label_raw = config.get("id2label") or {}
    id2label = {int(k): v for k, v in id2label_raw.items()}
    if not id2label:
        raise RuntimeError(f"config.json has no id2label mapping in {path}")

    tokenizer_path = path / "tokenizer.json"
    if not tokenizer_path.exists():
        raise RuntimeError(f"tokenizer.json missing in {path}")
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=_MAX_SEQ_LEN)

    provider = _detect_ort_provider()
    # Always include CPU as a fallback so a broken GPU driver or model-op
    # incompatibility degrades instead of disabling NER for the session.
    providers = [provider, "CPUExecutionProvider"] if provider != "CPUExecutionProvider" else [provider]

    # Every NER call is routed through the single-worker _inference_executor,
    # so there is never more than one `session.run()` in flight. Capping
    # ORT's own thread pools to 1 matches that reality, avoids contending
    # with ourselves, and reduces thread-count noise in PyInstaller builds.
    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = 1
    session_options.inter_op_num_threads = 1

    logger.info(
        "Loading ONNX NER model: %s (file=%s, providers=%s)",
        path, onnx_file.name, providers,
    )
    session = ort.InferenceSession(
        str(onnx_file),
        sess_options=session_options,
        providers=providers,
    )
    logger.info("ONNX NER pipeline loaded (provider=%s)", provider)
    return NerPipeline(session, tokenizer, id2label)


def shutdown_inference_executor() -> None:
    """Drain the dedicated inference executor. Call from app shutdown."""
    global _inference_executor
    if _inference_executor is not None:
        _inference_executor.shutdown(wait=False, cancel_futures=True)
        _inference_executor = None


async def get_pipeline() -> Any | None:
    """Get the loaded NER pipeline, loading it on first call.

    Returns None if NER is disabled or the pipeline fails to load.
    """
    global _pipeline, _loading, _load_error

    if not settings.ner_enabled:
        return None

    if _pipeline is not None:
        return _pipeline

    async with _pipeline_lock:
        if _pipeline is not None:
            return _pipeline

        _loading = True
        _load_error = None
        try:
            loop = asyncio.get_running_loop()
            _pipeline = await loop.run_in_executor(None, _load_pipeline_sync)
            return _pipeline
        except Exception as e:  # noqa: BLE001
            _load_error = str(e)
            logger.error("Failed to load NER pipeline: %s", e)
            return None
        finally:
            _loading = False


async def preload_pipeline() -> None:
    """Preload the NER model in the background so the first request is fast.

    Called during application startup. Failures are logged but do not
    prevent the app from starting — extraction falls back to regex.

    Retries once on failure with a short delay. This covers the first-
    launch-after-auto-update race where the installer hasn't yet flushed
    the 125 MB model file to disk. A single retry + 2 s pause is enough
    to clear that window without adding meaningful startup latency to a
    successful cold-start.
    """
    global _preload_complete
    if not settings.ner_enabled:
        logger.info("NER disabled, skipping model preload")
        _preload_complete = True
        return

    logger.info("Preloading NER model in background…")
    try:
        result = await get_pipeline()
        if result is not None:
            logger.info("NER model preloaded successfully")
            return

        first_error = _load_error
        logger.warning(
            "NER model preload failed (%s); retrying in 2 s…", first_error
        )
        await asyncio.sleep(2)

        result = await get_pipeline()
        if result is not None:
            logger.info("NER model preloaded successfully on retry")
        else:
            logger.warning(
                "NER model preload failed after retry (%s); committing to regex fallback",
                _load_error,
            )
    finally:
        # Flip the readiness flag no matter what — /api/ready will now
        # return 200 and the main process can unblock the UI.
        _preload_complete = True
