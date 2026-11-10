[CmdletBinding()]
param([string]$Python = "python")

# Explicitly invoked by the maintainer. Never force-push, overwrite tags, or
# publish artifacts that have not passed the repository's release workflow.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed (exit $LASTEXITCODE)." }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $dirty = & git status --porcelain
    if ($LASTEXITCODE -ne 0) { throw "Open this script from a Git checkout." }
    if ($dirty) { throw "Commit/review all changes first. The release must have a clean working tree." }
    $testedCommit = (& git rev-parse HEAD).Trim()
    $releaseVersion = (& $Python -c "import tomllib; from osu_scrobbler import __version__; v=tomllib.load(open('pyproject.toml','rb'))['project']['version']; assert v==__version__; print(v)").Trim()
    if ($LASTEXITCODE -ne 0 -or $releaseVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "Install this checkout and match pyproject.toml / __init__.py versions first."
    }
    $tag = "v$releaseVersion"
    $existingTag = & git ls-remote --tags origin "refs/tags/$tag"
    if ($LASTEXITCODE -ne 0) { throw "Cannot check remote tags." }
    if ($existingTag) { throw "$tag already exists. Bump the version; published tags are never overwritten." }
    $existingBranch = & git ls-remote --heads origin "refs/heads/release/$tag"
    if ($LASTEXITCODE -ne 0) { throw "Cannot check the release branch." }
    if ($existingBranch -and -not $existingBranch.StartsWith($testedCommit)) {
        throw "release/$tag already points to another commit. Use a new version."
    }
    Invoke-Checked -Program $Python -Arguments @("-m", "pytest", "-q")
    if ((& git status --porcelain) -or (& git rev-parse HEAD).Trim() -ne $testedCommit) {
        throw "Checkout changed during tests. Review and run again."
    }
    Invoke-Checked -Program "git" -Arguments @("push", "origin", "HEAD:main")
    Invoke-Checked -Program "git" -Arguments @("push", "origin", "HEAD:refs/heads/release/$tag")
    Write-Host "Pushed $testedCommit for $tag."
    Write-Host "Open GitHub Actions > Publish release. CI tests/builds/verifies the Windows packages, then creates the tag and release."
    Write-Host "A failed workflow publishes nothing. Fix the cause before retrying; do not create a tag manually."
} finally {
    Pop-Location
}
