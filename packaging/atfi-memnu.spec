# -*- mode: python ; coding: utf-8 -*-
#
# One-folder Windows build of the whole app: the Python host (atfi_app,
# pywebview/pythonnet), the in-process FastAPI backend, and the built React UI.
# Build with scripts/build.ps1 (it runs `npm run build` first).
#
# Not bundled anymore: the 125 MB NER model. It is downloaded once per machine
# by the installer / atfi_app.model_manager (see atfi_app/model_manifest.json).

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPEC).resolve().parent.parent
BACKEND = ROOT / "backend"
DIST_UI = ROOT / "frontend" / "dist"

if not (DIST_UI / "index.html").exists():
    raise SystemExit(f"Frontend build missing at {DIST_UI}; run `npm run build` in frontend/ first.")

# onnxruntime subtrees that hard-import `onnx` or are irrelevant for
# InferenceSession use (same filter as the old backend spec).
_ORT_SKIP = (
    "onnxruntime.quantization",
    "onnxruntime.backend",
    "onnxruntime.tools",
    "onnxruntime.transformers",
    "onnxruntime.training",
)

# The backend is imported by top-level module names (config, main, api, ...)
# after its directory is put on sys.path, so list them for PyInstaller.
# Enumerated from the filesystem (importing them here would need the backend
# on sys.path of PyInstaller's isolated helper process).
backend_modules = ["config", "main", "security"]
for pkg in ("api", "models", "scrapers", "services", "utils", "verifiers"):
    backend_modules.append(pkg)
    for py in sorted((BACKEND / pkg).glob("*.py")):
        if py.stem != "__init__":
            backend_modules.append(f"{pkg}.{py.stem}")

hiddenimports = sorted(
    set(
        backend_modules
        + collect_submodules("uvicorn")
        + collect_submodules("fastapi")
        + collect_submodules("onnxruntime", filter=lambda n: not n.startswith(_ORT_SKIP))
        + ["tokenizers", "truststore", "clr"]
    )
)

datas = collect_data_files("certifi")
datas += [(str(DIST_UI), "frontend_dist")]
datas += [(str(ROOT / "resources" / "icon.ico"), "resources")]
datas += [(str(ROOT / "atfi_app" / "model_manifest.json"), "atfi_app")]

excludes = [
    # heavy ML frameworks we don't use
    "torch", "torchvision", "torchaudio", "tensorflow", "jax", "flax",
    "transformers", "optimum", "onnx",
    *_ORT_SKIP,
    # pulled in transitively; numpy keeps its own OpenBLAS
    "scipy",
    # GUI toolkits pywebview can use on other platforms
    "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "cefpython3",
    # dev-only
    "pytest", "httpx", "IPython",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT), str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=2,  # strip docstrings/asserts (pydantic uses annotations, not docstrings)
)


def _drop_root_libs_dupes(binaries):
    """numpy's OpenBLAS is emitted twice (bundle root + numpy.libs); keep one."""
    in_libs_dir = {Path(d).name for d, _s, _k in binaries if any(p.endswith(".libs") for p in Path(d).parts)}
    return [e for e in binaries if not (Path(e[0]).parent == Path(".") and Path(e[0]).name in in_libs_dir)]


a.binaries = _drop_root_libs_dupes(a.binaries)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KaynakcaKontrolSistemi",
    icon=str(ROOT / "resources" / "icon.ico"),
    version=str(ROOT / "packaging" / "build" / "version_info.txt") if (ROOT / "packaging" / "build" / "version_info.txt").exists() else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX triggers AV false positives and has corrupted onnxruntime DLLs
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="KaynakcaKontrolSistemi",
)
