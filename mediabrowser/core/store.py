# SPDX-FileCopyrightText: 2026 Digital Archon
# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import json
from pathlib import Path

from . import config

DEFAULT_APP_SETTINGS = {
    "musicbrainz_format": "xml",
    # The Nano-GPT model and endpoint: see core.ai. The key is in the keyring.
    "ai_model": "anthropic/claude-sonnet-5",
    "ai_base_url": "https://nano-gpt.com/api/v1",
    "ai_frames_per_chapter": 2,
    "ai_search": "kagi",
}


def load_app_settings() -> dict:
    config.ensure_data_dir()
    if not config.APP_SETTINGS_FILE.exists():
        return dict(DEFAULT_APP_SETTINGS)
    try:
        with open(config.APP_SETTINGS_FILE, encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_APP_SETTINGS)
    merged = dict(DEFAULT_APP_SETTINGS)
    merged.update(settings)
    return merged


def save_app_settings(settings: dict) -> None:
    config.ensure_data_dir()
    tmp_path = config.APP_SETTINGS_FILE.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
    tmp_path.replace(config.APP_SETTINGS_FILE)


def make_video_id(kind: str, path, title_idx=None) -> str:
    h = hashlib.sha1()
    h.update(kind.encode("utf-8"))
    h.update(b"|")
    h.update(str(Path(path).resolve()).encode("utf-8"))
    if title_idx is not None:
        h.update(b"|")
        h.update(str(title_idx).encode("utf-8"))
    return h.hexdigest()[:16]


def default_library(root=None) -> dict:
    return {
        "settings": {
            "library_root": str(Path(root).resolve()) if root else None,
            "min_bluray_title_seconds": config.DEFAULT_MIN_BLURAY_TITLE_SECONDS,
        },
        "videos": {},
    }


def _normalize(data: dict) -> dict:
    data.setdefault("settings", {})
    data["settings"].setdefault("library_root", None)
    data["settings"].setdefault(
        "min_bluray_title_seconds", config.DEFAULT_MIN_BLURAY_TITLE_SECONDS
    )
    if data["settings"]["min_bluray_title_seconds"] == config.LEGACY_MIN_BLURAY_TITLE_SECONDS:
        data["settings"]["min_bluray_title_seconds"] = config.DEFAULT_MIN_BLURAY_TITLE_SECONDS
    data.setdefault("videos", {})
    return data


def _file_for_root(root) -> Path:
    digest = hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:16]
    return config.LIBRARIES_DIR / f"{digest}.json"


def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def migrate_legacy_library() -> None:
    """One-time move of the old single-file layout into the new per-folder
    layout, so upgrading the app doesn't lose anyone's existing data.
    """
    if not config.LEGACY_LIBRARY_FILE.exists():
        return
    data = _read_json(config.LEGACY_LIBRARY_FILE)
    root = (data or {}).get("settings", {}).get("library_root")
    if not root:
        config.LEGACY_LIBRARY_FILE.unlink(missing_ok=True)
        return

    config.ensure_data_dir()
    target = _file_for_root(root)
    if not target.exists():
        config.LEGACY_LIBRARY_FILE.replace(target)
    else:
        config.LEGACY_LIBRARY_FILE.unlink(missing_ok=True)


def load_library_for_root(root) -> dict:
    """Loads the stored library for this folder, or a fresh empty one if
    it's never been scanned before.
    """
    config.ensure_data_dir()
    path = _file_for_root(root)
    data = _read_json(path) if path.exists() else None
    if data is None:
        return default_library(root)
    return _normalize(data)


def has_library(root) -> bool:
    """Whether this folder has a stored library of its own."""
    return _file_for_root(root).exists()


def save_library(data: dict) -> None:
    root = data["settings"]["library_root"]
    config.ensure_data_dir()
    path = _file_for_root(root)
    tmp_path = path.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp_path.replace(path)


def list_libraries():
    """Returns [{"root": str, "video_count": int, "network_uri": str | None,
    "locked": bool, "private": bool}, ...] for every folder
    that has stored data, newest-scanned first.
    """
    config.ensure_data_dir()
    libraries = []
    for path in config.LIBRARIES_DIR.glob("*.json"):
        data = _read_json(path)
        if not data:
            continue
        root = data.get("settings", {}).get("library_root")
        if not root:
            continue
        libraries.append({
            "root": root,
            "video_count": len(data.get("videos", {})),
            "network_uri": data.get("settings", {}).get("network_uri"),
            "locked": bool(data.get("settings", {}).get("locked")),
            "private": bool(data.get("settings", {}).get("private")),
            "mtime": path.stat().st_mtime,
        })
    libraries.sort(key=lambda lib: lib["mtime"], reverse=True)
    return libraries


def delete_library(root) -> None:
    """Deletes only this folder's stored JSON data - never touches media
    files, which live entirely outside config.LIBRARIES_DIR.
    """
    _file_for_root(root).unlink(missing_ok=True)
