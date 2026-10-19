"""Offline category rules; historical task configurations are not shipped as presets."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def category_path(filename):
    if filename == "shower_curtain_local_union_20260922.json":
        return ROOT / "tests" / "fixtures" / "categories" / filename
    return ROOT / "config" / "categories" / filename
