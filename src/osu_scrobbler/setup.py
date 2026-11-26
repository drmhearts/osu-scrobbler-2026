"""Interactive first-run setup followed by the detachable welcome terminal."""

import asyncio
import getpass
import json
from pathlib import Path
import subprocess
import webbrowser

from .console import show_console
from .lastfm import CredentialStore
from .osu_api import OsuApiStore, public_token
from .terminal import color, enable_colors
from .tray import install_startup, launch_tray, require_windows, startup_installed, wait_for_tray, tray_health
from .updater import startup, stop_running
from . import __version__


def setup_complete(directory):
    try:
        state = json.loads((directory / "setup-state.json").read_text("utf-8"))
        return isinstance(state, dict) and state.get("complete") is True
    except (OSError, ValueError):
        return False


async def choice(prompt):
    while True:
        value = (await asyncio.to_thread(input, prompt)).strip().casefold()
        if value in {"c", "connect"}:
            return "connect"
        if value in {"s", "skip", "", "k", "keep"}:
            return "skip"
        print("Type c to connect or s to skip.")


async def connect_osu_api():
    print("Create an osu! OAuth application at https://osu.ppy.sh/home/account/edit")
    print("Use its Client ID and Client Secret. This public API connection reads map metadata;")
    print("osu! does not allow third-party API credentials to download archives.")
    if not webbrowser.open("https://osu.ppy.sh/home/account/edit"):
        print("Open the address above in your usual browser to create the application.")
    while True:
        value = (await asyncio.to_thread(input, "osu! Client ID: ")).strip()
        if value.isdecimal() and int(value) > 0:
            client_id = int(value)
            break
        print("Enter the positive Client ID shown in your osu! OAuth application.")
    secret = await asyncio.to_thread(getpass.getpass, "osu! Client Secret (hidden): ")
    await public_token(client_id, secret)
    OsuApiStore().save(client_id, secret)
    print(color("osu! public API connected. Client credentials saved in the OS vault.", "92"))


async def setup_services(directory):
    enable_colors()
    print("\n" + color("osu! life hacks  ·  first-time setup", "95"))
    print("Connect either service now, or skip and connect later.")
    print("The osu! public API helps with map information; Last.fm sends scrobbles.")
    print("Full beatmap downloads still require an osu! website session.\n")
    connected_osu = OsuApiStore().load() is not None
    print("osu! API: " + ("connected" if connected_osu else "not connected"))
    answer = await choice("Connect osu! API? [c] connect  [s] skip (default: skip): ")
    if answer == "connect":
        await connect_osu_api()
    else:
        print("osu! API skipped. Use setup later to connect it.")
    credentials = CredentialStore().load()
    connected_lastfm = bool(credentials and credentials.session_key)
    print("\nLast.fm: " + ("connected" if connected_lastfm else "not connected"))
    answer = await choice("Connect Last.fm? [c] connect  [s] skip (default: skip): ")
    if answer == "connect":
        from .cli import authorize
        await authorize(directory)
        print(color("Last.fm connected.", "92"))
    else:
        print("Last.fm skipped. Scrobbles will wait until you connect with r or setup.")


async def first_run(directory: Path, *, enable_startup=False, check_updates=True, repeat=False):
    require_windows()
    directory.mkdir(parents=True, exist_ok=True)
    new_install = not setup_complete(directory)
    if repeat or new_install:
        await setup_services(directory)
    registered = startup_installed(directory)
    if new_install and not enable_startup and not registered:
        while True:
            answer = (await asyncio.to_thread(input, "Start with Windows? Type yes or no [default: no]; type help for details: ")).strip().casefold()
            if answer in {"help", "help setup", "help tray"}:
                from .help import help_text
                print(help_text("tray"))
            elif answer in {"", "n", "no", "y", "yes"}:
                enable_startup = answer in {"y", "yes"}
                break
            else:
                print("Type yes or no. Press Enter for no.")
    if enable_startup or registered:
        install_startup(directory)
    # Choices may deliberately skip either service; remember the completed wizard.
    marker = directory / "setup-state.json"
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"complete": True}) + "\n", "utf-8")
    temporary.replace(marker)
    # Check/install before launching the hidden process, keeping progress and
    # errors visible and keeping long downloads outside the readiness timeout.
    if check_updates and await startup(directory, command="setup", enable_startup=enable_startup, output=print):
        return
    try:
        running = await tray_health(directory)
    except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
        running = None
    if running and running.get("version") != __version__:
        await stop_running(directory)
    process = launch_tray(directory, skip_update_check=True)
    await wait_for_tray(directory, process)
    # Clear installer output only on success; preserve error messages on failure.
    subprocess.run(["cmd.exe", "/d", "/c", "cls"], check=False)
    print("Ready. The terminal stays open; q hides it in the tray.")
    await show_console(directory)


async def launch(directory: Path, *, skip_update_check=False):
    """START opens setup only when needed; all later launches stay in the tray."""
    require_windows()
    if not setup_complete(directory):
        from .tray import python_executable
        process = await asyncio.create_subprocess_exec(python_executable(), "-m", "osu_scrobbler",
            "--data-dir", str(directory.resolve()), "setup", creationflags=subprocess.CREATE_NEW_CONSOLE)
        if await process.wait():
            raise RuntimeError("Setup did not finish. Reopen START.cmd to retry.")
        return
    # Updates stay hidden. Network failures keep the installed app.
    if await startup(directory, command="tray", skip=skip_update_check):
        return
    try:
        health = await tray_health(directory)
    except (RuntimeError, OSError, ValueError, asyncio.TimeoutError):
        health = None
    if health and health.get("version") == __version__ and health.get("ready"):
        return
    if health:
        await stop_running(directory)
    process = launch_tray(directory, skip_update_check=True)
    await wait_for_tray(directory, process)
