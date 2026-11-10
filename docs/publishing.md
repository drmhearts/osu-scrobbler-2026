# Publishing a release yourself

The release pipeline already builds the standalone Windows runtime, wheels,
compatibility aliases, source ZIP and SHA256SUMS.txt. It runs Linux/Windows tests
on Python 3.11/3.12/3.13 and three real installed tray smoke checks before publishing.
Do not upload an untested ZIP manually.

## Easiest route (PowerShell)

Use your Git checkout, with Python 3.11+ and Git installed and GitHub push access:

    python -m pip install -e ".[test]"
    git status
    git add .
    git commit -m "Add resumable bulk beatmap downloads"
    .\tools\publish.ps1

PowerShell may require a process-only execution-policy override:

    powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\publish.ps1

Pass -Python ".venv\Scripts\python.exe" if you use a virtual environment. Review the
files before git add; never commit browser profiles, credentials or download data.
The script requires a clean committed tree, checks package/runtime version
agreement and existing remote tags, runs the local test suite, then pushes HEAD
to main and release/v<version>. It never force-pushes or overwrites a tag.

The prepared version is **0.8.0**. Future releases must update pyproject.toml,
src/osu_scrobbler/__init__.py and docs/release-notes.md before committing.

Open your repository's **Actions → Publish release**. After every check succeeds,
the workflow creates the version tag on that exact tested commit and publishes
the downloads. Check the Releases page and download SHA256SUMS.txt. Nothing is
published if the required tests/build fail.

## Equivalent Git commands

After tests pass and the change is committed:

    git push origin HEAD:main
    git push origin HEAD:refs/heads/release/v0.8.0

This deliberately pushes a release branch rather than creating a tag yourself.
The workflow creates the tag after validation. Git will reject a non-fast-forward
main push; pull/rebase and resolve changes instead of adding --force.

If CI fails, inspect the failed job. Fix the cause, test, commit and push main plus
the same unreleased release branch again. Do not move an already published tag.
A rerun is appropriate for a demonstrated infrastructure failure, not to ignore
a reproducible test failure. A local sandbox may deny Windows junction creation;
run that existing updater test in ordinary Windows or rely on the required clean
Windows CI result before publication.

No GitHub API token belongs in application config. Git uses your normal GitHub
authentication; Actions uses the repository's GITHUB_TOKEN with contents:write in
the publish job. The repository must allow Actions to run and publish releases.
