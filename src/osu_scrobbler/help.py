"""Short, plain-language help shared by CLI and interactive prompts."""

TOPICS = {
    "scrobble": "Scrobbling: connect Last.fm with 'auth' (or r), then open osu! and play. Main-menu music and your own gameplay count; song selection alone does not. Example: osu-life-hacks auth; osu-life-hacks watch. Use f to force the selected track.",
    "download": "Downloads: m opens maps. Choose a destination, sign in to osu! once in Edge/Chrome, set optional mode/genre/category/UTC ranked dates/count/length/mapper filters, then start. Count means full beatmapsets; a set may contain other modes. Pause, cancel and resume retain the queue. Pacing is conservative and server limits still apply.",
    "export": "Export: e or 'export [file.txt]' writes accepted scrobble history as UTF-8 text. Without a path it uses the saved export_path or the app data directory's scrobbles.txt. Use a to toggle automatic export; 'config set export_path PATH' changes its destination.",
    "collections": "Collections: choose yes when starting a maps batch, then enter an existing or new collection name. Download/import and collection steps are tracked separately. After import, add the set in the game's Manage Collections UI. Lazer and Stable do not expose a supported external collection-write interface here; this tool never edits their collection databases.",
    "auth": "Last.fm: 'auth' opens the authorization page and saves credentials in the OS vault. Use 'auth --session' only if you already have an authorized session key. The maps downloader has a separate osu! browser sign-in (m, then 6).",
    "discogs": "Discogs: d or 'discogs auth' saves an optional personal access token in the OS vault for release matching. 'discogs status' checks it; 'discogs remove' removes it. MusicBrainz and Last.fm remain available.",
    "maps": "Maps: m opens the download menu. 1 start, 2 filters, 3 pause, 4 resume, 5 cancel, 6 sign in, 7 destination, 8 import saved, 9 scan, 0 back. Type help at any prompt to keep your place.",
    "watch": "Watch: 'watch' starts one gameplay watcher; the tray starts it when osu! opens. 'watch --dry-run' previews without scrobbling. Use 'status' to check what is playing.",
    "info": "Info: i shows the selected track; v or 'info --full' shows metadata and release candidates. 'config choose N' saves a numbered candidate; 'config correct' supplies a manual correction.",
    "status": "Status: s or 'status' shows the selected song, listening timer, metadata and delivery state. 'maps status' shows a download job.",
    "force": "Force: f or 'force' submits the currently selected track immediately to Last.fm. Use only when the displayed track is correct; normal scrobbling waits for listening time.",
    "timer": "Timer: t changes the listening threshold. Enter h for half the song, f for the full song, or a positive number of seconds. 'config show' displays saved settings.",
    "config": "Config: 'config show' lists non-secret settings. 'config set KEY VALUE' saves a setting. 'config choose N', 'config correct ...', and 'config clear-correction' manage metadata for the selected track.",
    "queue": "Queue: 'queue list' shows delivery history; 'queue retry ID' retries a failed delivery. Uncertain submissions require --accept-duplicate-risk because Last.fm might already have accepted them.",
    "tray": "Tray: 'tray --install' starts the background monitor at Windows sign-in; 'tray --uninstall' disables it. b toggles startup. q hides the terminal; x stops the app.",
    "console": "Console: 'console' opens the live terminal for the background watcher. Closing or hiding it leaves the tray watcher running. Press h for help topics.",
    "setup": "Setup: double-click START.cmd in the Windows package. The first run connects Last.fm and offers Windows startup; later runs use saved settings. 'setup' repeats the setup flow.",
    "launch": "Launch: 'launch' is the package entry point. It runs first-time setup or starts the saved tray configuration quietly.",
    "privacy": "Privacy: 'privacy' describes local data and requests. 'privacy --clear-cache' clears lookup cache and older logs while retaining history and corrections; stop watching first.",
    "update": "Update: u or 'update --check' checks the published version. 'update' installs and restarts; 'update --rollback' restores the previous installation after stopping the app.",
    "quit": "Quit: x or 'quit' stops the watcher. q only hides the background console; a foreground watch session quits with q.",
}

ALIASES = {"scrobbling": "scrobble", "downloads": "download", "map": "maps", "collection": "collections",
           "e": "export", "m": "maps", "r": "auth", "d": "discogs", "b": "tray", "u": "update",
           "i": "info", "v": "info", "s": "status", "f": "force", "t": "timer", "a": "export", "x": "quit"}


def help_text(topic=""):
    topic = ALIASES.get(str(topic or "").strip().casefold(), str(topic or "").strip().casefold())
    if not topic:
        return ("Help topics: " + ", ".join(TOPICS) + "\n"
                "Use 'osu-life-hacks help TOPIC' or press h in the terminal. "
                "At a maps prompt, type 'help' or 'help TOPIC' without losing your place.")
    return TOPICS.get(topic, "Unknown help topic. " + help_text())
