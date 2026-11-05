"""Download the NER model into backend/models/citation-ner-int8 (development).

Release builds don't bundle the model: the installer or the app downloads
it once per machine. For a source checkout run:

    uv run python scripts/fetch_model.py

Files are SHA-256 verified against atfi_app/model_manifest.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atfi_app import net  # noqa: E402
from atfi_app.model_manager import load_manifest  # noqa: E402


def main() -> int:
    target = ROOT / "backend" / "models" / "citation-ner-int8"
    manifest = load_manifest()
    for f in manifest["files"]:
        dest = target / f["name"]
        if dest.exists() and dest.stat().st_size == f["size"] and net.sha256_file(dest) == f["sha256"]:
            print(f"up to date  {f['name']}")
            continue
        for url in f["urls"]:
            try:
                print(f"downloading {f['name']} from {url}")
                net.download_file(url, dest, expected_sha256=f["sha256"], expected_size=f["size"])
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  failed: {exc}")
        else:
            print(f"could not download {f['name']}", file=sys.stderr)
            return 1
    print(f"Model ready in {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
