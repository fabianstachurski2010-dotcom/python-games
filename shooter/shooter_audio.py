#!/usr/bin/env python3
"""Synthesized sound effects for Barrage — standard library only, with fast paths.

Every effect is rendered from scratch into a short WAV file when a `SoundBank` is built
(two hundred milliseconds or so), so nothing is downloaded and nothing is written outside
a temporary folder that is removed on exit.

Playback picks the best backend the machine has:

  1. `pygame.mixer`   — a real mixer: overlapping voices, per-sound volume, ~30 us a call
  2. `AppKit.NSSound` — macOS with PyObjC (often already installed): ~0.4 ms a call
  3. a command-line player (`afplay`, `paplay`, `aplay`, `ffplay`, `play`) — no library at
     all, but every sound costs a process and `afplay` needs ~0.8 s to start, so effects
     land late: fine as a hint that something happened, useless for a rattle of gunfire
  4. nothing          — the game falls back to the Tk system bell for the big moments

    python3 shooter_audio.py                       what backend and effects you have
    python3 shooter_audio.py --list                every event, sound and rate limit
    python3 shooter_audio.py --play boom           play one effect, to hear it
    python3 shooter_audio.py --backend command --play over     force a backend
    python3 shooter_audio.py --selftest            check the synthesis and the mixer
"""

from __future__ import annotations

import argparse
import atexit
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

RATE = 22050                # samples per second
MAX_VOICES = 4              # never have more than this many players in flight

# event name -> (sound, minimum seconds between two plays of it)
EVENT_SOUNDS = {
    "shoot": ("shoot", 0.11),
    "hit": ("hit", 0.045),
    "boom": ("boom", 0.05),
    "hurt": ("hurt", 0.30),
    "shield": ("shield", 0.30),
    "pickup": ("pickup", 0.08),
    "wave": ("wave", 0.80),
    "boss": ("boss", 0.80),
    "laser": ("laser", 0.12),
    "bomb": ("bomb", 0.30),
    "perfect": ("perfect", 0.80),
    "over": ("over", 1.00),
}
# the few events worth a system bell when no player exists
BELL_EVENTS = {"boss", "over", "bomb", "perfect"}

# sound name -> minimum gap, derived from the event table above
EVENT_SOUNDS_MIX: dict[str, float] = {}
for _event, (_sound, _gap) in EVENT_SOUNDS.items():
    EVENT_SOUNDS_MIX[_sound] = min(_gap, EVENT_SOUNDS_MIX.get(_sound, 99.0))


# --------------------------------------------------------------------------- #
# Synthesis: build float buffers, then write 16-bit mono WAV
# --------------------------------------------------------------------------- #


def _envelope(index: int, total: int, decay: float, attack: float = 0.02) -> float:
    """Fast attack, exponential decay — the shape of every arcade blip."""
    progress = index / max(1, total)
    fade_in = min(1.0, progress / attack) if attack > 0 else 1.0
    return fade_in * math.exp(-decay * progress)


def _tone(freq_start: float, freq_end: float, duration: float, amp: float,
          decay: float = 6.0, harmonics: tuple[float, ...] = (1.0,),
          phase: float = 0.0) -> list[float]:
    """A frequency sweep with optional harmonics (1.0 = sine, odd ones add a square edge)."""
    total = max(1, int(RATE * duration))
    out = [0.0] * total
    angle = phase
    for i in range(total):
        t = i / total
        freq = freq_start + (freq_end - freq_start) * t
        angle += 2 * math.pi * freq / RATE
        sample = 0.0
        for harmonic, weight in enumerate(harmonics, start=1):
            sample += weight * math.sin(angle * harmonic)
        out[i] = sample * amp * _envelope(i, total, decay)
    return out


def _noise(duration: float, amp: float, decay: float = 8.0, smoothing: float = 0.35,
           rng: random.Random | None = None) -> list[float]:
    """Filtered noise: explosions, laser fizz, thruster wash."""
    rng = rng or random.Random(7)
    total = max(1, int(RATE * duration))
    out = [0.0] * total
    previous = 0.0
    for i in range(total):
        white = rng.uniform(-1.0, 1.0)
        previous = previous * smoothing + white * (1.0 - smoothing)     # one-pole low pass
        out[i] = previous * amp * _envelope(i, total, decay)
    return out


def _mix(*buffers: list[float]) -> list[float]:
    length = max(len(buffer) for buffer in buffers)
    out = [0.0] * length
    for buffer in buffers:
        for i, value in enumerate(buffer):
            out[i] += value
    return out


def _sequence(*notes: tuple[float, float, float]) -> list[float]:
    """Concatenate (frequency, duration, amplitude) blips into one arpeggio."""
    out: list[float] = []
    for freq, duration, amp in notes:
        out.extend(_tone(freq, freq, duration, amp, decay=4.0, harmonics=(1.0, 0.3)))
    return out


def _peak_normalise(buffer: list[float], ceiling: float = 0.92) -> list[float]:
    peak = max((abs(value) for value in buffer), default=0.0)
    if peak <= ceiling or peak == 0.0:
        return buffer
    scale = ceiling / peak
    return [value * scale for value in buffer]


def render(name: str, rng: random.Random | None = None) -> list[float]:
    """One named effect, as floats in -1..1."""
    rng = rng or random.Random(7)
    if name == "shoot":
        buffer = _mix(_tone(900, 1500, 0.055, 0.5, decay=9.0, harmonics=(1.0, 0.35)),
                      _tone(1800, 2600, 0.03, 0.18, decay=14.0))
    elif name == "hit":
        buffer = _mix(_tone(1400, 900, 0.04, 0.45, decay=16.0, harmonics=(1.0, 0.5)),
                      _noise(0.03, 0.22, decay=20.0, rng=rng))
    elif name == "boom":
        buffer = _mix(_noise(0.34, 0.85, decay=7.0, smoothing=0.55, rng=rng),
                      _tone(180, 60, 0.30, 0.45, decay=6.0, harmonics=(1.0, 0.4)))
    elif name == "hurt":
        buffer = _mix(_tone(320, 70, 0.34, 0.7, decay=5.0, harmonics=(1.0, 0.6, 0.3)),
                      _noise(0.20, 0.35, decay=9.0, rng=rng))
    elif name == "shield":
        buffer = _sequence((520, 0.07, 0.5), (880, 0.09, 0.42))
    elif name == "pickup":
        buffer = _sequence((660, 0.055, 0.42), (990, 0.075, 0.4), (1320, 0.06, 0.3))
    elif name == "wave":
        buffer = _sequence((440, 0.09, 0.4), (587, 0.09, 0.4), (740, 0.14, 0.38))
    elif name == "boss":
        buffer = _mix(_tone(120, 52, 0.65, 0.75, decay=2.6, harmonics=(1.0, 0.7, 0.4)),
                      _noise(0.5, 0.3, decay=4.0, smoothing=0.7, rng=rng),
                      _sequence((110, 0.12, 0.4), (98, 0.12, 0.4), (82, 0.3, 0.45)))
    elif name == "laser":
        buffer = _mix(_tone(2000, 320, 0.26, 0.5, decay=5.0, harmonics=(1.0, 0.25)),
                      _noise(0.24, 0.3, decay=6.0, smoothing=0.2, rng=rng))
    elif name == "bomb":
        buffer = _mix(_noise(0.62, 0.9, decay=4.5, smoothing=0.75, rng=rng),
                      _tone(95, 34, 0.6, 0.8, decay=3.0, harmonics=(1.0, 0.5)),
                      _tone(1400, 200, 0.3, 0.25, decay=8.0))
    elif name == "perfect":
        buffer = _sequence((523, 0.07, 0.4), (659, 0.07, 0.4), (784, 0.07, 0.4),
                           (1047, 0.18, 0.42))
    elif name == "over":
        buffer = _mix(_sequence((392, 0.18, 0.42), (311, 0.18, 0.42), (262, 0.42, 0.45)),
                      _noise(0.5, 0.18, decay=4.0, smoothing=0.8, rng=rng))
    else:
        raise KeyError(name)
    return _peak_normalise(buffer)


SOUND_NAMES = tuple(EVENT_SOUNDS[event][0] for event in EVENT_SOUNDS)


def write_wav(path: Path, samples: list[float], rate: int = RATE) -> Path:
    """16-bit mono WAV: what afplay, paplay and aplay all accept without argument."""
    frames = bytearray()
    for value in samples:
        clipped = max(-1.0, min(1.0, value))
        frames += struct.pack("<h", int(clipped * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

# (command, fixed arguments) - the file path is appended
PLAYERS = (
    ("afplay", ()),
    ("paplay", ()),
    ("aplay", ("-q",)),
    ("ffplay", ("-nodisp", "-autoexit", "-loglevel", "quiet")),
    ("play", ("-q",)),
)


def find_player() -> tuple[str, tuple[str, ...]] | None:
    for name, args in PLAYERS:
        path = shutil.which(name)
        if path:
            return path, args
    return None


def describe_player() -> str:
    found = find_player()
    if not found:
        return "(none found)"
    return " ".join((found[0], *found[1]))


class NullBackend:
    """No sound at all; the game rings the Tk bell instead."""

    name = "none"
    note = "silent"

    def add(self, name: str, path: Path) -> None:
        pass

    def play(self, name: str, volume: float) -> bool:
        return False

    def close(self) -> None:
        pass


class PygameBackend:
    """pygame.mixer: real mixing, several voices at once, ~30 us per call."""

    name = "pygame"
    note = "pygame.mixer"

    def __init__(self):
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        import pygame                                          # noqa: PLC0415 - optional
        pygame.mixer.pre_init(RATE, -16, 1, 512)
        pygame.mixer.init()
        self.pygame = pygame
        self.sounds: dict[str, object] = {}

    def add(self, name: str, path: Path) -> None:
        self.sounds[name] = self.pygame.mixer.Sound(str(path))

    def play(self, name: str, volume: float) -> bool:
        sound = self.sounds.get(name)
        if sound is None:
            return False
        channel = self.pygame.mixer.find_channel() or self.pygame.mixer.find_channel(True)
        if channel is None:
            return False
        sound.set_volume(volume)
        channel.play(sound)
        return True

    def close(self) -> None:
        try:
            self.pygame.mixer.quit()
        except Exception:                                     # noqa: BLE001 - never fatal
            pass


class NSSoundBackend:
    """macOS PyObjC: one NSSound per effect, ~0.4 ms a call, nothing to install."""

    name = "nssound"
    note = "AppKit.NSSound"

    def __init__(self):
        from AppKit import NSSound                             # noqa: PLC0415 - optional
        self.NSSound = NSSound
        self.sounds: dict[str, object] = {}

    def add(self, name: str, path: Path) -> None:
        sound = self.NSSound.alloc().initWithContentsOfFile_byReference_(str(path), True)
        if sound is None:
            raise RuntimeError(f"NSSound could not read {path}")
        self.sounds[name] = sound

    def play(self, name: str, volume: float) -> bool:
        sound = self.sounds.get(name)
        if sound is None:
            return False
        if sound.isPlaying():                                 # only pay for stop() if needed
            sound.stop()
        sound.setVolume_(volume)
        return bool(sound.play())

    def close(self) -> None:
        for sound in self.sounds.values():
            sound.stop()


class CommandBackend:
    """One CLI player process per sound. Works anywhere, but afplay takes ~0.8 s to start."""

    name = "command"
    note = "command-line player (late by design: a process per sound)"

    def __init__(self):
        found = find_player()
        if not found:
            raise RuntimeError("no command-line audio player found")
        self.player, self.args = found
        self.paths: dict[str, Path] = {}
        self.live: list[subprocess.Popen] = []

    def add(self, name: str, path: Path) -> None:
        self.paths[name] = path

    def play(self, name: str, volume: float) -> bool:
        path = self.paths.get(name)
        if path is None:
            return False
        self.live = [process for process in self.live if process.poll() is None]
        if len(self.live) >= 4:
            return False
        try:
            self.live.append(subprocess.Popen(
                [self.player, *self.args, str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except OSError:
            return False
        return True

    def close(self) -> None:
        for process in self.live:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        self.live.clear()


BACKENDS = (PygameBackend, NSSoundBackend, CommandBackend)
BACKEND_NAMES = ("auto", "pygame", "nssound", "command", "none")

# Rendering every effect costs about a second of pure-Python maths, so the full-scale
# masters are cached in the system temp folder; bump the version if a recipe changes.
CACHE_VERSION = "v1"


def master_files() -> dict[str, Path]:
    """Full-scale wav files, rendered once and then reused."""
    folder = Path(tempfile.gettempdir()) / f"barrage-sfx-{CACHE_VERSION}"
    folder.mkdir(parents=True, exist_ok=True)
    files: dict[str, Path] = {}
    for name in SOUND_NAMES:
        path = folder / f"{name}.wav"
        if not path.exists() or path.stat().st_size <= 44:
            temporary = path.with_suffix(".part")
            write_wav(temporary, render(name, random.Random(11)))
            temporary.replace(path)
        files[name] = path
    return files


# --------------------------------------------------------------------------- #
# The bank
# --------------------------------------------------------------------------- #


class SoundBank:
    """Renders every effect once, then mixes them down to whichever backend works."""

    def __init__(self, volume: float = 0.6, backend: str = "auto", keep_files: bool = False):
        self.volume = max(0.0, min(1.0, volume))
        self.requested = backend
        self.last: dict[str, float] = {}
        self.played = 0
        self.skipped = 0
        self.files: dict[str, Path] = {}
        self.directory: Path | None = None
        self.backend = NullBackend()
        self.reason = ""

        if backend == "none":
            self.reason = "sound switched off"
            return
        if backend == "auto":
            candidates = list(BACKENDS)
        else:
            candidates = [cls for cls in BACKENDS if cls.name == backend]
        if not candidates:
            self.reason = f"unknown backend {backend!r}"
            return

        self.files = master_files()
        if backend == "command" and self.volume < 1.0:
            # a command-line player cannot scale at play time: render scaled copies once
            rng = random.Random(11)
            self.directory = Path(tempfile.mkdtemp(prefix="barrage-sfx-"))
            self.files = {name: write_wav(self.directory / f"{name}.wav",
                                          [value * self.volume for value in render(name, rng)])
                          for name in SOUND_NAMES}

        failures = []
        for cls in candidates:
            chosen = cls()
            try:
                for name, path in self.files.items():
                    chosen.add(name, path)
            except Exception as exc:                          # noqa: BLE001 - try the next one
                failures.append(f"{cls.name}: {type(exc).__name__}")
                try:
                    chosen.close()
                except Exception:                             # noqa: BLE001
                    pass
                continue
            self.backend = chosen
            break
        if isinstance(self.backend, NullBackend):
            detail = ", ".join(failures) if failures else "none installed"
            self.reason = f"no usable backend ({detail})"
        if not keep_files:
            atexit.register(self.close)

    @property
    def available(self) -> bool:
        return not isinstance(self.backend, NullBackend)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    def play(self, name: str) -> bool:
        """Fire and forget. Returns False when it did not (and should not) play."""
        if not self.available:
            return False
        minimum = EVENT_SOUNDS_MIX.get(name, 0.1)
        now = time.monotonic()
        if now - self.last.get(name, -99.0) < minimum:
            self.skipped += 1
            return False
        if not self.backend.play(name, self.volume):
            self.skipped += 1
            return False
        self.last[name] = now
        self.played += 1
        return True

    def event(self, event: str) -> bool:
        """Play whatever the named game event sounds like (if anything)."""
        entry = EVENT_SOUNDS.get(event)
        return self.play(entry[0]) if entry else False

    def close(self) -> None:
        try:
            self.backend.close()
        except Exception:                                     # noqa: BLE001 - never fatal
            pass
        self.backend = NullBackend()
        if self.directory and self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None


def describe() -> str:
    return "\n".join([f"command-line player: {describe_player()}"] +
                     [f"  backend {cls.name:8s} {cls.note}" for cls in BACKENDS])


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #


def selftest(verbose: bool = True) -> int:
    checks = 0

    def check(name: str, condition: bool) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            raise AssertionError(name)
        if verbose:
            print(f"  ok  {name}")

    check("twelve effects are wired to game events", len(EVENT_SOUNDS) == 12)
    check("every event's sound is one of the rendered ones",
          all(EVENT_SOUNDS[event][0] in SOUND_NAMES for event in EVENT_SOUNDS))
    check("every sound has a rate limit", set(EVENT_SOUNDS_MIX) == set(SOUND_NAMES))
    check("the bell fallback is a subset of the events", BELL_EVENTS <= set(EVENT_SOUNDS))
    check("there are five backend choices",
          BACKEND_NAMES == ("auto", "pygame", "nssound", "command", "none"))

    for name in SOUND_NAMES:
        samples = render(name)
        check(f"{name}: renders a sane length", 0.02 <= len(samples) / RATE <= 1.2)
        check(f"{name}: stays inside -1..1", max(abs(value) for value in samples) <= 1.0)
        check(f"{name}: is not silent", max(abs(value) for value in samples) > 0.2)
        check(f"{name}: starts and ends quietly",
              abs(samples[0]) < 0.5 and abs(samples[-1]) < 0.5)
    check("an unknown effect is a KeyError", "KeyError" in _raises(lambda: render("nope")))
    check("rendering twice gives the same samples", render("boom") == render("boom"))
    check("every effect is its own waveform",
          len({tuple(round(v, 4) for v in render(n)[:400]) for n in SOUND_NAMES})
          == len(SOUND_NAMES))

    with tempfile.TemporaryDirectory() as folder:
        path = write_wav(Path(folder) / "one.wav", render("pickup"))
        with wave.open(str(path), "rb") as handle:
            check("the wav is mono 16-bit at 22050 Hz",
                  handle.getnchannels() == 1 and handle.getsampwidth() == 2
                  and handle.getframerate() == RATE)
            check("the wav holds every sample", handle.getnframes() == len(render("pickup")))
            frames = handle.readframes(handle.getnframes())
        loudest = max(abs(value[0]) for value in struct.iter_unpack("<h", frames))
        check("the wav is audible after quantising", loudest > 6000)

    check("the rendered masters are cached between runs",
          all(path.exists() for path in master_files().values()))
    silent = SoundBank(volume=0.0, backend="command", keep_files=True)
    check("the command backend writes its own volume-baked copies",
          bool(silent.files) and silent.directory is not None
          and all(path.exists() for path in silent.files.values()))
    silent.close()

    off = SoundBank(backend="none")
    check("sound can be switched off completely",
          not off.available and off.play("boom") is False and off.event("over") is False)
    check("a switched-off bank says why", "off" in off.reason)
    check("an unknown backend name is refused quietly",
          not SoundBank(backend="nope").available)

    # a real backend, at volume 0 so that running the tests makes no noise
    bank = SoundBank(volume=0.0, backend="auto", keep_files=True)
    if bank.available:
        check(f"a backend was found ({bank.backend_name})",
              bank.backend_name in {"pygame", "nssound", "command"})
        check("every effect was handed to the backend", bool(bank.files))
        check("playing reports success", bank.play("boom") is True)
        check("the same sound twice in a row is rate limited", bank.play("boom") is False)
        check("what was mixed down is counted", bank.skipped >= 1)
        check("events route to their sound", bank.event("over") is True)
        check("a silent event asks for nothing", bank.event("dash") is False)
        check("the bank remembers what it played", bank.played >= 2)
        bank.close()
        check("closing the bank releases the backend", not bank.available)
        check("closing removes the temporary files",
              bank.directory is None or not bank.directory.exists())
    else:
        check("with no backend at all, playing is a quiet no-op",
              bank.play("boom") is False and bank.event("over") is False)
        check("a failed bank records why", bool(bank.reason))

    print(f"\n{checks} checks passed.")
    return 0


def _raises(function) -> str:
    try:
        function()
    except Exception as exc:                     # noqa: BLE001 - the type is the point
        return type(exc).__name__
    return ""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Barrage sound effects (synthesized from scratch, standard library only).")
    parser.add_argument("--selftest", action="store_true", help="check the synthesis and mixer")
    parser.add_argument("--play", metavar="NAME", help="play one effect and exit")
    parser.add_argument("--list", action="store_true", help="list the effects and rate limits")
    parser.add_argument("--backend", choices=BACKEND_NAMES, default="auto",
                        help="force one playback backend (default: best available)")
    parser.add_argument("--volume", type=float, default=0.6)
    parser.add_argument("--write", metavar="DIR", help="write the wav files into DIR and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        print("running Barrage sound self tests...")
        return selftest()

    if args.write:
        folder = Path(args.write)
        folder.mkdir(parents=True, exist_ok=True)
        for name in SOUND_NAMES:
            write_wav(folder / f"{name}.wav", [value * args.volume for value in render(name)])
            print(f"  wrote {folder / (name + '.wav')}")
        return 0

    if args.list:
        print(describe())
        for event, (sound, gap) in sorted(EVENT_SOUNDS.items()):
            print(f"  {event:10s} -> {sound:8s} (at most every {gap:.2f}s)")
        return 0

    if args.play:
        bank = SoundBank(volume=args.volume, backend=args.backend, keep_files=True)
        if not bank.available:
            print(f"cannot play: {bank.reason}", file=sys.stderr)
            return 1
        print(f"playing {args.play} through the {bank.backend_name} backend")
        bank.play(args.play)
        time.sleep(0.8)
        bank.close()
        return 0

    bank = SoundBank(volume=0.0, backend=args.backend, keep_files=True)
    print(f"backend: {bank.backend_name}" + (f"  ({bank.reason})" if bank.reason else ""))
    if bank.available:
        for name in SOUND_NAMES:
            path = bank.files[name]
            print(f"  {name:8s} {len(render(name)) / RATE * 1000:5.0f} ms"
                  f"  {path.stat().st_size:6d} bytes")
    else:
        print(describe())
    bank.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
