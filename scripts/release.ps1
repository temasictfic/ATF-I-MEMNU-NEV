<#
.SYNOPSIS
  Bumps the app version, commits, tags v<version> and pushes. The tag push
  triggers .github/workflows/release.yml, which builds and publishes the release.

.DESCRIPTION
  Updates the version in every place it lives:
    atfi_app/version.py         (__version__, source of truth; CI checks the tag against it)
    pyproject.toml              ([project] version)
    uv.lock                     (via `uv lock`)
    frontend/package.json       (via `npm version --no-git-tag-version`)
    frontend/package-lock.json  (same)

  Then: commit "Release X.Y.Z", annotated tag vX.Y.Z, atomic push of main + tag.

  Refuses to run unless you are on main, the working tree is clean, main is
  not behind origin, and the tag does not exist locally or on origin.

.PARAMETER Bump
  major | minor | patch

.PARAMETER DryRun
  Show the new version and what would happen; change nothing.

.PARAMETER NoPush
  Commit and tag locally but do not push. Push later with:
    git push --atomic origin main vX.Y.Z

.PARAMETER Yes
  Skip the confirmation prompt before pushing.

.EXAMPLE
  ./scripts/release.ps1 patch
  ./scripts/release.ps1 minor -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('major', 'minor', 'patch')]
    [string]$Bump,
    [switch]$DryRun,
    [switch]$NoPush,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$branch = 'main'
$remote = 'origin'

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
function ReadText($rel) { [IO.File]::ReadAllText((Join-Path $root $rel)) }
function WriteText($rel, $text) {
    # UTF-8 without BOM; line endings are preserved because we edit the raw text.
    [IO.File]::WriteAllText((Join-Path $root $rel), $text, [Text.UTF8Encoding]::new($false))
}
function ReplaceOnce($rel, $pattern, $replacement) {
    $text = ReadText $rel
    $rx = [regex]::new($pattern, 'Multiline')
    if (-not $rx.IsMatch($text)) { throw "Version pattern not found in $rel" }
    WriteText $rel ($rx.Replace($text, $replacement, 1))
}

Push-Location $root
try {
    # ---- preflight -----------------------------------------------------------
    Step 'Preflight checks'

    $current = (git rev-parse --abbrev-ref HEAD).Trim(); Check 'git rev-parse'
    if ($current -ne $branch) { throw "On branch '$current'; releases are made from '$branch'." }

    $dirty = git status --porcelain; Check 'git status'
    if ($dirty) { throw "Working tree is not clean. Commit or stash first:`n$($dirty -join "`n")" }

    git fetch --quiet --tags $remote $branch; Check 'git fetch'
    $behind = [int](git rev-list --count "HEAD..$remote/$branch"); Check 'git rev-list'
    if ($behind -gt 0) { throw "$branch is $behind commit(s) behind $remote/$branch. Pull first." }

    $versionPy = ReadText 'atfi_app/version.py'
    $m = [regex]::Match($versionPy, '(?m)^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"')
    if (-not $m.Success) { throw 'Could not parse __version__ in atfi_app/version.py' }
    $old = '{0}.{1}.{2}' -f $m.Groups[1].Value, $m.Groups[2].Value, $m.Groups[3].Value
    [int]$major = $m.Groups[1].Value
    [int]$minor = $m.Groups[2].Value
    [int]$patch = $m.Groups[3].Value

    # The other version fields should already agree; a mismatch means an
    # earlier manual edit went wrong, so stop rather than paper over it.
    $pyproj = [regex]::Match((ReadText 'pyproject.toml'), '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
    $pkg = (ReadText 'frontend/package.json' | ConvertFrom-Json).version
    if ($pyproj -ne $old -or $pkg -ne $old) {
        throw "Version mismatch: version.py=$old pyproject.toml=$pyproj package.json=$pkg"
    }

    switch ($Bump) {
        'major' { $major++; $minor = 0; $patch = 0 }
        'minor' { $minor++; $patch = 0 }
        'patch' { $patch++ }
    }
    $new = "$major.$minor.$patch"
    $tag = "v$new"

    git rev-parse -q --verify "refs/tags/$tag" *> $null
    if ($LASTEXITCODE -eq 0) { throw "Tag $tag already exists locally." }
    $remoteTag = git ls-remote --tags $remote "refs/tags/$tag"; Check 'git ls-remote'
    if ($remoteTag) { throw "Tag $tag already exists on $remote." }

    $unpushed = [int](git rev-list --count "$remote/$branch..HEAD")
    Write-Host "Version  $old -> $new  ($Bump)" -ForegroundColor Green
    Write-Host "Tag      $tag"
    if ($unpushed -gt 0) { Write-Host "Note     $unpushed unpushed commit(s) on $branch will be pushed with the release." -ForegroundColor Yellow }

    if ($DryRun) {
        Write-Host "`nDry run: nothing changed." -ForegroundColor Yellow
        return
    }

    # ---- bump ----------------------------------------------------------------
    $touched = @('atfi_app/version.py', 'pyproject.toml', 'uv.lock', 'frontend/package.json', 'frontend/package-lock.json')
    try {
        Step 'Updating version files'
        ReplaceOnce 'atfi_app/version.py' '^(__version__\s*=\s*")[^"]+(")' "`${1}$new`${2}"
        ReplaceOnce 'pyproject.toml' '^(version\s*=\s*")[^"]+(")' "`${1}$new`${2}"

        uv lock --quiet; Check 'uv lock'

        Push-Location (Join-Path $root 'frontend')
        try { npm version $new --no-git-tag-version --allow-same-version | Out-Null; Check 'npm version' }
        finally { Pop-Location }

        $check = (uv run --frozen python scripts/release_tools.py version).Trim(); Check 'version check'
        if ($check -ne $new) { throw "release_tools reports $check after bump, expected $new" }

        Step "Commit and tag $tag"
        git add -- $touched; Check 'git add'
        git commit --quiet -m "Release $new"; Check 'git commit'
        git tag -a $tag -m "Release $new"; Check 'git tag'
    }
    catch {
        Write-Host "`nBump failed; restoring version files." -ForegroundColor Red
        git reset --quiet HEAD -- $touched 2>$null
        git checkout -- $touched 2>$null
        throw
    }

    git log --oneline -1

    # ---- push ----------------------------------------------------------------
    if ($NoPush) {
        Write-Host "`nCommitted and tagged locally. Push with:" -ForegroundColor Yellow
        Write-Host "  git push --atomic $remote $branch $tag"
        return
    }

    if (-not $Yes) {
        $answer = Read-Host "`nPush $branch and $tag to $remote (starts the release build)? [y/N]"
        if ($answer -notmatch '^(y|yes)$') {
            Write-Host 'Not pushed. The commit and tag stay local. Push later with:' -ForegroundColor Yellow
            Write-Host "  git push --atomic $remote $branch $tag"
            Write-Host 'Or undo with:'
            Write-Host "  git tag -d $tag; git reset --hard HEAD~1"
            return
        }
    }

    Step "Pushing $branch and $tag"
    git push --atomic $remote $branch $tag; Check 'git push'

    $repo = (git remote get-url $remote) -replace '\.git$', '' -replace '^git@github\.com:', 'https://github.com/'
    Write-Host "`nReleased $tag. The build is running at:" -ForegroundColor Green
    Write-Host "  $repo/actions/workflows/release.yml"
}
finally {
    Pop-Location
}
