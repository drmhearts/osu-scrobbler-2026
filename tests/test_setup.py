import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from osu_scrobbler.lastfm import Credentials
from osu_scrobbler.setup import first_run


@pytest.fixture(autouse=True)
def no_live_update_check(monkeypatch):
    async def unchanged(*args, **kwargs):
        return False
    monkeypatch.setattr("osu_scrobbler.setup.startup", unchanged)
    monkeypatch.setattr("osu_scrobbler.setup.asyncio.sleep", unchanged)


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
    monkeypatch.setattr(setup, "launch_tray", lambda _, **kwargs: events.append("launch"))
    async def ready(directory, process):
        events.append("ready")
    monkeypatch.setattr(setup, "wait_for_tray", ready)
    monkeypatch.setattr(setup.subprocess, "run", clear)
    monkeypatch.setattr(setup, "show_console", show)
    await first_run(tmp_path)
    assert events == ["authorize", "ask"] + (["register"] if choice else []) + ["launch", "ready", "clear"] + ([] if choice else ["welcome"])
    events.clear()
    await first_run(tmp_path)
    assert "authorize" not in events and "ask" not in events
    expected = ["launch", "ready", "clear"] + ([] if choice else ["welcome"])
    assert events[-len(expected):] == expected
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


async def test_failed_tray_start_does_not_clear_error_or_open_dead_console(tmp_path, monkeypatch):
    import osu_scrobbler.setup as setup
    monkeypatch.setattr(setup, "require_windows", lambda: None)
    credentials = Credentials("key", "secret", "session")
    monkeypatch.setattr(setup, "CredentialStore", lambda: SimpleNamespace(load=lambda: credentials))
    monkeypatch.setattr(setup, "startup_installed", lambda _: True)
    monkeypatch.setattr(setup, "install_startup", lambda _: None)
    monkeypatch.setattr(setup, "launch_tray", lambda _, **kwargs: None)
    async def failed(*args):
        raise RuntimeError("Background tray did not become ready")
    monkeypatch.setattr(setup, "wait_for_tray", failed)
    monkeypatch.setattr(setup.subprocess, "run", lambda *args, **kwargs: pytest.fail("Do not clear failed setup"))
    async def forbidden(*args):
        pytest.fail("Do not open a console for a failed launch")
    monkeypatch.setattr(setup, "show_console", forbidden)
    with pytest.raises(RuntimeError, match="did not become ready"):
        await first_run(tmp_path)


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


async def test_installed_update_replaces_older_running_backend_before_showing_terminal(tmp_path, monkeypatch):
    import osu_scrobbler.setup as setup
    events = []
    monkeypatch.setattr(setup, 'require_windows', lambda: None)
    monkeypatch.setattr(setup, 'CredentialStore', lambda: SimpleNamespace(load=lambda: Credentials('key', 'secret', 'session')))
    monkeypatch.setattr(setup, 'startup_installed', lambda _: True)
    monkeypatch.setattr(setup, 'install_startup', lambda _: None)
    async def health(_):
        return {'ready': True, 'version': '0.2.7'}
    async def stopped(_):
        events.append('stop old')
    async def ready(*args):
        events.append('new ready')
    async def shown(*args):
        events.append('new view')
    monkeypatch.setattr(setup, 'tray_health', health)
    monkeypatch.setattr(setup, 'stop_running', stopped)
    monkeypatch.setattr(setup, 'launch_tray', lambda *args, **kwargs: events.append('launch new'))
    monkeypatch.setattr(setup, 'wait_for_tray', ready)
    monkeypatch.setattr(setup, 'show_console', shown)
    monkeypatch.setattr(setup.subprocess, 'run', lambda *args, **kwargs: None)
    await first_run(tmp_path, check_updates=False)
    assert events == ['stop old', 'launch new', 'new ready']


async def test_returning_start_stays_in_tray_without_prompts_browser_or_terminal(tmp_path, monkeypatch):
    import osu_scrobbler.setup as setup
    import osu_scrobbler.cli as cli
    (tmp_path / "setup-state.json").write_text('{"complete":true}')
    monkeypatch.setattr(setup, "require_windows", lambda: None)
    monkeypatch.setattr(setup, "CredentialStore", lambda: SimpleNamespace(load=lambda: Credentials("key", "secret", "session")))
    async def health(_):
        return {"ready": True, "version": setup.__version__}
    def forbidden(*args, **kwargs):
        pytest.fail("Returning START must not prompt, authorize, or spawn another terminal/tray")
    monkeypatch.setattr(setup, "tray_health", health)
    monkeypatch.setattr(setup, "launch_tray", forbidden)
    monkeypatch.setattr(setup.asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(cli, "authorize", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    await setup.launch(tmp_path)


async def test_first_launch_opens_setup_and_returning_launch_starts_hidden_tray(tmp_path, monkeypatch):
    import osu_scrobbler.setup as setup
    calls = []
    stored = [None]
    monkeypatch.setattr(setup, "require_windows", lambda: None)
    monkeypatch.setattr(setup, "CredentialStore", lambda: SimpleNamespace(load=lambda: stored[0]))
    monkeypatch.setattr("osu_scrobbler.tray.python_executable", lambda: "python.exe")
    monkeypatch.setattr(setup.subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        async def done():
            (tmp_path / "setup-state.json").write_text('{"complete":true}')
            stored[0] = Credentials("key", "secret", "session")
            return 0
        return SimpleNamespace(wait=done)
    monkeypatch.setattr(setup.asyncio, "create_subprocess_exec", spawn)
    await setup.launch(tmp_path)
    assert calls[0][0][-1] == "setup" and calls[0][1]["creationflags"] == 16
    async def absent(*args):
        return None
    async def ready(*args):
        calls.append("ready")
    monkeypatch.setattr(setup, "tray_health", absent)
    monkeypatch.setattr(setup, "launch_tray", lambda *args, **kwargs: calls.append("hidden tray"))
    monkeypatch.setattr(setup, "wait_for_tray", ready)
    await setup.launch(tmp_path)
    assert calls[1:] == ["hidden tray", "ready"]


def test_hidden_launch_errors_do_not_open_message_boxes(tmp_path, monkeypatch):
    import ctypes
    import osu_scrobbler.cli as cli
    async def failed(*args, **kwargs):
        raise RuntimeError("Local startup failed")
    def popup(*args):
        pytest.fail("Background errors must not create popups")
    monkeypatch.setattr(cli, "execute", failed)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(MessageBoxW=popup)), raising=False)
    with monkeypatch.context() as context:
        context.setattr(cli.sys, "stdout", None)
        context.setattr(cli.sys, "stderr", None)
        result = cli.main(["--data-dir", str(tmp_path), "launch"])
    assert result == 1
