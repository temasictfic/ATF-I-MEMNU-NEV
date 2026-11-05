"""PyInstaller entry point for the desktop app."""

import multiprocessing
import sys

from atfi_app.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
