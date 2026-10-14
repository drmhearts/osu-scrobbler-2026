# Updates

Live `tray` and `watch` starts check this repository's **latest stable GitHub
release** automatically. Source commits alone do not update users. Prereleases,
downgrades and packages without verification data are rejected. Dry runs and
fixture demos use the installed version without contacting the update service.

The updater downloads the release's universal Python wheel over HTTPS and checks
its size, GitHub SHA-256 digest, package name/version and archive paths. It installs
in a separate virtual environment, resolves binary dependencies through the
configured package index, and verifies imports before selecting the new runtime.
The original project/venv, history, corrections, settings, startup registration
and OS-vault credentials are kept. Cached runtimes live in `DATA_DIR/updates`;
the current and previous versions are retained. Newer Python requirements need a
Python upgrade; an incompatible update fails without replacing the working app.

Checks have short network timeouts. Offline, rate-limited, corrupt or failed
installs leave the installed version running; the next startup tries again.
Updating waits until this profile's tray/watcher is stopped. Startup may take
longer while installing a release. Runtime regressions can be rolled back; the
installer check does not prove every native Windows UI interaction works.

Commands, prefixed with `.\.venv\Scripts\python.exe -m osu_scrobbler`:

| Command | Action |
| --- | --- |
| `update --check` | Check without installing |
| `update` | Install a published release; stop the app first |
| `update --rollback` | Restore the previous runtime and disable checks; stop the app first |
| `config set auto_update false` / `true` | Disable / enable automatic checks |

Initial installations predating **0.2.0** need the updater-containing release once.
After that, users do not need to revisit GitHub for routine application updates.
tosu and the underlying Python installation remain separately managed.

## Publishing (maintainers)

Commit the source, bump `pyproject.toml` and `src/osu_scrobbler/__init__.py` together,
and push a matching tag such as `v0.2.1`. The release workflow runs the Windows/
Linux test matrix, builds the wheel/source ZIP/checksums, then publishes the
release. GitHub supplies the wheel's digest in its API; the updater refuses assets
without it. Manual builds: `python tools/build_release.py --tag v0.2.1`.
If a newly introduced tag workflow did not start, push a `release/v0.2.1` branch
to run the same tests/publisher against the exact existing tag.

Keep releases immutable and retain storage/IPC compatibility for rollback and the
original launcher. Review dependency and data changes before tagging. No release
has been published by editing this checkout; the first tagged release must exist
in GitHub before other installations can download it automatically.
