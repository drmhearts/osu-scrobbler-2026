"""32-bit Stable CLR layout. Signatures/offset assumptions: docs/telemetry.md."""

from .beatmap import contained_path, last_object
from .memory import ReaderUnavailable
from .state import Sample


SIGNATURES = {
    'map': ('F8 01 74 04 83 65', -0x0c),
    'state': ('48 83 F8 04 73 1E', -4),
    'time': ('5E 5F 5D C3 A1 ?? ?? ?? ?? 89 ?? 04', 5),
    'audio': ('55 8B EC 83 EC 08 A1 ?? ?? ?? ?? 85 C0', 7),
    'ruleset': ('7D 15 A1 ?? ?? ?? ?? 85 C0', -0x0b),
    'replay': ('55 8B EC 80 3D ?? ?? ?? ?? 00 75 26 80 3D', 0x46),
    'spectator': ('8B 0D ?? ?? ?? ?? 85 C0 74 05 8B 50 30', -4),
    'settings': ('83 E0 20 85 C0 7E 2F', 8),
}


class StableReader:
    def __init__(self, memory):
        if memory.pointer_size != 4:
            raise ReaderUnavailable('Stable requires the 32-bit game layout')
        self.memory = memory
        self.addresses = {name: memory.unique(pattern) + offset for name, (pattern, offset) in SIGNATURES.items()}
        self.selection = None
        self.endpoint = None

    def read(self):
        m, a = self.memory, self.addresses
        state = m.i32(m.ptr(a['state']))
        selected = m.chain(a['map'], 0, 0)
        position = m.i32(m.ptr(a['time']))
        data = {key: m.text(selected + offset) for key, offset in {
            'artist': 0x18, 'artistUnicode': 0x1c, 'title': 0x24,
            'titleUnicode': 0x28, 'checksum': 0x6c, 'version': 0xac}.items()}
        data.update(id=m.i32(selected + 0xc8), set=m.i32(selected + 0xcc))
        duration = m.f64(m.chain(a['audio'], 0, 0) + 4)
        if data['checksum'] != self.selection:
            self.endpoint = None
            try:
                folder = m.string(m.chain(a['settings'], 0, 0xb8, 4))
                path = contained_path(folder, m.text(selected + 0x78), m.text(selected + 0x90))
                self.endpoint = last_object(path, data['checksum'])
            except (ReaderUnavailable, OSError, ValueError):
                pass  # Metadata and the audio-length threshold remain usable.
            self.selection = data['checksum']
        data['time'] = {'mp3Length': duration, 'lastObject': self.endpoint}
        personal, failed = False, False
        if state == 2:
            replay = m.boolean(m.ptr(a['replay']))
            spectator = m.chain(a['spectator'], 0)
            # A null spectator reference is positive evidence of personal play.
            watching = m.ptr(spectator) != 0
            gameplay = m.chain(a['ruleset'], 0, 4, 0x64)
            health = m.f64(m.chain(gameplay, 0x40) + 0x1c)
            if not 0 <= health <= 200:
                raise ReaderUnavailable('Stable health layout is invalid')
            failed, personal = health <= 0, not replay and not watching
        # Reject a map/state switch during the multi-field read (GC/transitions).
        if selected != m.chain(a['map'], 0, 0) or state != m.i32(m.ptr(a['state'])):
            raise ReaderUnavailable('Stable changed screens during sampling')
        return Sample('stable', state, data, position, personal, failed).validate()
