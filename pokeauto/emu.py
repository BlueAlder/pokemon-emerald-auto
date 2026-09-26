"""Emulator backends.

Everything above this module talks to one small interface:

    run(keys, frames)      hold `keys` (a GBA key mask) for `frames` frames
    read(addr, n)          read n bytes of the GBA address space
    save_state()/load_state(blob)
    screenshot(path)

Two implementations:

* ``HeadlessEmu`` runs mGBA's core in-process through stable-retro. It is
  unthrottled (~45x real time on an M-series Mac), savestates are in-memory
  blobs, and reads are direct. This is the backend for actually finishing the
  game -- a 20+ hour playthrough becomes well under an hour.
* ``MgbaEmu`` drives the mGBA desktop app through ``lua/bridge.lua`` so a human
  can watch at normal speed. Same semantics, much slower.

ROM reads (0x08xxxxxx) are always served from the ROM file on disk when we have
it: it is immutable, and that avoids a bridge round trip per table lookup.
"""

from __future__ import annotations

import socket
import struct
from pathlib import Path

# Canonical GBA key bits (hardware KEYINPUT order, also mGBA's).
KEY_BITS = {
    "A": 0, "B": 1, "SELECT": 2, "START": 3,
    "RIGHT": 4, "LEFT": 5, "UP": 6, "DOWN": 7,
    "R": 8, "L": 9,
}


def keymask(*buttons: str) -> int:
    m = 0
    for b in buttons:
        m |= 1 << KEY_BITS[b.upper()]
    return m


ROM_BASE = 0x08000000


class Emu:
    """Shared helpers. Subclasses implement run/_read_ram/save_state/load_state."""

    rom: bytes | None = None
    frame: int = 0

    # -- subclass API --------------------------------------------------------
    def run(self, keys: int, frames: int) -> None:
        raise NotImplementedError

    def _read_ram(self, addr: int, n: int) -> bytes:
        raise NotImplementedError

    def save_state(self) -> bytes:
        raise NotImplementedError

    def load_state(self, blob: bytes) -> None:
        raise NotImplementedError

    def screenshot(self, path: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # -- reads ---------------------------------------------------------------
    def read(self, addr: int, n: int) -> bytes:
        if self.rom is not None and ROM_BASE <= addr < ROM_BASE + len(self.rom):
            o = addr - ROM_BASE
            return self.rom[o:o + n]
        return self._read_ram(addr, n)

    def u8(self, addr: int) -> int:
        return self.read(addr, 1)[0]

    def u16(self, addr: int) -> int:
        return struct.unpack("<H", self.read(addr, 2))[0]

    def s16(self, addr: int) -> int:
        return struct.unpack("<h", self.read(addr, 2))[0]

    def u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read(addr, 4))[0]

    # -- input ---------------------------------------------------------------
    def idle(self, frames: int) -> None:
        self.run(0, frames)

    def press(self, *buttons: str, hold: int = 4, release: int = 4) -> None:
        """Tap buttons: hold for `hold` frames, then release for `release`."""
        self.run(keymask(*buttons), hold)
        if release:
            self.run(0, release)


# ---------------------------------------------------------------------------
# Headless (stable-retro / libretro mGBA core)
# ---------------------------------------------------------------------------

# stable-retro's GbAdvance button order.
_RETRO_ORDER = ["B", None, "SELECT", "START", "UP", "DOWN", "LEFT", "RIGHT",
                "A", None, "L", "R"]


class HeadlessEmu(Emu):
    def __init__(self, rom_path: str | Path):
        import numpy as np
        import stable_retro as retro

        self._np = np
        self.rom = Path(rom_path).read_bytes()
        self.em = retro.RetroEmulator(str(rom_path))
        self.data = retro.data.GameData()
        self.em.configure_data(self.data)
        self._mask_cache: dict[int, object] = {}
        self.frame = 0
        self.on_frames = None     # optional hook(emu) called after each run()
        # memory.blocks hands out a fresh copy of a whole block (256 KB of
        # EWRAM) per access: keep one per block until the next frame runs.
        self._blocks: dict[int, bytes] = {}

    def _retro_mask(self, keys: int):
        arr = self._mask_cache.get(keys)
        if arr is None:
            arr = self._np.zeros(16, dtype=self._np.uint8)
            for i, name in enumerate(_RETRO_ORDER):
                if name and keys & (1 << KEY_BITS[name]):
                    arr[i] = 1
            self._mask_cache[keys] = arr
        return arr

    def run(self, keys: int, frames: int) -> None:
        mask = self._retro_mask(keys)
        for _ in range(frames):
            self.em.set_button_mask(mask, 0)
            self.em.step()
        self.frame += frames
        self._blocks.clear()
        if self.on_frames:
            self.on_frames(self)

    def _read_ram(self, addr: int, n: int) -> bytes:
        base = addr & 0xFF000000
        block = self._blocks.get(base)
        if block is None:
            block = self.data.memory.blocks.get(base)
            if block is None:
                return b"\x00" * n
            self._blocks[base] = block
        o = addr - base
        return bytes(block[o:o + n])

    def save_state(self) -> bytes:
        return bytes(self.em.get_state())

    def load_state(self, blob: bytes) -> None:
        self.em.set_state(blob)
        self._blocks.clear()

    def screen(self):
        return self.em.get_screen()

    def screenshot(self, path: str) -> None:
        from PIL import Image
        Image.fromarray(self.em.get_screen()).save(path)


# ---------------------------------------------------------------------------
# mGBA desktop app via lua/bridge.lua
# ---------------------------------------------------------------------------

class BridgeError(RuntimeError):
    pass


class MgbaEmu(Emu):
    """Client for lua/bridge.lua. `run` blocks until the frames have elapsed."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8888,
                 rom_path: str | Path | None = None, timeout: float = 60.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise BridgeError(
                f"could not reach the mGBA bridge on {host}:{port}. Open the ROM in "
                "mGBA and load lua/bridge.lua (Tools > Scripting > File > Load script)"
            ) from exc
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass        # macOS: EINVAL when the peer already hung up; INFO says why
        self._buf = b""
        self.on_frames = None     # optional hook(emu) called after each run()
        self.rom = Path(rom_path).read_bytes() if rom_path else None
        try:
            info = self._cmd("INFO").split(" ", 3)
        except (OSError, BridgeError) as exc:
            raise BridgeError(
                f"the mGBA bridge on {host}:{port} dropped the connection ({exc}). Is a ROM "
                "running in mGBA, and lua/bridge.lua loaded? If mGBA was restarted, load the "
                "script again (Tools > Scripting > File > Load script)") from exc
        self.game_code = info[0]
        self.frame = int(info[2])
        caps = self._cmd("CAPS").split()
        if "RUN" not in caps or "LOCK" not in caps:
            raise BridgeError("lua/bridge.lua is outdated: restart mGBA and load it again")
        # Lockstep: mGBA only advances inside RUN, as the headless core does.
        # Without it the game keeps running (fast-forward: several frames per
        # round trip) while we read RAM and decide, and walks go off course.
        self._cmd("LOCK 1")

    def _cmd(self, line: str) -> str:
        self.sock.sendall(line.encode() + b"\n")
        while b"\n" not in self._buf:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise BridgeError("bridge closed the connection")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        reply = raw.decode("ascii", "replace").strip()
        if reply.startswith("ERR"):
            raise BridgeError(f"{line!r} -> {reply}")
        return reply

    def run(self, keys: int, frames: int) -> None:
        reply = self._cmd(f"RUN {keys} {frames}")
        self.frame = int(reply.split()[1])
        if self.on_frames:
            self.on_frames(self)

    def _read_ram(self, addr: int, n: int) -> bytes:
        return bytes.fromhex(self._cmd(f"READ {addr:08x} {n}"))

    def save_state(self) -> bytes:
        # mGBA's Lua API saves to slots, not buffers; slot 9 is ours.
        self._cmd("STATE 9")
        return b"slot:9"

    def load_state(self, blob: bytes) -> None:
        self._cmd(f"LOAD {int(blob.decode().split(':')[1])}")

    def screenshot(self, path: str) -> None:
        self._cmd(f"SHOT {path}")

    def close(self) -> None:
        try:
            self._cmd("LOCK 0")          # let the game run freely again
        except (OSError, BridgeError):
            pass
        self.sock.close()
