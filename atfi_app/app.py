"""Entry point of the desktop app (replaces src/main/index.ts).

Start-up sequence:

1. Single-instance check: a second launch activates the first window.
2. Logging, data directories, the session list.
3. In-process backend on a pre-bound loopback socket. It also serves the
   built UI and the ``/__host`` bridge.
4. NER model check, downloading it in the background if needed.
5. The WebView2 window (or, with ``--dev-browser``, a plain browser tab).
6. One background update check about 5 s after the window is shown.

On exit the backend shuts down gracefully, and a downloaded-but-not-installed
update is installed (as electron-updater's ``autoInstallOnAppQuit`` did).
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser
from typing import Any

from . import logging_setup, paths
from .events import HostEvents
from .model_manager import ModelManager
from .session_store import SessionStore
from .single_instance import SingleInstance
from .updater import Updater
from .version import WINDOW_TITLE, __version__

logger = logging.getLogger("atfi.app")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="atfi-memnu", description="Atf-ı Memnu desktop app")
    parser.add_argument(
        "--dev-browser",
        action="store_true",
        help="serve the UI to a regular browser instead of the WebView2 window (development/testing)",
    )
    parser.add_argument("--port", type=int, default=0, help="backend port (default: random free port)")
    parser.add_argument("--token", default=None, help="fixed API token (dev-browser only; default random)")
    parser.add_argument("--no-open", action="store_true", help="--dev-browser: don't open a browser tab")
    parser.add_argument(
        "--smoke-test",
        metavar="SCREENSHOT",
        default=None,
        help="launch, wait until the UI and backend are up, save a screenshot and exit (CI)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _saved_language() -> str | None:
    try:
        from services.settings_store import get_current_settings  # backend

        lang = get_current_settings().language
        return lang if lang in ("tr", "en") else None
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    paths.ensure_dirs()
    log_file = logging_setup.configure(paths.logs_dir(), verbose=args.verbose)
    logger.info("Atf-ı Memnu %s starting (frozen=%s, log=%s)", __version__, paths.IS_FROZEN, log_file)

    dev_browser = args.dev_browser
    instance: SingleInstance | None = None
    if not dev_browser and args.smoke_test is None:
        instance = SingleInstance(paths.local_data_dir())
        if instance.notify_existing():
            logger.info("Another instance is running; asked it to come to the front")
            return 0

    # ---- shared services --------------------------------------------------
    events = HostEvents()
    session = SessionStore(paths.session_file())
    session.rotate()  # previous run's imports become the "last session" list

    dev_model_dir = None if paths.IS_FROZEN else paths.backend_dir() / "models" / "citation-ner-int8"
    model = ModelManager(events, paths.models_dir(), dev_model_dir=dev_model_dir)

    updates_enabled = paths.IS_FROZEN and not paths.is_portable() and not dev_browser
    updater = Updater(events, paths.updates_dir(), enabled=updates_enabled)

    # ---- platform -----------------------------------------------------------
    if dev_browser:
        from .platform_base import DevBrowserPlatform

        platform: Any = DevBrowserPlatform()
    else:
        if not paths.IS_WINDOWS:
            print("The desktop window needs Windows (WebView2). Use --dev-browser elsewhere.", file=sys.stderr)
            return 2
        from .native.platform import WebView2Platform

        platform = WebView2Platform(events=events, smoke_test_path=args.smoke_test)

    # ---- backend ------------------------------------------------------------
    from .backend_host import BackendHost, prepare_backend_import

    prepare_backend_import(paths.backend_dir(), output_dir=paths.output_dir(), model_dir=model.ready_path())
    from .host_api import HostServices, build_router

    services = HostServices(platform, session=session, updater=updater, model=model)

    host_holder: dict[str, BackendHost] = {}

    def boot_info() -> dict[str, Any]:
        host = host_holder["host"]
        return {
            "port": host.port,
            "token": host.token,
            "version": __version__,
            "language": _saved_language(),
            "mode": platform.mode,
            "lastSessionPdfs": session.last,
            "updatesEnabled": updates_enabled,
        }

    frontend = paths.frontend_dist_dir()
    try:
        host = BackendHost(
            frontend_dir=frontend,
            boot_info=boot_info,
            extra_routers=[build_router(services)],
            on_loop_ready=events.attach_loop,
            port=args.port,
            token=args.token if dev_browser else None,
            dev_origins=["http://localhost:5173", "http://127.0.0.1:5173"] if dev_browser else None,
        )
        host_holder["host"] = host
        host.start()
        host.wait_started(timeout=90)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Backend startup failed")
        platform.show_error(
            "Backend Startup Failed",
            f"The backend service failed to start.\n\n{exc}\n\nPlease reinstall or run the installer again.",
        )
        return 1
    logger.info("Backend serving %s", host.url)

    model.start(on_ready=host.reload_ner)

    exit_code = 0
    try:
        if dev_browser:
            exit_code = _run_dev_browser(host, platform, open_browser=not args.no_open)
        else:
            if instance is not None:
                instance.listen(platform.activate_window)
            exit_code = platform.run(
                url=host.url,
                title=WINDOW_TITLE,
                icon=paths.icon_path(),
                storage_path=paths.webview2_data_dir(),
                on_shown=lambda: updater.schedule_startup_check(5.0),
                window_state_path=paths.local_data_dir() / "window.json",
            )
    finally:
        logger.info("Shutting down")
        if instance is not None:
            instance.close()
        host.stop()
        installing = updater.install_pending_on_quit()
    # After the single-instance lock is released, so the new process doesn't
    # just hand off to this one. A pending update's installer relaunches
    # nothing, so skip it then rather than race the installer for files.
    if getattr(platform, "restart_requested", False) and not installing:
        _relaunch()
    return exit_code


def _relaunch() -> None:
    import subprocess

    if paths.IS_FROZEN:
        cmd = [sys.executable, *sys.argv[1:]]
    else:
        cmd = [sys.executable, "-m", "atfi_app", *sys.argv[1:]]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(cmd, close_fds=True, creationflags=flags)  # noqa: S603
        logger.info("Relaunched: %s", cmd)
    except OSError:
        logger.exception("Relaunch failed")


def _run_dev_browser(host: Any, platform: Any, *, open_browser: bool) -> int:
    url = host.url
    print(f"Atf-ı Memnu UI: {url}  (API token: {host.token})", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        while not platform.quit_requested:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

