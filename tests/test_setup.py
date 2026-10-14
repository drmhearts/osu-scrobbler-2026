import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from osu_scrobbler.lastfm import Credentials
from osu_scrobbler.setup import first_run


@pytest.mark.parametrize("choice", ["y", ""])
async def test_first_click_authorizes_once_respects_startup_choice_and_then_clears_to_welcome(tmp_path, monkeypatch, capsys, choice):
    import osu_scrobbler.setup as setup
    import osu_scrobbler.cli as cli
    events = []
    stored = [None]
    registered = [False]
    credentials = Credentials("private-api-key", "private-api-secret", "private-session")
    monkeypatch.setattr(setup, "require_windows", lambda: None)
    monkeypatch.setattr(setup, "CredentialStore", lambda: SimpleNamespace(load=lambda: stored[0]))
    monkeypatch.setattr(setup, "startup_installed", lambda _: registered[0])
    async def authorize(directory):
        events.append("authorize")
        stored[0] = credentials
    def ask(prompt):
        events.append("ask")
        return choice
    def install(directory):
        events.append("register")
        registered[0] = True
    def clear(command, **options):
        assert command == ["cmd.exe", "/d", "/c", "cls"]
        events.append("clear")
    async def show(directory):
        assert stored[0] == credentials
        assert (directory / "setup-state.json").exists()
        events.append("welcome")
    monkeypatch.setattr(cli, "authorize", authorize)
    monkeypatch.setattr("builtins.input", ask)
    monkeypatch.setattr(setup, "install_startup", install)
    monkeypatch.setattr(setup, "launch_tray", lambda _: events.append("launch"))
    monkeypatch.setattr(setup.subprocess, "run", clear)
    monkeypatch.setattr(setup, "show_console", show)
    await first_run(tmp_path)
    assert events == ["authorize", "ask"] + (["register"] if choice else []) + ["launch", "clear", "welcome"]
    events.clear()
    await first_run(tmp_path)
    assert "authorize" not in events and "ask" not in events
    assert events[-3:] == ["launch", "clear", "welcome"]
    assert registered[0] is bool(choice)
    output = capsys.readouterr().out
    for secret in (credentials.api_key, credentials.api_secret, credentials.session_key):
        assert secret not in output
        assert secret not in (tmp_path / "setup-state.json").read_text()


async def test_failed_authorization_preserves_errors_and_does_not_launch_or_mark_complete(tmp_path, monkeypatch):
    import osu_scrobbler.setup as setup
    import osu_scrobbler.cli as cli
    monkeypatch.setattr(setup, "require_windows", lambda: None)
    monkeypatch.setattr(setup, "CredentialStore", lambda: SimpleNamespace(load=lambda: None))
    async def failed(directory):
        raise RuntimeError("Authorization unavailable")
    def forbidden(*args, **kwargs):
        pytest.fail("Failed setup must not clear errors, enable startup or launch the app")
    monkeypatch.setattr(cli, "authorize", failed)
    monkeypatch.setattr(setup, "install_startup", forbidden)
    monkeypatch.setattr(setup, "launch_tray", forbidden)
    monkeypatch.setattr(setup.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="Authorization unavailable"):
        await first_run(tmp_path)
    assert not (tmp_path / "setup-state.json").exists()


def bootstrap_module():
    path = Path(__file__).resolve().parents[1] / "tools/start_windows.py"
    spec = importlib.util.spec_from_file_location("start_windows", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_installs_once_and_repairs_changed_requirements_without_manual_commands(tmp_path):
    module = bootstrap_module()
    (tmp_path / ".venv").mkdir()
    project = tmp_path / "pyproject.toml"
    project.write_text("original requirements")
    commands = []
    def run(command, **options):
        commands.append(command)
        return SimpleNamespace(returncode=0)
    module.prepare(tmp_path, runner=run)
    assert len(commands) == 2 and "pip" in commands[0]
    assert "-e" in commands[0] and str(tmp_path) in commands[0]
    assert (tmp_path / ".venv/scrobbler-installed.sha256").exists()
    commands.clear()
    module.prepare(tmp_path, runner=run)
    assert len(commands) == 1 and "pip" not in commands[0]
    project.write_text("updated requirements")
    commands.clear()
    module.prepare(tmp_path, runner=run)
    assert len(commands) == 2 and "pip" in commands[0]


def test_bootstrap_does_not_mark_a_failed_install_as_ready(tmp_path):
    module = bootstrap_module()
    (tmp_path / ".venv").mkdir()
    (tmp_path / "pyproject.toml").write_text("requirements")
    def failed(command, **options):
        raise subprocess.CalledProcessError(1, command)
    with pytest.raises(subprocess.CalledProcessError):
        module.prepare(tmp_path, runner=failed)
    assert not (tmp_path / ".venv/scrobbler-installed.sha256").exists()
