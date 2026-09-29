"""Vérification des nouvelles versions publiées sur GitHub, une fois par heure.

Interroge la dernière « release » du dépôt public version.UPDATE_REPO (API GitHub sans
authentification : 60 requêtes par heure et par adresse IP, largement suffisant).
L'application ne se met pas à jour seule : elle signale la version et le lien de téléchargement.
"""
from __future__ import annotations

import re
import threading
import time

import requests

from version import APP_VERSION, UPDATE_REPO

CHECK_INTERVAL = 3600


def _numbers(version: str) -> tuple:
  return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def check_once() -> dict | None:
  """Retourne la nouvelle version disponible, ou None si l'application est à jour."""
  if not UPDATE_REPO:
    return None
  resp = requests.get(
    f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", timeout=15,
    headers={"Accept": "application/vnd.github+json", "User-Agent": f"WFM-Dashboard/{APP_VERSION}"},
  )
  if resp.status_code == 404:  # aucune version publiée pour l'instant
    return None
  resp.raise_for_status()
  release = resp.json()
  tag = str(release.get("tag_name") or "")
  if not _numbers(tag) or _numbers(tag) <= _numbers(APP_VERSION):
    return None
  exe = next((a for a in release.get("assets", []) if str(a.get("name", "")).lower().endswith(".exe")), None)
  return {
    "version": tag.lstrip("vV"),
    "page": release.get("html_url"),
    "download": exe.get("browser_download_url") if exe else release.get("html_url"),
    "notes": str(release.get("body") or "")[:1000],
  }


def start(on_update) -> None:
  """Lance la vérification en tâche de fond ; on_update(info) est appelé si une version sort."""
  if not UPDATE_REPO:
    return

  def loop() -> None:
    while True:
      try:
        info = check_once()
        if info:
          on_update(info)
      except Exception:
        pass  # pas de réseau, GitHub indisponible… on réessaiera à la prochaine heure
      time.sleep(CHECK_INTERVAL)

  threading.Thread(target=loop, daemon=True, name="update-check").start()
