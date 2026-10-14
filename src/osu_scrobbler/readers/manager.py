"""Process discovery and independent reader lifecycle, with bounded retries."""

import os
import time
from .memory import ReaderUnavailable, WindowsMemory
from .stable import StableReader
from .lazer import LazerReader


def processes():
    if os.name != 'nt':
        raise ReaderUnavailable('Live game reading requires Windows')
    import psutil
    result = []
    try:
        for process in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if (process.info['name'] or '').casefold() in {'osu!.exe', 'osu.exe', 'osulazer.exe'}:
                    if process.info['create_time'] is not None:
                        result.append((process.pid, process.info['create_time']))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    finally:
        psutil.process_iter.cache_clear()
    return sorted(result)


class ReaderManager:
    def __init__(self, discover=processes, memory_factory=WindowsMemory, clock=time.monotonic):
        self.discover, self.memory_factory, self.clock = discover, memory_factory, clock
        self.readers, self.retry_at = {}, {}
        self.errors = {}
        self.selected = None

    def close(self):
        for memory, _ in self.readers.values():
            memory.close()
        self.readers.clear()

    def read(self):
        identities = self.discover()
        for identity in list(self.readers):
            if identity not in identities:
                self.readers.pop(identity)[0].close()
        self.retry_at = {key: value for key, value in self.retry_at.items() if key in identities}
        self.errors = {key: value for key, value in self.errors.items() if key in identities}
        samples, errors = [], []
        for identity in identities:
            if self.clock() < self.retry_at.get(identity, 0):
                errors.append(self.errors.get(identity, 'Waiting for a supported game layout'))
                continue
            memory = None
            try:
                if identity not in self.readers:
                    memory = self.memory_factory(identity[0])
                    reader = StableReader(memory) if memory.pointer_size == 4 else LazerReader(memory)
                    self.readers[identity] = memory, reader
                samples.append((identity, self.readers[identity][1].read()))
                self.errors.pop(identity, None)
            except (ReaderUnavailable, OSError) as error:
                if identity in self.readers:
                    self.readers.pop(identity)[0].close()
                elif memory is not None:
                    memory.close()
                self.retry_at[identity] = self.clock() + 5
                errors.append(str(error))
                self.errors[identity] = str(error)
        if not samples:
            raise ReaderUnavailable(errors[0] if errors else 'Waiting for osu! / a supported game layout')
        # Prefer personal gameplay, otherwise retain the selected process. This
        # avoids alternating maps each poll when both clients are open.
        samples.sort(key=lambda pair: (not (pair[1].state == 2 and pair[1].personal), pair[0] != self.selected, pair[0]))
        self.selected, sample = samples[0]
        return sample
