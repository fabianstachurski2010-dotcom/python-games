#!/usr/bin/env python3
"""Save, index and load game records for the replay screen.

The host server already writes a record for every finished game; every other
machine receives the same record object inside ``sync`` and mirrors it here, so
each player ends up with a local, replayable copy.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import engine
import i18n

ROOT = Path(__file__).resolve().parent
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

_ZH_MONTHS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"]
_EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def records_dir() -> Path:
    """Where this machine keeps replays (override with the settings file)."""
    try:
        import settings

        custom = settings.load().get("records_dir") or ""
    except Exception:  # pragma: no cover - settings is tiny, never block the UI
        custom = ""
    if custom:
        return Path(custom).expanduser()
    return ROOT / "records"


def _record_key(record: dict) -> str:
    payload = json.dumps(
        {
            "s": record.get("started"),
            "e": record.get("ended"),
            "m": record.get("columns") or [m.get("col") for m in record.get("moves", [])],
            "p": [
                (record.get("players", {}).get("1", {}) or {}).get("name"),
                (record.get("players", {}).get("2", {}) or {}).get("name"),
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _stamp(record: dict) -> str:
    when = None
    ended = record.get("ended") or record.get("started")
    if ended:
        try:
            when = _dt.datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
        except ValueError:
            when = None
    if when is None:
        when = _dt.datetime.now()
    return when.strftime("%Y%m%d-%H%M%S")


def _safe(text: object, fallback: str) -> str:
    cleaned = _SAFE.sub("_", str(text or "")).strip("_")[:20]
    return cleaned or fallback


def _players(record: dict) -> Dict[str, dict]:
    players = record.get("players")
    if not isinstance(players, dict):
        players = {}
    out = {}
    for seat in ("1", "2"):
        info = players.get(seat)
        out[seat] = info if isinstance(info, dict) else {"name": "?", "ai": False}
    return out


def pretty_when(record: dict, lang: Optional[str] = None) -> str:
    lang = lang or i18n.get_lang()
    raw = record.get("ended") or record.get("started") or ""
    try:
        when = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return str(raw)[:16] or "—"
    months = _ZH_MONTHS if lang == "zh" else _EN_MONTHS
    month = months[when.month - 1]
    if lang == "zh":
        return f"{month}月{when.day}日 {when:%H:%M}"
    return f"{month} {when.day}, {when:%H:%M}"


def summary(record: dict) -> dict:
    """Human-facing facts about a record, including localised labels."""
    players = _players(record)
    seats = {}
    for key, info in players.items():
        name = info.get("name") or "?"
        if info.get("ai"):
            name = f"AI · {i18n.level_label(str(record.get('ai_level') or 'normal'))}"
        seats[key] = {"name": name, "ai": bool(info.get("ai"))}
    winner = record.get("winner")
    return {
        "red": seats["1"]["name"],
        "yellow": seats["2"]["name"],
        "ai_level": record.get("ai_level"),
        "moves": int(record.get("move_count") or len(record.get("columns") or []) or 0),
        "winner": winner,
        "draw": bool(record.get("draw")),
        "when": pretty_when(record),
        "started": record.get("started"),
        "ended": record.get("ended"),
    }


def save(record: dict, directory: Optional[Path | str] = None) -> Optional[Path]:
    """Write ``record`` into the replay folder, skipping an identical copy."""
    if not isinstance(record, dict) or not record.get("columns"):
        return None
    folder = Path(directory).expanduser() if directory else records_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    key = _record_key(record)
    existing = _find_by_key(folder, key)
    if existing is not None:
        return existing
    players = _players(record)
    name = "c4_{}_{}_vs_{}.json".format(
        _stamp(record),
        _safe(players["1"].get("name"), "red"),
        _safe(players["2"].get("name"), "yellow"),
    )
    path = folder / name
    suffix = 1
    while path.exists():
        suffix += 1
        path = folder / f"{name[:-5]}_{suffix}.json"
    payload = dict(record)
    payload["saved_by"] = "app"
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return None
    return path


def _find_by_key(folder: Path, key: str) -> Optional[Path]:
    try:
        paths = list(folder.glob("*.json"))
    except OSError:
        return None
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and _record_key(data) == key:
            return path
    return None


def load(path: Path | str) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("game") != "connect4":
        return None
    if not isinstance(data.get("columns"), list) and not isinstance(data.get("moves"), list):
        return None
    return data


def list_records(directory: Optional[Path | str] = None) -> List[dict]:
    """Every readable record in the folder, newest first."""
    folder = Path(directory).expanduser() if directory else records_dir()
    entries: List[dict] = []
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return entries
    for path in paths:
        record = load(path)
        if record is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        entries.append({"path": path, "record": record, "info": summary(record), "mtime": mtime})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def frames_for(record: dict) -> List[dict]:
    columns = record.get("columns")
    if not isinstance(columns, list) or not columns:
        columns = [m.get("col") for m in record.get("moves") or [] if isinstance(m, dict)]
    columns = [int(c) for c in columns if isinstance(c, int) or (isinstance(c, str) and c.isdigit())]
    return engine.replay_frames(columns)


def open_folder(directory: Optional[Path | str] = None) -> bool:
    folder = Path(directory).expanduser() if directory else records_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        elif os.name == "nt":  # pragma: no cover - Windows only
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return True
    except OSError:
        return False
