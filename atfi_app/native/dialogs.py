"""Native file dialogs (replaces Electron's dialog.showOpenDialog/showSaveDialog).

Call these on the UI thread (``UiThread.invoke``). The folder picker reuses
pywebview's Vista ``IFileDialog`` wrapper, because .NET Framework's
``FolderBrowserDialog`` is the old tree-view picker.
"""

from __future__ import annotations

import os
from typing import Any

import System.Windows.Forms as WinForms  # type: ignore[import-not-found]


def _filter_string(filters: list[dict[str, Any]]) -> str:
    parts = []
    for f in filters or []:
        exts = [str(e).lstrip(".") for e in (f.get("extensions") or []) if e]
        pattern = ";".join(f"*.{e}" for e in exts) or "*.*"
        parts.append(f"{f.get('name') or 'Files'} ({pattern})|{pattern}")
    return "|".join(parts) or "All Files (*.*)|*.*"


def select_directory(owner: Any, default_path: str | None, title: str) -> str | None:
    from webview.platforms.winforms import OpenFolderDialog  # type: ignore[import-not-found]

    initial = default_path if default_path and os.path.isdir(default_path) else None
    result = OpenFolderDialog.show(owner, initial, False, title)
    return result[0] if result else None


def select_pdfs(owner: Any, title: str) -> list[str]:
    dialog = WinForms.OpenFileDialog()
    dialog.Title = title
    dialog.Filter = "PDF Files (*.pdf)|*.pdf"
    dialog.Multiselect = True
    dialog.RestoreDirectory = True
    if dialog.ShowDialog(owner) == WinForms.DialogResult.OK:
        return list(dialog.FileNames)
    return []


def save_as(owner: Any, title: str, default_path: str | None, filters: list[dict[str, Any]]) -> str | None:
    # Electron's `buttonLabel` has no WinForms equivalent; the dialog keeps
    # its standard "Save" button (see PORTING_REPORT.md).
    dialog = WinForms.SaveFileDialog()
    dialog.Title = title
    dialog.Filter = _filter_string(filters)
    dialog.OverwritePrompt = True
    dialog.AddExtension = True
    exts = (filters[0].get("extensions") if filters else None) or []
    if exts:
        dialog.DefaultExt = str(exts[0]).lstrip(".")
    if default_path:
        directory, name = os.path.split(default_path)
        dialog.FileName = name
        if directory and os.path.isdir(directory):
            dialog.InitialDirectory = directory
    dialog.RestoreDirectory = True
    if dialog.ShowDialog(owner) == WinForms.DialogResult.OK:
        return str(dialog.FileName)
    return None
