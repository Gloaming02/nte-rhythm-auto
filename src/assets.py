"""资源路径解析：兼容开发模式与 PyInstaller 打包模式。"""

from __future__ import annotations

import sys
from pathlib import Path


def asset_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def asset_path(relative: str) -> Path:
    return asset_root() / relative


def list_song_templates() -> list[tuple[str, Path]]:
    tpl_dir = asset_path("assets/song_templates")
    if not tpl_dir.is_dir():
        return []
    results: list[tuple[str, Path]] = []
    for p in sorted(tpl_dir.iterdir()):
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"):
            results.append((p.stem, p))
    return results


def list_scene_templates(kind: str) -> list[tuple[str, Path]]:
    tpl_dir = asset_path("assets/scene_templates") / kind
    if not tpl_dir.is_dir():
        return []
    results: list[tuple[str, Path]] = []
    for p in sorted(tpl_dir.iterdir()):
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"):
            results.append((p.stem, p))
    return results