"""Synthetic managed heaps, not captured player memory or live compatibility claims."""

import asyncio
import ctypes
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct

import pytest

from osu_scrobbler.config import Config
from osu_scrobbler.models import Phase, Track
from osu_scrobbler.playback import PlaybackEngine
from osu_scrobbler.readers.memory import Memory, ReaderUnavailable, WindowsMemory
from osu_scrobbler.readers.stable import StableReader, SIGNATURES
from osu_scrobbler.readers.lazer import LazerReader, GAME, ROOT_HEADER
from osu_scrobbler.readers.manager import ReaderManager
from osu_scrobbler.readers.state import Sample
from osu_scrobbler.readers.beatmap import last_object, contained_path
from osu_scrobbler.telemetry import InternalAdapter
from osu_scrobbler.welcome import welcome


class Heap(Memory):
    def __init__(self, width):
        self.pointer_size = width
        self.data = bytearray(3 * 1024 * 1024)
        self.cursor = 0x20000
        self.closed = False

    def read(self, address, size):
        if address < 0x10000 or address + size > 0x10000 + len(self.data):
            raise ReaderUnavailable('Unmapped fixture memory')
        return bytes(self.data[address - 0x10000:address - 0x10000 + size])

    def write(self, address, fmt, value):
        struct.pack_into('<' + fmt, self.data, address - 0x10000, value)

    def pointer(self, address, value):
        self.write(address, 'I' if self.pointer_size == 4 else 'Q', value)

    def allocate(self, size=2048):
        address = self.cursor
        self.cursor += size
        return address

    def link(self, address, offset=0):
        child = self.allocate()
        self.pointer(address + offset, child)
        return child

    def put_text(self, address, value):
        obj = self.link(address)
        encoded = value.encode('utf-16-le')
        self.write(obj + self.pointer_size, 'i', len(encoded) // 2)
        start = obj + self.pointer_size + 4 - 0x10000
        self.data[start:start + len(encoded)] = encoded
        return obj

    def regions(self, **options):
        yield 0x10000, len(self.data)

    def close(self):
        self.closed = True


def stable_heap():
    m = Heap(4)
    locations = {}
    for i, (name, (pattern, offset)) in enumerate(SIGNATURES.items()):
        anchor = 0x11000 + i * 0x100
        encoded = bytes.fromhex(pattern.replace('??', '11'))
        m.data[anchor - 0x10000:anchor - 0x10000 + len(encoded)] = encoded
        locations[name] = anchor + offset
    selected = m.link(m.link(locations['map']))
    state, position = m.link(locations['state']), m.link(locations['time'])
    m.write(state, 'i', 2)
    m.write(position, 'i', 0)
    audio = m.link(m.link(locations['audio']))
    m.write(audio + 4, 'd', 120000)
    for offset, value in {0x18: 'Artist', 0x1c: '歌手', 0x24: 'Song', 0x28: '曲', 0x6c: 'a' * 32, 0xac: 'Hard'}.items():
        m.put_text(selected + offset, value)
    m.write(selected + 0xc8, 'i', 123)
    m.write(selected + 0xcc, 'i', 456)
    replay = m.link(locations['replay'])
    spectator = m.link(locations['spectator'])
    ruleset = m.link(m.link(locations['ruleset']), 4)
    gameplay = m.link(ruleset, 0x64)
    health = m.link(gameplay, 0x40)
    m.write(health + 0x1c, 'd', 150)
    return m, dict(map=selected, state=state, time=position, replay=replay, spectator=spectator, health=health + 0x1c)


def lazer_heap():
    m = Heap(8)
    anchor = 0x11000
    m.data[anchor - 0x10000:anchor - 0x10000 + 12] = bytes.fromhex('01 01 00 00 00 00 80 44 00 00 40 44')
    external = m.link(anchor - 0x28)
    api = m.link(external, 536)
    root = m.link(api, 784)
    m.write(m.link(root), 'Q', ROOT_HEADER)
    m.pointer(root + GAME['api'], api)
    m.link(root, GAME['spectator'])
    m.link(root, GAME['score_manager'])
    m.link(root, GAME['logo'])
    options = m.link(m.link(m.link(m.link(root, GAME['sentry']), 16), 8), 32)
    release = m.put_text(options + 184, 'osu@2026.1005.0')
    screen_stack = m.link(m.link(root, GAME['stack']), 800)
    m.write(screen_stack + 16, 'i', 1)
    items = m.link(screen_stack, 8)
    screen = m.link(items, 16)
    m.pointer(screen + 1264, api)
    m.pointer(screen + 1272, m.ptr(root + GAME['spectator']))
    m.pointer(screen + 1008, api)
    m.pointer(screen + 1000, m.ptr(root + GAME['score_manager']))
    ruleset = m.link(screen, 1112)
    health = m.link(m.link(screen, 1104), 560)
    m.write(health + 64, 'd', 1)
    working = m.link(m.link(root, GAME['beatmap']), 32)
    info, set_info = m.link(working, 8), m.link(working, 16)
    metadata = m.link(info, 48)
    for offset, value in {24: 'Song', 32: '曲', 40: 'Artist', 48: '歌手'}.items():
        m.put_text(metadata + offset, value)
    m.put_text(info + 88, 'b' * 32)
    m.put_text(info + 24, 'Insane')
    m.write(info + 140, 'i', 789)
    m.write(set_info + 48, 'i', 456)
    clock = m.link(root, GAME['clock'])
    time = m.link(clock, 528) + 48
    m.write(time, 'd', 0)
    audio = m.link(m.link(clock, 552), 24)
    m.write(audio + 72, 'd', 120000)
    return m, dict(root=root, screen=screen, time=time, replay=ruleset + 808, health=health + 64, release=release)


@pytest.mark.parametrize('factory,reader', [(stable_heap, StableReader), (lazer_heap, LazerReader)])
def test_actual_pointer_chains_decode_music_and_personal_play(factory, reader):
    memory, addresses = factory()
    sample = reader(memory).read()
    assert sample.beatmap['titleUnicode'] == '曲'
    assert sample.beatmap['artistUnicode'] == '歌手'
    assert sample.beatmap['time']['mp3Length'] == 120000
    assert sample.personal and not sample.failed and sample.position_ms == 0
    assert 'score' not in sample.beatmap and 'audio_path' not in sample.beatmap
    memory.write(addresses['health'], 'd', 0)
    assert reader(memory).read().failed


def test_stable_replay_spectator_unknown_flags_and_layout_loss():
    memory, a = stable_heap()
    reader = StableReader(memory)
    memory.write(a['replay'], 'B', 1)
    assert not reader.read().personal
    memory.write(a['replay'], 'B', 0)
    memory.pointer(a['spectator'], 0x22222)
    assert not reader.read().personal
    memory.write(a['replay'], 'B', 2)
    with pytest.raises(ReaderUnavailable):
        reader.read()
    memory.data[0x1000:0x1006] = b'\0' * 6
    with pytest.raises(ReaderUnavailable, match='signature'):
        StableReader(memory)


def test_lazer_replay_and_spectator_are_excluded_and_menu_is_explicit():
    memory, a = lazer_heap()
    reader = LazerReader(memory)
    memory.pointer(a['replay'], 0x22222)
    assert not reader.read().personal
    memory.pointer(a['replay'], 0)
    # Spectator is a base Player but has no SubmittingPlayer identity pair.
    memory.pointer(a['screen'] + 1264, 0)
    memory.pointer(a['screen'] + 1008, memory.ptr(a['root'] + GAME['api']))
    memory.pointer(a['screen'] + 1000, memory.ptr(a['root'] + GAME['score_manager']))
    assert not reader.read().personal
    memory.pointer(a['root'] + GAME['menu'], a['screen'])
    memory.pointer(a['screen'] + 896, memory.ptr(a['root'] + GAME['logo']))
    assert reader.read().state == 0
    memory.pointer(a['root'] + GAME['menu'], 0)
    memory.pointer(a['screen'] + 1008, 0)
    assert reader.read().state == 5  # Unknown screen is never main-menu listening.


def test_lazer_unsupported_version_and_relocated_root_are_rejected():
    memory, a = lazer_heap()
    reader = LazerReader(memory)
    memory.pointer(a['root'], 0)
    with pytest.raises(ReaderUnavailable):
        reader.read()
    memory, a = lazer_heap()
    offset = a['release'] + 12 - 0x10000
    memory.data[offset:offset + 4] = 'xx'.encode('utf-16-le')
    # Also replace the version, not just the product prefix.
    memory.data[offset + 8:offset + 16] = '1999'.encode('utf-16-le')
    with pytest.raises(ReaderUnavailable, match='supported release'):
        LazerReader(memory)


@pytest.mark.parametrize('width', [4, 8])
def test_managed_strings_are_bounded_and_unicode_safe(width):
    memory = Heap(width)
    address = memory.allocate()
    obj = memory.put_text(address, '曲 🎵')
    assert memory.text(address) == '曲 🎵'
    memory.write(obj + width, 'i', 10000000)
    with pytest.raises(ReaderUnavailable, match='length'):
        memory.text(address)


def test_signature_scan_handles_chunk_boundary_and_ambiguity():
    memory = Heap(4)
    memory.data[1024 * 1024 - 2:1024 * 1024 + 2] = bytes.fromhex('DE AD BE EF')
    assert memory.unique('DE AD ?? EF') == 0x10000 + 1024 * 1024 - 2
    memory.data[0:4] = bytes.fromhex('DE AD 00 EF')
    with pytest.raises(ReaderUnavailable, match='ambiguous'):
        memory.unique('DE AD ?? EF')


@pytest.mark.skipif(os.name != 'nt', reason='Windows ReadProcessMemory integration')
def test_native_read_only_backend_reads_and_closes_a_real_handle():
    buffer = ctypes.create_string_buffer(b'local fixture')
    memory = WindowsMemory(os.getpid())
    try:
        assert memory.read(ctypes.addressof(buffer), 13) == b'local fixture'
        assert any(base <= ctypes.addressof(buffer) < base + size for base, size in memory.regions())
    finally:
        memory.close()
    assert memory.handle is None


def sample(client='stable', state=2, position=0, **options):
    return Sample(client, state, {'title': 'Song', 'artist': 'Artist', 'checksum': 'a' * 32,
                                 'time': {'mp3Length': 120000, 'lastObject': 100000}}, position,
                  options.get('personal', True), options.get('failed', False))


@pytest.mark.parametrize('client', ['stable', 'lazer'])
def test_live_clock_pause_seek_retry_and_long_gaps_never_inflate_time(client):
    adapter = InternalAdapter(manager=object())
    engine = PlaybackEngine()
    def observe(at, position):
        event = adapter.observe(sample(client, position=position), monotonic=at)
        engine.update(event, at, 1700000000 + at)
        return event
    observe(0, 0)
    assert observe(.1, 100).phase == Phase.PLAYING
    observe(.2, 100)
    assert engine.phase == Phase.PAUSED
    assert engine.active_seconds == pytest.approx(.1)
    observe(.3, 10000)  # Seek.
    assert engine.active_seconds == pytest.approx(.1)
    observe(.4, 10100)
    observe(10, 20000)  # Long scheduling gap.
    assert engine.active_seconds < 1
    old = engine.attempt_id
    observe(10.1, 0)
    assert engine.attempt_id != old and engine.active_seconds == 0


@pytest.mark.parametrize('client', ['stable', 'lazer'])
def test_internal_menu_and_gameplay_reach_existing_threshold(client):
    for state in (0, 2):
        adapter, engine = InternalAdapter(manager=object()), PlaybackEngine()
        submitted = None
        for tick in range(602):
            at = tick / 10
            engine.update(adapter.observe(sample(client, state, tick * 100), monotonic=at), at, 1700000000 + at)
            if engine.track is None:
                engine.bind_track(Track.fallback(engine.beatmap))
            submitted = engine.next_submission(at, 1700000000 + at)
            if submitted:
                break
        assert submitted is not None and at >= 60


def test_manager_retries_independently_closes_handles_and_survives_pid_reuse():
    identities, clock, heaps = [(10, 1.0), (20, 1.0)], [0], []
    def factory(pid):
        memory, _ = stable_heap() if pid == 10 else lazer_heap()
        if pid == 20:
            memory.data[0x1000:0x100c] = b'\0' * 12
        heaps.append(memory)
        return memory
    manager = ReaderManager(lambda: identities, factory, lambda: clock[0])
    try:
        assert manager.read().client == 'stable'
        assert heaps[1].closed
        manager.read()
        assert len(heaps) == 2
        identities[:] = [(10, 2.0)]
        assert manager.read().client == 'stable'
        assert heaps[0].closed and manager.selected == (10, 2.0)
        identities.clear()
        with pytest.raises(ReaderUnavailable):
            manager.read()
        assert heaps[-1].closed
    finally:
        manager.close()


async def test_stream_recovers_without_network_or_orphan_handles():
    class Manager:
        selected = (1, 1)
        def __init__(self):
            self.count, self.closed = 0, False
        def read(self):
            self.count += 1
            if self.count == 2:
                raise ReaderUnavailable('Layout lost')
            return sample(position=0)
        def close(self):
            self.closed = True
    manager = Manager()
    messages = []
    stream = InternalAdapter(manager=manager, output=messages.append, poll_interval=.001).stream()
    first = await anext(stream)
    assert first.phase == Phase.PLAYING
    assert (await anext(stream)).phase == Phase.DISCONNECTED
    assert (await anext(stream)).connected
    await stream.aclose()
    assert manager.closed and messages == ['Layout lost']


def test_selected_file_timing_checks_checksum_and_slider_spinner_hold_ends(tmp_path):
    content = b'[Difficulty]\nSliderMultiplier:1.4\n[TimingPoints]\n0,500,4,0,0,100,1,0\n[HitObjects]\n0,0,1000,2,0,B|0:0,2,140\n0,0,3000,8,0,4000\n0,0,5000,128,0,6000:0:0:0:0\n'
    path = tmp_path / 'map.osu'
    path.write_bytes(content)
    assert last_object(path, hashlib.md5(content).hexdigest()) == 6000
    assert last_object(path, '0' * 32) is None
    with pytest.raises(ValueError):
        contained_path(tmp_path, '../outside.osu')


def test_old_config_migrates_without_transport_settings_or_losing_preferences(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps({'tosu_url': 'ws://localhost:24050/websocket/v2', 'tosu_path': 'old.exe', 'auto_update': False}), 'utf-8')
    config = Config.load(tmp_path)
    assert not config.auto_update
    config.save(tmp_path)
    assert 'tosu' not in (tmp_path / 'config.json').read_text('utf-8')


def test_welcome_has_every_control_exact_attribution_and_no_source_link(tmp_path):
    text = welcome(tmp_path, background=True)
    for key in ('i info', 'v details', 's status', 'f force', 't timer', 'e export', 'a auto-export', 'r Last.fm', 'b startup', 'u update', 'q hide', 'x stop'):
        assert key in text
    assert text.endswith('©Hky Sigh, 2026') and 'osu! life hacks' in text
    assert all(word not in text.lower() for word in ('github', 'reimu', '? more', 'osu! scrobbler'))
