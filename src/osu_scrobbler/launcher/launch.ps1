# Dispatch only this profile's verified current runtime. No system Python needed.
$ErrorActionPreference = 'Stop'
try {
    $profileDir = $PSScriptRoot
    $updates = Join-Path $profileDir 'updates'
    $current = Get-Content -LiteralPath (Join-Path $updates 'current.json') -Raw | ConvertFrom-Json
    if ($current.version -cnotmatch '^\d+\.\d+\.\d+$' -or
        $current.digest -cnotmatch '^[a-f0-9]{64}$' -or
        $current.environment -cnotmatch '^versions/[0-9.]+-[a-z0-9_-]+/(venv|runtime)$') { exit 1 }
    $runtime = Join-Path $updates $current.environment
    $ready = Get-Content -LiteralPath (Join-Path (Split-Path $runtime) 'ready.json') -Raw | ConvertFrom-Json
    if ($ready.version -cne $current.version -or $ready.digest -cne $current.digest -or
        $ready.environment -cne $current.environment) { exit 1 }
    if ($current.environment.EndsWith('/runtime')) { $python = Join-Path $runtime 'pythonw.exe' }
    else { $python = Join-Path $runtime 'Scripts/pythonw.exe' }
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) { exit 1 }
    # Windows paths cannot contain quotes; pass the local profile as one argument.
    $arguments = @('-m', 'osu_scrobbler', '--data-dir', ('"' + $profileDir + '"'), 'launch')
    Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $runtime -WindowStyle Hidden
    exit 0
} catch { exit 1 }
