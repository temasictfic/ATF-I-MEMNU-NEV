# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPEC).resolve().parent
backend_dir = project_root

# Hidden imports for servers and ML runtime. Uvicorn/fastapi use dynamic
# import strings; onnxruntime/tokenizers are safer to force-include so
# PyInstaller's static analysis doesn't miss native module loaders.
# Several onnxruntime subtrees hard-import `onnx` (a model-conversion dep we
# don't ship) or are otherwise irrelevant for pure InferenceSession use:
# `quantization`, `backend`, `tools`, `transformers`, `training`. Probing
# them during collect_submodules raises ModuleNotFoundError and emits a
# warning. Filter them out before the import probe — applied via `filter`
# so the warning is suppressed at the source. The core inference path
# (`onnxruntime.capi`, top-level package) is preserved.
_onnxruntime_skip_prefixes = (
    "onnxruntime.quantization",
    "onnxruntime.backend",
    "onnxruntime.tools",
    "onnxruntime.transformers",
    "onnxruntime.training",
)
onnxruntime_modules = collect_submodules(
    "onnxruntime",
    filter=lambda name: not name.startswith(_onnxruntime_skip_prefixes),
)
hiddenimports = sorted(
    set(
        collect_submodules("uvicorn")
        + collect_submodules("fastapi")
        + onnxruntime_modules
        + ["tokenizers"]
    )
)

# Bundled data: certifi root certs + the fine-tuned INT8 NER model. The
# model is placed under `models/citation-ner-int8/` inside the frozen
# bundle so config.py's `_MEIPASS / models / citation-ner-int8` lookup
# resolves it.
ner_model_dir = backend_dir / "models" / "citation-ner-int8"
datas = collect_data_files("certifi")
if not ner_model_dir.exists():
    raise SystemExit(
        f"NER model dir missing at {ner_model_dir}. "
        "Ensure git LFS has pulled the model before building."
    )
# Guard against checking out the LFS pointer (~130 B) instead of the real
# ~125 MB ONNX file. CI without `lfs: true` would otherwise ship a broken
# build that fails only at runtime.
onnx_path = ner_model_dir / "model_quantized.onnx"
if not onnx_path.exists() or onnx_path.stat().st_size < 1_000_000:
    size = onnx_path.stat().st_size if onnx_path.exists() else 0
    raise SystemExit(
        f"NER model at {onnx_path} looks like an LFS pointer (size={size} B). "
        "Run `git lfs pull` before building."
    )
datas += [
    (str(p), "models/citation-ner-int8")
    for p in ner_model_dir.iterdir()
    if p.is_file()
]

# Exclude the heavy ML frameworks we deliberately do not use. Without these
# excludes PyInstaller will still try to bundle torch / tensorflow / jax if
# any stray metadata sources them, adding ~1 GB each.
excludes = [
    "torch",
    "torchvision",
    "torchaudio",
    "tensorflow",
    "jax",
    "flax",
    "transformers",
    "optimum",
    "optimum.onnxruntime",
    "optimum_onnx",
    "onnx",
    "onnxruntime.quantization",
    "onnxruntime.backend",
    "onnxruntime.tools",
    "onnxruntime.transformers",
    "onnxruntime.training",
    # scipy is pulled in transitively (no `import scipy` in our code).
    # Excluding it drops scipy/optimize/_highspy and scipy.libs' OpenBLAS
    # (~45-50 MB combined). numpy keeps its own bundled OpenBLAS in
    # numpy.libs/, so linear algebra still works.
    "scipy",
    "scipy.libs",
]

a = Analysis(
    [str(backend_dir / "main.py")],
    pathex=[str(backend_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # -OO equivalent: strip docstrings and asserts from bundled .pyc files.
    # Pydantic uses annotations, not docstrings, so no behavioral impact.
    optimize=2,
)


# PyInstaller's numpy hook emits OpenBLAS twice: once at the bundle root
# (so Windows' DLL search path finds it) and once under `numpy.libs/`
# (where numpy._distributor_init's `os.add_dll_directory` points). On
# numpy 2.x the latter is sufficient — verified empirically by removing
# the root copy and confirming the backend boots and NER loads. Drop the
# duplicate to recover ~20 MB.
def _drop_root_libs_dupes(binaries):
    in_libs_dir = {
        Path(dest).name
        for dest, _src, _kind in binaries
        if any(part.endswith(".libs") for part in Path(dest).parts)
    }
    kept, dropped = [], 0
    for entry in binaries:
        dest, _src, _kind = entry
        path = Path(dest)
        if path.parent == Path(".") and path.name in in_libs_dir:
            dropped += 1
            continue
        kept.append(entry)
    print(f"[spec] Stripped {dropped} root-level *.libs duplicate(s)")
    return kept


a.binaries = _drop_root_libs_dupes(a.binaries)

pyz = PYZ(a.pure)

# UPX is disabled: it's a major antivirus false-positive trigger on Windows
# and historically corrupted onnxruntime's native DLLs. The bundle is small
# enough without UPX and runs reliably.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="atfi-memnu-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="atfi-memnu-backend",
)
