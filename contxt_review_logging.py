#!/usr/bin/env python3
"""
Shared logging helpers for the review loop.
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ReviewLogger:
    def __init__(self, path: str | Path, component: str):
        self.path = Path(path)
        self.component = component
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _format_fields(self, fields: dict[str, Any]) -> str:
        if not fields:
            return ""
        parts = []
        for key, value in fields.items():
            if value is None:
                continue
            parts.append(f"{key}={json.dumps(value, sort_keys=True)}")
        return " | " + " ".join(parts) if parts else ""

    def log(self, level: str, message: str, **fields: Any) -> None:
        line = f"{_utc_now()} {level.upper():5} [{self.component}] {message}{self._format_fields(fields)}\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def info(self, message: str, **fields: Any) -> None:
        self.log("info", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.log("error", message, **fields)

    def tail_text(self, lines: int = 200) -> str:
        if not self.path.exists():
            return "No review-loop logs yet."
        last_lines: deque[str] = deque(maxlen=lines)
        with self.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                last_lines.append(line.rstrip("\n"))
        return "\n".join(last_lines) if last_lines else "No review-loop logs yet."
