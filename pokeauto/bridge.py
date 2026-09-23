"""TCP client for the mGBA Lua bridge (see lua/bridge.lua).

Deliberately thin: it moves bytes and button presses. All Pokemon knowledge
lives in memory.py / observer.py.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

# GBA key bit indices, from mGBA's C.GBA_KEY table.
KEY_BITS = {
    "A": 0, "B": 1, "SELECT": 2, "START": 3,
    "RIGHT": 4, "LEFT": 5, "UP": 6, "DOWN": 7,
    "R": 8, "L": 9,
}
BUTTONS = tuple(KEY_BITS)


def mask_for(*buttons: str) -> int:
    m = 0
    for b in buttons:
        key = b.upper()
        if key not in KEY_BITS:
            raise ValueError(f"unknown button {b!r}; expected one of {BUTTONS}")
        m |= 1 << KEY_BITS[key]
    return m


class BridgeError(RuntimeError):
    pass


@dataclass
class RomInfo:
    game_code: str
    title: str
    rom_size: int
    frame: int

    @property
    def short_code(self) -> str:
        """Bare 4-character product code.

        mGBA reports the full header form ("AGB-BPEE"); the 4-character tail
        ("BPEE") is what identifies the game.
        """
        return self.game_code.rsplit("-", 1)[-1]


class Bridge:
    """Synchronous line-protocol client. One command, one reply."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8888, timeout: float = 10.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    # -- connection --------------------------------------------------------

    def connect(self, retries: int = 1, delay: float = 1.0) -> "Bridge":
        last: Exception | None = None
        for attempt in range(retries):
            try:
                s = socket.create_connection((self.host, self.port), timeout=self.timeout)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock, self._buf = s, b""
                return self
            except OSError as exc:  # emulator not up yet, or script not loaded
                last = exc
                if attempt + 1 < retries:
                    time.sleep(delay)
        raise BridgeError(
            f"could not reach the mGBA bridge on {self.host}:{self.port}. "
            "Is mGBA running with lua/bridge.lua loaded (Tools > Scripting...)?"
        ) from last

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "Bridge":
        return self.connect(retries=1) if self._sock is None else self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- protocol ----------------------------------------------------------

    def _command(self, line: str) -> str:
        if self._sock is None:
            raise BridgeError("bridge is not connected; call connect() first")
        self._sock.sendall(line.encode("ascii") + b"\n")
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise BridgeError("bridge closed the connection")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        reply = raw.decode("ascii", "replace").strip()
        if reply.startswith("ERR"):
            raise BridgeError(f"{line!r} -> {reply}")
        return reply

    # -- queries -----------------------------------------------------------

    def ping(self) -> bool:
        return self._command("PING") == "PONG"

    def info(self) -> RomInfo:
        # Title is last and may contain spaces ("POKEMON EMER").
        code, size, frame, title = self._command("INFO").split(" ", 3)
        return RomInfo(code, title, int(size), int(frame))

    def frame(self) -> int:
        return int(self._command("FRAME"))

    def read(self, addr: int, length: int) -> bytes:
        return bytes.fromhex(self._command(f"READ {addr:08x} {length}"))

    def read_many(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        """Fetch several memory ranges in one round trip.

        The round trip dominates cost here, so batching the ~8 reads that make
        up a game state snapshot is worth roughly 8x on observation latency.
        """
        if not ranges:
            return []
        spec = ",".join(f"{a:08x}:{n}" for a, n in ranges)
        parts = self._command(f"READM {spec}").split("|")
        if len(parts) != len(ranges):
            raise BridgeError(f"expected {len(ranges)} ranges, got {len(parts)}")
        return [bytes.fromhex(p) if p else b"" for p in parts]

    # -- input -------------------------------------------------------------

    def press(self, *buttons: str, hold: int = 8, gap: int = 12, wait: bool = True) -> int:
        """Queue a button press. Returns the frame at which it completes."""
        done = int(self._command(f"PRESS {mask_for(*buttons)} {hold} {gap}").split()[1])
        if wait:
            self.wait_until(done)
        return done

    def idle(self, frames: int, wait: bool = True) -> int:
        done = int(self._command(f"IDLE {frames}").split()[1])
        if wait:
            self.wait_until(done)
        return done

    def hold(self, *buttons: str, frames: int = 16, wait: bool = True) -> int:
        done = int(self._command(f"HOLD {mask_for(*buttons)} {frames}").split()[1])
        if wait:
            self.wait_until(done)
        return done

    def wait_until(self, target_frame: int, poll: float = 0.008) -> None:
        """Block until emulation passes target_frame.

        Guards against a paused emulator: if the frame counter stops moving we
        raise rather than spin forever.
        """
        stalled_since = time.monotonic()
        last = self.frame()
        while last < target_frame:
            time.sleep(poll)
            now = self.frame()
            if now != last:
                last, stalled_since = now, time.monotonic()
            elif time.monotonic() - stalled_since > 5.0:
                raise BridgeError(
                    "emulation is not advancing (is mGBA paused? check the window)"
                )

    # -- convenience -------------------------------------------------------

    def savestate(self, slot: int) -> None:
        self._command(f"STATE {slot}")

    def loadstate(self, slot: int) -> None:
        self._command(f"LOAD {slot}")

    def screenshot(self, path: str) -> None:
        self._command(f"SHOT {path}")
