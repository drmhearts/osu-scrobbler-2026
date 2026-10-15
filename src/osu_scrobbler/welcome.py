"""One quiet welcome screen."""

from . import __version__
from .terminal import color

REIMU = r'''
         /\_><_/\
        /  /__\  \
       |  /o  o\  |
       |  \ u  /  |
       |__/'--'\__|
        /| >o< |\
       (_| /\  |_)
         /____\
        /_/  \_\
'''.strip('\n')


def commands(*, background=False):
    return '\n'.join([
        'i info  v details  s status  f force  t timer',
        'e export  a auto-export  r Last.fm  d Discogs  b startup  u update',
        ('q hide' if background else 'q quit') + '  x stop',
    ])


def welcome(directory, *, background=False):
    return "\n".join([
        color(REIMU, "91"), color(f"osu! life hacks {__version__}", "95"),
        "", "Open osu! and listen. Last.fm setup is remembered.",
        commands(background=background), '', '©Hky Sigh, 2026',
    ])
