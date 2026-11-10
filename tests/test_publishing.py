"""Exercise publishing guards with fake Git/Python functions: no remote mutations."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("scenario,success,push_count", [
    ("ok", True, 2), ("dirty", False, 0), ("published", False, 0), ("tests_fail", False, 0),
])
def test_publishing_requires_clean_unpublished_passing_commit(tmp_path, scenario, success, push_count):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is unavailable on this platform.")
    script = Path(__file__).resolve().parents[1] / "tools" / "publish.ps1"
    recorded = tmp_path / "calls.txt"
    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"
    harness = tmp_path / "harness.ps1"
    harness.write_text("""
$global:scenario = SCENARIO
$global:calls = CALLS
function global:git {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Parameters)
    $global:LASTEXITCODE = 0
    if ($Parameters[0] -eq 'status') {
        if ($global:scenario -eq 'dirty') { return ' M dirty.py' }
        return
    }
    if ($Parameters[0] -eq 'rev-parse') { return ('a' * 40) }
    if ($Parameters[0] -eq 'ls-remote') {
        if ($global:scenario -eq 'published' -and $Parameters[1] -eq '--tags') { return 'published-tag' }
        return
    }
    if ($Parameters[0] -eq 'push') { Add-Content -LiteralPath $global:calls -Value ($Parameters -join ' '); return }
    throw 'Unexpected git invocation'
}
function global:TestPython {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Parameters)
    $global:LASTEXITCODE = 0
    if ($Parameters[0] -eq '-c') { return '0.8.0' }
    if ($Parameters[0] -eq '-m' -and $Parameters[1] -eq 'pytest') {
        if ($global:scenario -eq 'tests_fail') { $global:LASTEXITCODE = 1 }
        return
    }
    throw 'Unexpected python invocation'
}
& SCRIPT -Python TestPython
""".replace("SCENARIO", literal(scenario)).replace("CALLS", literal(recorded)).replace("SCRIPT", literal(script)), encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
                            text=True, capture_output=True, timeout=25)
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    pushes = recorded.read_text(encoding="utf-8-sig").splitlines() if recorded.exists() else []
    assert len(pushes) == push_count
    if success:
        assert pushes == ["push origin HEAD:main", "push origin HEAD:refs/heads/release/v0.8.0"]
