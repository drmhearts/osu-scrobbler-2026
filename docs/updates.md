# Updates

START checks public stable GitHub releases. **u** updates and restarts from the
terminal. A running instance is stopped through authenticated local IPC before
installation. Saved authorization, settings, corrections and history are kept.

The complete interface runs from the updated environment. Known original START
launchers are refreshed; user-edited files are left alone. The install folder is
remembered locally for this purpose. Older installs discover only their own
Python launch chain's location once; other process paths or command lines are not read.

Updates verify SHA256, package identity, archive paths and size, install into an
isolated environment, and require a ready tray before takeover. API limits fall
back to GitHub's public release/checksum downloads with the same verification.
Current and previous runtimes are kept. Failed updates keep the installed app.

Advanced commands: `update --check`, `update --rollback` (stop the app first),
`config set auto_update false`. Rollback also disables automatic checks.
