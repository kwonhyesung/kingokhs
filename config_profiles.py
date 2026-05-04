import json
import os
from typing import Any, Dict, Optional


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_FILE = os.path.join(BASE_DIR, "config.default.json")
LOCAL_CONFIG_FILE = os.path.join(BASE_DIR, "config.local.json")
LEGACY_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

ROLE_CONFIG_FILES = {
    "도사": os.path.join(BASE_DIR, "config.dosa.json"),
    "격수": os.path.join(BASE_DIR, "config.warrior.json"),
    "술사": os.path.join(BASE_DIR, "config.shaman.json"),
    "dosa": os.path.join(BASE_DIR, "config.dosa.json"),
    "warrior": os.path.join(BASE_DIR, "config.warrior.json"),
    "shaman": os.path.join(BASE_DIR, "config.shaman.json"),
}

ROLE_ALIASES = {
    "도사": "도사",
    "dosa": "도사",
    "priest": "도사",
    "격수": "격수",
    "warrior": "격수",
    "술사": "술사",
    "shaman": "술사",
}


def normalize_role_name(role: Optional[str]) -> str:
    text = str(role or "").strip()
    return ROLE_ALIASES.get(text, text)


def _read_json(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_profiled_config(role: Optional[str] = None, include_local: bool = True) -> Dict[str, Any]:
    """Load config.default + role-specific + local/legacy config in order."""
    config: Dict[str, Any] = {}
    config = _merge_dicts(config, _read_json(DEFAULT_CONFIG_FILE))

    normalized_role = normalize_role_name(role)
    role_path = ROLE_CONFIG_FILES.get(normalized_role)
    if role_path:
        config = _merge_dicts(config, _read_json(role_path))

    if include_local and os.path.exists(LOCAL_CONFIG_FILE):
        config = _merge_dicts(config, _read_json(LOCAL_CONFIG_FILE))
    elif include_local and os.path.exists(LEGACY_CONFIG_FILE):
        config = _merge_dicts(config, _read_json(LEGACY_CONFIG_FILE))

    return config


def load_local_config() -> Dict[str, Any]:
    """Load config.local.json if present, otherwise fall back to legacy config.json."""
    if os.path.exists(LOCAL_CONFIG_FILE):
        return _read_json(LOCAL_CONFIG_FILE)
    return _read_json(LEGACY_CONFIG_FILE)


def save_local_config(config: Dict[str, Any]) -> None:
    """Persist the per-PC config into config.local.json."""
    current = load_local_config()
    merged = _merge_dicts(current, config)
    with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=4, ensure_ascii=False)
