# Updates

START checks public stable GitHub releases. **u** updates and restarts from the
terminal. A running instance is stopped through authenticated local IPC before
installation. Saved authorization, settings, corrections and history are kept.

The complete interface runs from the updated environment. Known original START
launchers are refreshed; user-edited files are left alone. The install folder is
remembered locally for this purpose. Older installs discover only their own
Python launch chain's location once; other process paths or command lines are not read.

Windows updates include Python and dependencies. No system Python or pip is
needed. Updates verify SHA256, package identity, every bundled file, archive
paths and size, install into an isolated local environment, and require a ready
tray before takeover. API limits fall
back to GitHub's public release/checksum downloads with the same verification.
Current and previous runtimes are kept. Failed updates keep the installed app.
Sign-in startup and normal launches perform these checks without opening windows.
Older source installs keep their credentials/settings and can move to the bundled
runtime through the same updater.

Advanced commands: `update --check`, `update --rollback` (stop the app first),
`config set auto_update false`. Rollback also disables automatic checks.


The application remains on the existing release repository, but current artifacts
use `osu-life-hacks` names. Transition releases also publish a legacy wheel and
Windows ZIP with identical program code and legacy archive/package identities.
`tools/compat_release.py` regenerates wheel RECORD hashes and the legacy portable
manifest, allowing pre-rename validators to install the new runtime. Current
updaters select the new names. Never replace the repository URL with an invented
repository name or remove integrity checks as part of a rename.

The old updater's smoke check unconditionally imports `websockets`. Only the
legacy transition wheel/ZIP therefore includes that library (unused by the new
application); the normal `osu-life-hacks` wheel/ZIP does not. Neither variant
includes, installs, launches or contacts tosu. Remove the legacy aliases and this
compatibility dependency once pre-0.5 updaters are no longer supported.

The import package, data directory, credential service and startup entry identity
remain stable. These internal compatibility names are intentional. The old reader
settings are ignored on load and removed on the next settings save. Old downloaded
helper files, if any, are never accessed or executed; users may remove them separately.
