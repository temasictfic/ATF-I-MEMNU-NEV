import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_output_dir() -> str:
    env_output = os.getenv("ATFI_OUTPUT_DIR")
    if env_output:
        return env_output

    if getattr(sys, "frozen", False):
        # Mirror atfi_app.paths.USER_DATA_DIR_NAME (historically Electron's
        # STABLE_USERDATA_DIR). Both must point at the same directory so
        # settings, API keys and caches survive the Electron -> Python port,
        # even if the env var is somehow not propagated.
        fallback = (
            Path.home() / "AppData" / "Roaming" / "atfi-memnu-app" / "output"
        )
        return str(fallback)

    return str(Path(__file__).resolve().parent.parent / "output")


def _default_ner_local_model_path() -> str:
    """Path to the fine-tuned INT8 ONNX NER model, or "" if it isn't present.

    The model is no longer bundled with releases: the desktop host downloads
    it once per machine and passes its location via
    ``ATFI_NER_LOCAL_MODEL_PATH`` (see ``atfi_app.model_manager``). In a
    source checkout ``scripts/fetch_model.py`` places it under
    ``backend/models/citation-ner-int8``.
    """
    candidates: list[Path] = []
    candidates.append(Path(__file__).resolve().parent / "models" / "citation-ner-int8")
    for c in candidates:
        if c.exists() and any(c.glob("*.onnx")):
            return str(c)
    return ""


class Settings(BaseSettings):
    port: int = 0
    # Loopback only: this is a desktop companion backend, not a network
    # service. Binding 0.0.0.0 would expose it to the LAN whenever the
    # firewall permits it.
    host: str = "127.0.0.1"
    output_dir: str = _default_output_dir()

    # Search
    search_timeout: int = 20
    max_concurrent_apis: int = 5
    max_concurrent_sources_per_pdf: int = 3

    # NER
    ner_local_model_path: str = _default_ner_local_model_path()
    ner_enabled: bool = True

    model_config = SettingsConfigDict(env_prefix="ATFI_")

    def get_cache_dir(self) -> Path:
        p = Path(self.output_dir) / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_settings_path(self) -> Path:
        return Path(self.output_dir) / "settings.json"


settings = Settings()
