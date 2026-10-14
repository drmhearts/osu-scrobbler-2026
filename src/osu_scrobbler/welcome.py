"""A small welcome screen shared by the visible terminal modes."""

GITHUB_URL = "https://github.com/drmhearts/osu-scrobbler-2026"


def welcome(directory, *, background=False):
    title = "  osu! scrobbler  /  Your plays, on Last.fm.  "
    lines = ["╭" + "─" * len(title) + "╮", "│" + title + "│", "╰" + "─" * len(title) + "╯",
             "GitHub: " + GITHUB_URL, "",
             "Getting started: open osu! stable or lazer, select a map, and play.",
             "Last.fm setup is remembered. Scrobbles follow your listening setting.", "",
             "Commands here (press a key):",
             "  i / v  Track info / full details",
             "  f      Force the selected track",
             "  e / a  Export / toggle automatic export",
             ("  s / q  Status / hide this window" if background else "  q      Stop watching")]
    if background:
        lines.extend(["", "Open again: click the pink tray icon or double-click START.cmd.",
                      "Closing this window keeps background scrobbling running. Tray menu → Exit stops it."])
    return "\n".join(lines)
