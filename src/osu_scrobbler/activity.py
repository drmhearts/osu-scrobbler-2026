"""Short-lived terminal messages, held only in memory by the tray process."""

from collections import deque
import secrets
import time


class ActivityBuffer:
    def __init__(self, *, limit=50, ttl=300, clock=time.monotonic):
        self.events = deque(maxlen=limit)
        self.ttl = ttl
        self.clock = clock
        self.sequence = 0
        self.session = secrets.token_hex(8)  # Local, per-run cursor namespace; never saved or sent online.

    def expire(self):
        cutoff = self.clock() - self.ttl
        while self.events and self.events[0][1] <= cutoff:
            self.events.popleft()

    def append(self, message):
        self.expire()
        self.sequence += 1
        self.events.append((self.sequence, self.clock(), str(message)[:4096]))

    def read(self, after=0):
        self.expire()
        return {"session": self.session, "cursor": self.sequence,
                "events": [message for number, _, message in self.events if number > after]}

    def clear(self):
        self.events.clear()
