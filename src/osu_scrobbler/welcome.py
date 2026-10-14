"""One quiet welcome screen."""

from . import __version__
from .terminal import color

GITHUB_URL = "https://github.com/drmhearts/osu-scrobbler-2026"
REIMU = r'''
          \__><__/
         /  /__\  \
        |  |o  o|  |
        |__ \--/ __|
          /| >< |\
         /_| /\ |_\
           /____\
          /______\
'''.strip('\n')


def welcome(directory, *, background=False):
    return "\n".join([
        color(REIMU, "91"), color(f"osu! scrobbler {__version__}  ·  Reimu", "95"), GITHUB_URL,
        "", "Open osu! and listen. Last.fm setup is remembered.",
        "i info   f force   t timer   b startup   u update   ? more",
        "q hide" if background else "q quit",
    ])
