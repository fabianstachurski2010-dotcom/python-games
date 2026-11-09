#!/usr/bin/env python3
"""Tiny JSON settings store (language, last player name, records folder)."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

PATH = os.path.join(os.path.expanduser("~"), ".connect_four_settings.json")

DEFAULTS: Dict[str, Any] = {
    "lang": "zh",
    "name": "",
    "ai_level": "normal",
    "records_dir": "",
    "show_replay": True,
}


def load() -> Dict[str, Any]:
    data = dict(DEFAULTS)
    try:
        with open(PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in DEFAULTS and isinstance(value, type(DEFAULTS[key])):
                    data[key] = value
    except (OSError, ValueError):
        pass
    return data


def save(**patch: Any) -> Dict[str, Any]:
    data = load()
    data.update({k: v for k, v in patch.items() if k in DEFAULTS})
    try:
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, PATH)
    except OSError:
        pass
    return data
