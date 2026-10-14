"""osu!lazer x64, 2026.1005.0 managed layout. No remote offset updates.

Only fields needed for music and playback are listed here. Refuse other releases
instead of applying guessed offsets. See docs/telemetry.md for provenance/limits.
"""

import re
from .beatmap import contained_path, last_object
from .memory import ReaderUnavailable
from .state import Sample


VERSION = '2026.1005.0'
ROOT_HEADER = 7902756601856
ANCHOR = '01 01 00 00 00 00 80 44 00 00 40 44'
# Reference fields in OsuGame/OsuGameBase (byte offsets from the object pointer).
GAME = dict(api=1080, spectator=1200, score_manager=1024, clock=1240,
            beatmap=1104, storage=1088, stack=1568, logo=1608, menu=1616,
            sentry=1536)
PLAYER = dict(api=1008, score_manager=1000, submitting_api=1264,
              submitting_spectator=1272, ruleset=1112, health=1104)


class LazerReader:
    def __init__(self, memory):
        if memory.pointer_size != 8:
            raise ReaderUnavailable('Lazer requires the 64-bit game layout')
        self.memory = memory
        self.root = self._resolve()
        self.selection = None
        self.endpoint = None

    def _valid_root(self, root):
        m = self.memory
        return root and m.unpack(m.ptr(root), 'Q') == ROOT_HEADER

    def _resolve(self):
        m = self.memory
        candidates = set()
        for anchor in m.scan(ANCHOR):
            for delta in (0x24, 0x28, 0x2c, 0x20, 0x30, 0x1c, 0x34):
                try:
                    root = m.chain(anchor - delta, 0, 536, 784)
                    if self._valid_root(root):
                        # Sentry's release string, not its logs or account data.
                        release = m.string(m.chain(root, GAME['sentry'], 16, 8, 32, 184))
                        if re.search(r'(?<![\d.])' + re.escape(VERSION) + r'(?![\d.])', release):
                            candidates.add(root)
                except ReaderUnavailable:
                    continue
        if len(candidates) != 1:
            raise ReaderUnavailable(f'Lazer layout unavailable; supported release: {VERSION} (x64)')
        return candidates.pop()

    def _screen(self):
        m = self.memory
        stack = m.chain(self.root, GAME['stack'], 800)
        count = m.i32(stack + 16)
        if not 1 <= count <= 32:
            raise ReaderUnavailable('Lazer screen stack is not ready')
        return m.chain(stack, 8, 16 + 8 * (count - 1))

    def _same(self, obj, offset, root_field):
        expected = self.memory.ptr(self.root + GAME[root_field])
        return bool(expected) and self.memory.ptr(obj + offset) == expected

    def _state(self, screen):
        m = self.memory
        # Main-menu identity is explicit; unknown screens never count as music.
        if screen == m.ptr(self.root + GAME['menu']) and self._same(screen, 896, 'logo'):
            return 0, False, False
        submitting = self._same(screen, PLAYER['submitting_api'], 'api') and self._same(screen, PLAYER['submitting_spectator'], 'spectator')
        player = self._same(screen, PLAYER['api'], 'api') and self._same(screen, PLAYER['score_manager'], 'score_manager')
        if player or submitting:
            replay = m.ptr(m.chain(screen, PLAYER['ruleset']) + 808) != 0
            # SpectatorPlayer is not a SubmittingPlayer. Replays/autoplay carry
            # ReplayScore. Both must be excluded even if the clock advances.
            personal = player and submitting and not replay
            health = m.f64(m.chain(screen, PLAYER['health'], 560) + 64)
            if not 0 <= health <= 1:
                raise ReaderUnavailable('Lazer health layout is invalid')
            return 2, personal, health <= 0
        if self._same(screen, 1040, 'api') and self._same(screen, 896, 'logo'):
            return 7, False, False
        return 5, False, False  # Selection/editor/lobby/unknown: metadata only.

    def read(self):
        m, root = self.memory, self.root
        if not self._valid_root(root):
            raise ReaderUnavailable('Lazer root moved; resolving again')
        screen = self._screen()
        state, personal, failed = self._state(screen)
        working = m.chain(root, GAME['beatmap'], 32)
        info, set_info = m.chain(working, 8), m.chain(working, 16)
        metadata = m.chain(info, 48)
        data = {key: m.text(metadata + offset) for key, offset in {
            'title': 24, 'titleUnicode': 32, 'artist': 40, 'artistUnicode': 48}.items()}
        data.update(checksum=m.text(info + 88), version=m.text(info + 24),
                    id=m.i32(info + 140), set=m.i32(set_info + 48))
        clock = m.chain(root, GAME['clock'])
        position = m.f64(m.chain(clock, 528) + 48)
        duration = m.f64(m.chain(clock, 552, 24) + 72)
        if data['checksum'] != self.selection:
            self.endpoint = None
            try:
                base = m.string(m.chain(root, GAME['storage'], 16, 8))
                file_hash = m.text(info + 80)
                if re.fullmatch('[a-fA-F0-9]{64}', file_hash):
                    path = contained_path(base, 'files', file_hash[0], file_hash[:2], file_hash)
                    self.endpoint = last_object(path, data['checksum'])
            except (ReaderUnavailable, OSError, ValueError):
                pass
            self.selection = data['checksum']
        data['time'] = {'mp3Length': duration, 'lastObject': self.endpoint}
        if screen != self._screen() or working != m.chain(root, GAME['beatmap'], 32) or not self._valid_root(root):
            raise ReaderUnavailable('Lazer changed objects during sampling')
        return Sample('lazer', state, data, position, personal, failed).validate()
