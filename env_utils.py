# -*- coding: utf-8 -*-
"""Minimal .env loader for local runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def _iter_candidate_paths(start_dir: Path | None = None) -> Iterable[Path]:
    if start_dir is not None:
        yield start_dir / ".env"
    yield Path.cwd() / ".env"
    yield Path(__file__).resolve().parent / ".env"


def load_env_file(start_dir: str | os.PathLike[str] | None = None) -> None:
    """Load key/value pairs from a .env file without overriding existing vars."""
    base_dir = Path(start_dir).resolve() if start_dir is not None else None

    env_path = None
    seen = set()
    for candidate in _iter_candidate_paths(base_dir):
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            env_path = candidate
            break

    if env_path is None:
        return

    with env_path.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue

            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]

            os.environ.setdefault(key, value)
