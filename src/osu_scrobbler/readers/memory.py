"""Bounded ReadProcessMemory access; deliberately no process-write capability."""

import ctypes
from ctypes import wintypes
import math
import os
import re
import struct
import time


class ReaderUnavailable(RuntimeError):
    """The game is absent, transitioning, inaccessible, or unsupported."""


class Memory:
    pointer_size = 8

    def unpack(self, address, fmt):
        return struct.unpack('<' + fmt, self.read(address, struct.calcsize('<' + fmt)))[0]

    def ptr(self, address):
        return self.unpack(address, 'I' if self.pointer_size == 4 else 'Q')

    def i32(self, address):
        return self.unpack(address, 'i')

    def f64(self, address):
        value = self.unpack(address, 'd')
        if not math.isfinite(value):
            raise ReaderUnavailable('Invalid game clock/value')
        return value

    def boolean(self, address):
        value = self.unpack(address, 'B')
        if value not in (0, 1):
            raise ReaderUnavailable('Invalid game flag')
        return bool(value)

    def string(self, address):
        if not address:
            return ''
        length = self.i32(address + self.pointer_size)
        if not 0 <= length <= 4096:
            raise ReaderUnavailable('Invalid managed string length')
        try:
            return self.read(address + self.pointer_size + 4, length * 2).decode('utf-16-le')
        except UnicodeError as error:
            raise ReaderUnavailable('Invalid managed string') from error

    def text(self, address):
        return self.string(self.ptr(address))

    def chain(self, address, *offsets):
        for offset in offsets:
            address = self.ptr(address + offset)
            if not address:
                raise ReaderUnavailable('Game object is not ready')
        return address

    def scan(self, pattern, *, executable=False, timeout=4, limit=128):
        """Scan readable pages in bounded chunks, including cross-chunk matches."""
        parts = pattern.split()
        regex = re.compile(b''.join(b'.' if p == '??' else re.escape(bytes.fromhex(p)) for p in parts), re.DOTALL)
        deadline = time.monotonic() + timeout
        matches = []
        for base, size in self.regions(executable=executable):
            tail = b''
            for offset in range(0, size, 1024 * 1024):
                if time.monotonic() > deadline:
                    raise ReaderUnavailable('Game memory scan timed out; retrying')
                try:
                    data = self.read(base + offset, min(1024 * 1024, size - offset))
                except ReaderUnavailable:
                    tail = b''
                    continue
                chunk = tail + data
                matches.extend(base + offset - len(tail) + m.start() for m in regex.finditer(chunk))
                if len(matches) > limit:
                    raise ReaderUnavailable('Ambiguous game memory signature')
                tail = chunk[-(len(parts) - 1):] if len(parts) > 1 else b''
        return matches

    def unique(self, pattern):
        matches = self.scan(pattern, executable=True)
        if len(matches) != 1:
            raise ReaderUnavailable('Stable layout signature is missing or ambiguous')
        return matches[0]


class WindowsMemory(Memory):
    def __init__(self, pid):
        if os.name != 'nt' or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ReaderUnavailable('Game readers require 64-bit Python on Windows')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.ReadProcessMemory.restype = wintypes.BOOL
        self.kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self.kernel.VirtualQueryEx.restype = ctypes.c_size_t
        self.kernel.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.handle = self.kernel.OpenProcess(0x0400 | 0x0010, False, pid)  # query + read only
        if not self.handle:
            raise ReaderUnavailable('Cannot read osu! process; check process permissions')
        wow64 = wintypes.BOOL()
        if not self.kernel.IsWow64Process(self.handle, ctypes.byref(wow64)):
            self.close()
            raise ReaderUnavailable('Cannot determine game architecture')
        self.pointer_size = 4 if wow64.value else 8

    def read(self, address, size):
        if size == 0:
            return b''
        if not self.handle or not 0x10000 <= address < 0x7fffffffffff or not 0 < size <= 1024 * 1024:
            raise ReaderUnavailable('Invalid game memory range')
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(count)) or count.value != size:
            raise ReaderUnavailable('Game memory changed or process exited')
        return buffer.raw

    def regions(self, *, executable=False):
        class Region(ctypes.Structure):
            _fields_ = [('base', ctypes.c_void_p), ('allocation', ctypes.c_void_p),
                        ('allocation_protect', wintypes.DWORD), ('partition', wintypes.WORD),
                        ('size', ctypes.c_size_t), ('state', wintypes.DWORD),
                        ('protect', wintypes.DWORD), ('type', wintypes.DWORD)]
        address, maximum = 0, 0x100000000 if self.pointer_size == 4 else 0x7fffffffffff
        while address < maximum:
            region = Region()
            if not self.kernel.VirtualQueryEx(self.handle, address, ctypes.byref(region), ctypes.sizeof(region)):
                break
            base = region.base or 0
            if region.size == 0 or base + region.size <= address:
                break
            readable = region.protect & 0xff in {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
            if region.state == 0x1000 and readable and not region.protect & 0x100:
                if not executable or region.protect & 0xf0:
                    yield base, region.size
            address = base + region.size

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
