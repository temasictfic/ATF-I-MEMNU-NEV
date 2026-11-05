"""Windows-only native layer (pywebview / WinForms / WebView2 via pythonnet).

Nothing in this package may be imported on other platforms, or before
pywebview has loaded the CLR and the WebView2 assemblies (that happens in
``webview.start``). ``platform.WebView2Platform`` imports the rest lazily
from inside pywebview callbacks.

This is the only code that touches pywebview internals: ``window.native``
is the WinForms ``BrowserForm`` and ``form.browser.webview`` is the
``Microsoft.Web.WebView2.WinForms.WebView2`` control. pywebview is pinned
to an exact version in pyproject.toml, and the CI smoke test exercises
this layer on Windows.
"""
