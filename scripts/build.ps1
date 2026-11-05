<#
.SYNOPSIS
  Builds the Windows release: frontend, PyInstaller one-folder app, Inno Setup
  installer, portable zip and SHA256SUMS.

.DESCRIPTION
  Replaces `npm run dist:win` (electron-builder) and scripts/build-backend.ps1.

  Steps:
    1. npm ci + vite build in frontend/            -> frontend/dist
    2. uv sync (with the build group)                -> .venv with PyInstaller
    3. version info + installer include (manifest)   -> packaging/build/
    4. WebView2 Evergreen bootstrapper download      -> packaging/build/
    5. PyInstaller                                   -> packaging/dist/KaynakcaKontrolSistemi/
    6. Inno Setup (iscc)                             -> packaging/dist/Kaynakca-Kontrol-Sistemi-Setup-<v>.exe
    7. Portable zip (+ portable.txt marker)          -> packaging/dist/Kaynakca-Kontrol-Sistemi-<v>-portable.zip
    8. SHA256SUMS (verified by the in-app updater)   -> packaging/dist/SHA256SUMS

  Requirements: Node 22+, uv, Inno Setup 6 (iscc on PATH or in the default
  install location). The NER model is NOT bundled: it is downloaded once per
  machine by the installer or the app.

.PARAMETER SkipInstaller
  Build only the one-folder app (and portable zip).
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$packaging = Join-Path $root 'packaging'
$buildDir = Join-Path $packaging 'build'
$distDir = Join-Path $packaging 'dist'

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

Step 'Frontend (npm ci + vite build)'
Push-Location (Join-Path $root 'frontend')
npm ci; Check 'npm ci'
npm run build; Check 'vite build'
Pop-Location

Step 'Python environment (uv sync --group build)'
Push-Location $root
uv sync --frozen --group build; Check 'uv sync'
$version = (uv run python scripts/release_tools.py version).Trim(); Check 'version'
Write-Host "Version $version"

Remove-Item -Recurse -Force $buildDir, $distDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $buildDir, $distDir | Out-Null
uv run python scripts/release_tools.py version-info (Join-Path $buildDir 'version_info.txt'); Check 'version-info'
uv run python scripts/release_tools.py installer-include (Join-Path $buildDir 'app.iss'); Check 'installer-include'

Step 'WebView2 Evergreen bootstrapper'
$bootstrapper = Join-Path $buildDir 'MicrosoftEdgeWebview2Setup.exe'
Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $bootstrapper -UseBasicParsing
$sig = Get-AuthenticodeSignature $bootstrapper
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Microsoft') {
    throw "WebView2 bootstrapper signature check failed: $($sig.Status) $($sig.SignerCertificate.Subject)"
}

Step 'PyInstaller'
uv run pyinstaller --noconfirm --clean (Join-Path $packaging 'atfi-memnu.spec') --distpath $distDir --workpath (Join-Path $buildDir 'pyinstaller'); Check 'PyInstaller'
$appDir = Join-Path $distDir 'KaynakcaKontrolSistemi'
if (-not (Test-Path (Join-Path $appDir 'KaynakcaKontrolSistemi.exe'))) { throw 'PyInstaller output missing' }
Pop-Location

$artifacts = @()

if (-not $SkipInstaller) {
    Step 'Inno Setup'
    $iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
            if (Test-Path $candidate) { $iscc = $candidate; break }
        }
    }
    if (-not $iscc) { throw 'Inno Setup 6 (ISCC.exe) not found' }
    & $iscc (Join-Path $packaging 'installer.iss'); Check 'iscc'
    $artifacts += Join-Path $distDir "Kaynakca-Kontrol-Sistemi-Setup-$version.exe"
}

Step 'Portable zip'
$portableStage = Join-Path $buildDir 'portable'
Copy-Item -Recurse $appDir $portableStage
Set-Content -Path (Join-Path $portableStage 'portable.txt') -Value 'Portable build: updates are disabled; the NER model is downloaded on first launch.'
$zip = Join-Path $distDir "Kaynakca-Kontrol-Sistemi-$version-portable.zip"
Compress-Archive -Path (Join-Path $portableStage '*') -DestinationPath $zip -CompressionLevel Optimal
$artifacts += $zip

Step 'SHA256SUMS'
Push-Location $root
uv run python scripts/release_tools.py checksums (Join-Path $distDir 'SHA256SUMS') @artifacts; Check 'checksums'
Pop-Location

Write-Host "`nArtifacts in $distDir" -ForegroundColor Green
Get-ChildItem $distDir -File | Format-Table Name, Length
