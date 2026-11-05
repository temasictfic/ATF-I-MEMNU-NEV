# Porting report: Electron → Python (pywebview) + native WebView2

This port moved Atf-ı Memnu v2.17.1 ([temasictfic/Atf-I-Memnu](https://github.com/temasictfic/Atf-I-Memnu)) off Electron. The app now runs in a Python host on the WebView2 runtime that ships with Windows. This report covers:

- what changed architecturally,
- what was **fixed** during the port (only low-risk, clear defects),
- what was **found but deliberately left as-is** (behaviour changes, larger refactors), for your decision,
- user-visible differences and how things were verified.

File references point at this repository unless marked *(old)*, which means the Electron repo.

---

## 1. Architecture

| Concern | Electron (old) | Now |
|---|---|---|
| Window and engine | `BrowserWindow` with bundled Chromium | pywebview window on the **system WebView2** (Edge) runtime |
| Main process | `src/main/index.ts` | `atfi_app/app.py` |
| Renderer ↔ host | preload `window.electronAPI` + IPC | `hostAPI` (`frontend/src/lib/host/bridge.ts`) calls token-authenticated same-origin HTTP (`/__host/rpc/*`, `/__host/pdf`); host events arrive on the existing WebSocket |
| `<webview>` (Scholar overlay and hidden scanner) | Electron guest webviews | `<atfi-webview>` custom element backed by **native WebView2 controls** (`atfi_app/native/guest_view.py`) sharing a persistent `scholar-panel` profile |
| Backend | separate PyInstaller exe, random port, `taskkill /F` on exit | **in-process** uvicorn thread on a pre-bound loopback socket, graceful shutdown |
| UI origin | `file://` | served by the backend at `http://127.0.0.1:<port>/`, so UI and API share one origin |
| Updater | electron-updater (`latest.yml`, blockmaps) | `atfi_app/updater.py`: GitHub Releases + `SHA256SUMS` + Inno Setup installer |
| NER model (125 MB) | bundled in **every** installer | downloaded **once** per machine to `%LOCALAPPDATA%\atfi-memnu-app\models`; updates reuse it |
| Installer | electron-builder NSIS + portable exe | Inno Setup (per-user) + portable zip |

### Why Google Scholar and Cloudflare Turnstile should behave better

The guest views are real Microsoft Edge. Electron had to imitate Chrome:

- a spoofed UA,
- `Sec-CH-UA` header rewriting,
- a preload that faked `navigator.userAgentData` and `navigator.webdriver`.

That imitation is a classic fingerprint mismatch. All of it is **removed**: `scholar-preload.ts`, the header rewriting in `onBeforeSendHeaders`, and the `getScholarUserAgent` plumbing. The guest also no longer receives an injected `console.log` relay script. Shortcuts are caught natively instead.

Google may still show CAPTCHAs based on IP and request rate, which the existing 4 s + jitter pacing handles. Those can't be eliminated entirely.

### Notable implementation details

- **`executeJavaScript`** uses DevTools `Runtime.evaluate` with `awaitPromise`. WebView2's `ExecuteScriptAsync` does not await promises, and the Scholar extraction scripts are async.
- **Native views and HTML overlays:** a native child window paints above the page. The HTML find bar is marked `data-webview-occluder`, and the host cuts it out of the native view's window region.
- **Tab visibility:** inactive tabs (`display:none`) report no client rects, so the native overlay hides.
- **The hidden scanner** stays alive off-screen at 1280×800 (`background` attribute), so timers aren't throttled.
- **Resource blocking** uses WebView2 resource filters scoped to tracker hosts and to scholar.google.com images/fonts/media. Other requests never round-trip through Python.
- **Dropped files:** WebView2 gives no path on `File` objects. The page posts the files with `postMessageWithAdditionalObjects`, and the host returns their paths.

---

## 2. Fixed during the port

### Security

| Issue | Where (old) | Fix |
|---|---|---|
| Backend CORS `allow_origins=["*"]` **with credentials** and no auth. Any website could scan localhost and read `GET /api/settings` (all API keys plus the OpenAIRE refresh token), start verifications, or shut the backend down. No Host check, so DNS rebinding worked too. | `backend/main.py:35-41`, `api/router.py:59` *(old)* | `backend/security.py` `LoopbackGuard` enforces a Host allow-list, a per-launch token on `/api/*` and `/__host/*` (header for REST, `?token=` for the WebSocket) and a WebSocket Origin check. CORS is removed (same origin). `/api/shutdown` is removed. |
| `shell.openExternal` accepted any URL, including `file://` and custom protocols. | `src/main/index.ts` *(old)* | `openExternal` allows only http/https/mailto (`atfi_app/host_api.py`). |
| `index.html` carried no anti-framing headers. | n/a | `X-Frame-Options: DENY` and `frame-ancestors 'none'` on the page that carries the token. |
| API keys could leak into log lines through URLs in error messages. | `orchestrator.py:666/674` *(old)* | `send_log` now writes to the log file, with `api_key`/`apikey`/`refreshToken`/`token` query values redacted. |

### Data loss and robustness

- **Non-atomic writes** of `settings.json` and the caches, combined with `taskkill /F`, could truncate files. A truncated settings file was then reset to defaults, wiping **all API keys**. Fixed with `backend/utils/atomic_io.py` (temp file, fsync, `os.replace`) plus graceful shutdown.
- **Lost or overwritten source edits:** the import orchestrator treated *any* backend error as "no cache" and PUT freshly detected sources over the user's edited or approved ones. It now retries once and otherwise fails the import (`frontend/src/lib/pdf/orchestrator.ts`).
- **Shutdown order:** in-flight verification tasks are cancelled and awaited *before* the aiohttp session closes. The daily-quota file is flushed. Background tasks are held by strong references (`services/background_tasks.py`); `asyncio.create_task` results could previously be garbage-collected.
- **Port race:** the backend socket is bound before uvicorn starts (`SO_EXCLUSIVEADDRUSE` on Windows). The old code acknowledged a TOCTOU window here.
- **Readiness:** the renderer could talk to the backend before it was listening (the port was exposed before health checks). The UI is now served *by* the backend, so it can't load first.

### Correctness

- **Rate limiter:** cancelled waiters kept their reserved slot, so arXiv's queue fed itself. They now release it (`scrapers/rate_limiter.py`).
- **OpenAIRE:** the anonymous 60/hour window was reset on every anonymous call, so it was never enforced. Re-registering an identical window is now a no-op.
- **Crossref:** author family names lost all non-ASCII letters (`Öztürk` became `ztrk`), which hurt Turkish citations.
- **TRDizin:** `publicationYear` is coerced to `int`. A string year raised `TypeError` inside scoring.
- **NER load failures:** the model reload used to be retried on every extraction call after a failure. The failure is now remembered, and there is an explicit `reload_pipeline()`.
- **Single-source reverify:** untouched text sent `""` and the backend verified an empty string. Fixed on both sides.
- **WebSocket broadcast:** it iterated a list that `disconnect()` mutated, which could skip clients.
- **Proxies:** aiohttp now honours `HTTP(S)_PROXY` and the Windows system proxy (`trust_env=True`). Before, the verifiers could not work on proxy-only networks.
- **SSL context:** it is cached instead of rebuilt (OS store + certifi) on the event loop for every URL check.

### Renderer bugs

- **Verification page:**
  - Ctrl+wheel zoomed twice per notch.
  - Overlay shortcuts (Esc/F5/Ctrl+F/Ctrl±/mouse back) fired while another tab was showing.
  - The overlay collapsed to 220 px after a tab switch.
  - "Open in browser" opened the initial URL instead of the current one.
  - Re-opening the same link did nothing.
  - CAPTCHA auto-resume timers leaked.
  - The Scholar scan could be scheduled twice, and still started after Stop.
  - One NER request fired per keystroke (now debounced).
  - The export toast showed the button label.
  - Several alerts were hard-coded in English.
  - It referenced an undefined CSS class.
- **Scholar scanner:**
  - A cancel/restart race let two loops process one queue.
  - Results were scored after a cancel.
  - Unhandled `loadURL` rejections.
  - Sub-frame and aborted loads failed the whole search.
- **Parsing page:**
  - Space merged sources and Ctrl+Z reverted even while typing in text fields.
  - Delete removed both a note and a hidden source.
  - The note-Delete handler fired on other tabs.
  - A stale counter gave duplicate ids for drawn rectangles.
  - CSS-module classes used by NotesLayer were missing.
- **Settings page:**
  - Clearing a number field saved `0` and ignored min/max. Fields now commit on blur/Enter, clamped.
  - The OpenAIRE button could stay disabled forever.
  - Message timers overlapped.
- **Update pill:**
  - It jumped off-centre during its fade-in animation.
  - A dismissed download popped back up.
  - Events sent before the page connected were lost. The state is now replayed on mount.
  - User-initiated download errors are always shown. electron-updater swallowed "ignorable" network errors even then, leaving the UI stuck at its progress percentage.
- **Fonts:** a failed font load was cached forever and broke every PDF export for the session.
- **WebSocket client:** a connect race could create duplicate sockets.
- **Startup language:** the UI always started in Turkish until settings loaded. The saved language is now injected at boot.
- **Recent-PDF list:** it was lost after any launch without an import. It is now host-persisted (`atfi_app/session_store.py`).

### Dead code removed

- **Backend:**
  - endpoints `POST /verify`, `POST /verify/pdf/{id}`, `DELETE /parse/pdf/{id}`, `POST /settings/databases`, `POST /shutdown`;
  - `server_state.py` and `compare_extraction.py`;
  - helpers `get_pipeline_sync`, `get_reports_dir`, `get_models_dir`, `normalize_text`, `clean_source_text`, `find_first_url`, `DOI_RE`/`URL_RE`;
  - the unused `log_entry` WebSocket stream;
  - dependencies `beautifulsoup4`, `lxml`, `python-multipart`, `urllib3`, and the `uvicorn[standard]` extras.
- **Frontend:**
  - store actions `addPdf`, `setPdfPath`, `clearPdfs`, `reverifyPdf`, `reorderSources`, `addDatabase`;
  - helpers `mergeWithClosest`, `canRevert`, `canRevertNotes`, `removeAutoNotesForPdf`, `setHighlightColor`, `setCalloutColor`, `anyTagOn`, `verifyStatusLabel`, `looksLikeCitation`, plus 5 unused types;
  - no-op console-logging buffers in the verification and PDF stores;
  - 14 unused i18n keys (the en/tr key sets are still identical);
  - 49 unused CSS-module rules;
  - an unused store subscription that caused extra renders.
- **TypeScript:** `noUnusedLocals` is now on.
- **Electron-only code:** the main process, preloads, UA spoofing and the console-sentinel injection script.

---

## 3. Found but not changed (your call)

These change behaviour or need a bigger refactor, so they were left alone.

### Correctness and behaviour

1. **An internal error becomes "fabricated".** `_verify_source` catches every exception, and `_finalize_result(parsed=None)` then records `low` + `fabricated` (`backend/services/verification_orchestrator.py`, around lines 448 and 796). An internal error should probably surface as an error state instead.
2. **"Cancel all" doesn't stop a multi-PDF batch.** Batch jobs swallow `CancelledError` per PDF and move on to the next one (`api/verification.py` `_verify_batch`, orchestrator 208-213).
3. **Races during verification:** `PUT /parse/sources` replaces results while a run is still writing them, so removed sources come back. `cancel/source` misses sources that belong to a PDF run.
4. **No request pacing** for Europe PMC, TRDizin and Open Library, even though rates are configured for two of their hosts.
5. **`search_timeout` changes need a restart.** The aiohttp session timeout is frozen at creation.
6. **`strip_source_noise` is not idempotent** but runs 3–4 times per source. For example, `"[12] 3-D printing"` loses `3-` on the second pass.
7. **"~X" (verify non-high) Scholar scan.** An old comment says it should run *regardless* of the auto-Scholar setting, but the code checks the setting. It now schedules once and respects the setting; the comment was updated. Decide whether it should be forced.
8. **Settings store overwrites in-flight edits.** Autosave and DB-op responses replace the whole `settings` object, so characters typed while a request is in flight can be lost. Concurrent DB toggles can also resolve out of order.
9. **PDF annotation coordinates** ignore a non-zero MediaBox origin and `/Rotate` (`frontend/src/lib/pdf/annotation-writer.ts`). **Report writer:** `wrapText` never hard-breaks long URLs or DOIs, so they overflow.
10. **Source detection:** the loose header regex matches substrings ("Resources", "Kaynakları"), so an early body-text line can win over the real KAYNAKLAR header.
11. **Polling** stops permanently after 3 errors and isn't resumed after a WebSocket reconnect, so cards can stay `in_progress`. Polling runs every 5 s even while the WebSocket is healthy.
12. **URL checker:** the SSRF guard resolves DNS itself, and aiohttp resolves again when it connects (DNS-rebinding race).
13. **Notes stay after a PDF is removed** and reappear on re-import (`clearNotesForPdf` exists but isn't called). This may be intentional.
14. **`GET /settings/openaire/status`** exists so the UI can show silent OpenAIRE auth failures, but nothing displays it.
15. **Settings modal:** pressing Enter on *any* button applies the values.

### Performance

- **VerificationPage (≈3,300 lines)** subscribes to whole store maps with no `useShallow` and no memoized card component. Every `verify_db_checked` WebSocket event re-renders the whole page, even on another tab.
- **ParsingPage** renders NotesLayer and source boxes for every page (only the canvases are virtualized). Drags and scrolling set state on every event.
- **The PDF is read and parsed up to 3 times** per import/view/export.
- **O(n²) result accumulation** per PDF, and per-note copies in auto-notes.
- **Backend:**
  - synchronous disk I/O on the event loop (cache/settings);
  - `inspect.signature` per verifier call;
  - the verifier registry is rebuilt per source;
  - Crossref makes up to 8 query variants per source at 1 req/s.
- **ONNX** uses `intra_op_num_threads = 1`, although inference is already serialized on one worker. Raising it to the number of physical cores would speed up NER.

### Security hardening

- API keys are stored in plain text in `settings.json`. Encrypting them with Windows DPAPI would be cheap.

---

## 4. User-visible differences

- **First install** downloads the NER model (~125 MB) once. Until it finishes, field extraction uses the regex fallback (same as before when the model failed to load). A small status pill in the header shows progress or offers a retry. Installers and updates are ~100 MB smaller.
- **Updates** are full installers rather than blockmap deltas, but much smaller without the model. The portable zip doesn't self-update.
- **Popups** opened from the Scholar panel (`target=_blank`) open in the same panel, where Back/zoom/find work. Electron opened a separate bare window.
- **The Save dialog** keeps Windows' standard "Save" button; Electron's custom `buttonLabel` has no WinForms equivalent.
- **The "recent PDFs" list** starts empty once after upgrading (it lived in Electron's localStorage). Settings, API keys, caches and verification records are kept, because the same `%APPDATA%\atfi-memnu-app` folder is used.
- **Leftover Electron data** under `%APPDATA%\atfi-memnu-app` (`Cache`, `Code Cache`, `GPUCache`, `Partitions`, `Local Storage`, …) is no longer used and can be deleted. Only `output\` matters.
- **Old install:** the new installer offers to uninstall the old Electron app and reuses its bundled model instead of downloading.

---

## 5. Verification status

Done in this (Linux) environment:

- `npm run typecheck` and `npm run build` pass, with `noUnusedLocals` on.
- `uv run pytest` passes (backend regression tests + host tests): the security guard, atomic writes, rate limiter, OpenAIRE window, Crossref/TRDizin, updater (mock GitHub: version compare, download, checksum mismatch), model manager (mirror fallback, resume, legacy adoption, bad hash), host API (token, dialogs, PDF streaming, sorting, `openExternal` policy, exclusion cache), a real in-process uvicorn lifecycle, single instance, and the drop queue.
- End-to-end in Chromium against `--dev-browser` (`tests/ui/dev-browser-smoke.mjs`), with screenshots:
  - import (dialog stand-in, `/__host/pdf`, pdf.js, detection, cache write);
  - NER fields;
  - approve;
  - Verification page (WebSocket flow, Scholar queue);
  - Settings.
- The real model download from the mirror, SHA-256 verification and the hot reload of the backend NER all worked.
- Live database queries couldn't be exercised: this sandbox's network allowlist blocks those hosts, and the requests reached the proxy and were refused.

**Not verifiable here (needs Windows).** CI covers part of it: `ci.yml` builds on `windows-latest` and runs `--smoke-test`. The smoke test launches the real exe, waits for the UI, creates the hidden Scholar WebView2 view, runs an async `executeJavaScript` through it, and saves a screenshot. Please check by hand:

- [ ] Scholar overlay: position and resize at 100% / 150% DPI; the find bar visible through the overlay; switching tabs hides it.
- [ ] CAPTCHA flow: the overlay opens, solving it auto-resumes the scanner, and cookies are shared with the hidden scanner.
- [ ] Mouse back/forward in the overlay navigates once (not twice).
- [ ] Drag-and-drop PDFs from Explorer.
- [ ] Update: download, progress, cancel, install and relaunch; install-on-quit.
- [ ] Fresh install downloads the model; upgrading over the Electron app reuses its model and keeps settings.
