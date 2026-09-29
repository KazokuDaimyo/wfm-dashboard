"""Emplacement des fichiers et réglages de l'utilisateur.

Les ressources (page web, code de lecture Overframe) sont à côté du code, ou à l'intérieur
de l'exécutable PyInstaller. Les données personnelles et les caches vivent dans
%APPDATA%\\WFM-Dashboard : chacun a les siennes, et remplacer l'exécutable par une nouvelle
version ne les efface pas.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path

APP_NAME = "WFM-Dashboard"

# sys._MEIPASS : dossier temporaire où PyInstaller décompresse les ressources du .exe
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Valeurs acceptées par l'en-tête "Platform" de l'API warframe.market
PLATFORMS = {"pc": "PC", "ps4": "PlayStation", "xbox": "Xbox", "switch": "Nintendo Switch", "mobile": "Mobile"}


def data_file(name: str) -> Path:
  return DATA_DIR / name


def resource(name: str) -> Path:
  return RESOURCE_DIR / name


# ---------- Déménagement des anciennes données ----------
# Avant la version autonome, tout était écrit à côté du code. Au premier lancement depuis
# les sources, ces fichiers sont déplacés (jamais copiés : le jeton ne doit pas traîner
# dans le dossier du dépôt Git).

_LEGACY_FILES = (
  "wfm_token.txt", "builds.json", "builds.backup-avant-dossiers.json",
  "items_cache.json", "stats_cache.json", "drops_cache.json", "prime_scan_cache.json",
  "wiki_cache.json", "wiki_images.json",
)


def _migrate_legacy_files() -> None:
  if getattr(sys, "frozen", False):
    return
  code_dir = Path(__file__).resolve().parent
  for name in _LEGACY_FILES:
    old, new = code_dir / name, DATA_DIR / name
    if old.exists() and not new.exists():
      try:
        shutil.move(str(old), str(new))
      except OSError:
        pass


_migrate_legacy_files()


# ---------- Réglages ----------

_SETTINGS_FILE = data_file("settings.json")
_lock = threading.Lock()
_settings: dict | None = None


def settings() -> dict:
  global _settings
  with _lock:
    if _settings is None:
      try:
        loaded = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
      except (OSError, ValueError):
        loaded = {}
      _settings = {
        "ingame_name": str(loaded.get("ingame_name") or ""),
        "platform": loaded.get("platform") if loaded.get("platform") in PLATFORMS else "pc",
      }
    return dict(_settings)


def save_settings(ingame_name: str, platform: str) -> dict:
  global _settings
  if platform not in PLATFORMS:
    raise ValueError("Plateforme inconnue.")
  with _lock:
    _settings = {"ingame_name": ingame_name, "platform": platform}
    _SETTINGS_FILE.write_text(json.dumps(_settings, ensure_ascii=False), encoding="utf-8")
    return dict(_settings)


def ingame_name() -> str:
  return settings()["ingame_name"]


def platform() -> str:
  return settings()["platform"]
