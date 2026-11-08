"""Settings, token and OAuth client file locations."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, Optional

APP_NAME = "9J Helper"
CLIENT_SECRET_NAME = "client_secret.json"


def app_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def token_path() -> str:
    return os.path.join(app_dir(), "token.json")


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def client_secret_path() -> Optional[str]:
    """Look for the Google OAuth client file: bundled in the exe, next to the exe/project,
    or in the settings folder."""
    candidates = []
    if os.environ.get("NINEJ_CLIENT_SECRET"):
        candidates.append(os.environ["NINEJ_CLIENT_SECRET"])
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(getattr(sys, "_MEIPASS", ""), CLIENT_SECRET_NAME))
        candidates.append(os.path.join(os.path.dirname(sys.executable), CLIENT_SECRET_NAME))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), CLIENT_SECRET_NAME))
    candidates.append(os.path.join(app_dir(), CLIENT_SECRET_NAME))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def spreadsheet_id_from(text: str) -> Optional[str]:
    text = (text or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]{25,}", text):
        return text
    return None


class Config:
    def __init__(self, data: Optional[Dict] = None):
        data = data or {}
        self.spreadsheet_id: str = data.get("spreadsheet_id", "")
        self.spreadsheet_title: str = data.get("spreadsheet_title", "")
        self.part_tabs: Dict[str, str] = dict(data.get("part_tabs", {}))
        self.last_folder: str = data.get("last_folder", "")
        self.sort_order: str = data.get("sort_order", "date")  # a key of planner.SORTS

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                return cls(json.load(f))
        except (OSError, ValueError):
            return cls()

    def save(self) -> None:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump({
                "spreadsheet_id": self.spreadsheet_id,
                "spreadsheet_title": self.spreadsheet_title,
                "part_tabs": self.part_tabs,
                "last_folder": self.last_folder,
                "sort_order": self.sort_order,
            }, f, indent=2)
