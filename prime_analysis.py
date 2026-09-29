"""Analyse Prime : flips sur les sets vaultés et score de farm des pièces disponibles.

Sources : tables de drop communautaires (drops.warframestat.us) pour le contenu des
reliques et les taux de drop, worldstate (api.warframestat.us) pour exclure la
Resurgence, et warframe.market pour les prix. Les résultats sont poussés dans
l'état du serveur au fil de l'eau pour l'affichage progressif.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

import appdata
import main as core

DROPS_BASE = "https://drops.warframestat.us/data"
VAULT_TRADER_URL = "https://api.warframestat.us/pc/vaultTrader"
DROPS_CACHE = appdata.data_file("drops_cache.json")
RESULTS_CACHE = appdata.data_file("prime_scan_cache.json")
DROPS_TTL = 24 * 3600

MIN_FLIP_PROFIT = 10  # delta minimum (en platinum) pour lister un flip

# Nodes de farm standard par tier de relique (choix utilisateur)
FARM_NODES = {
  "Lith": ("Void", "Hepit"),
  "Meso": ("Void", "Ukko"),
  "Neo": ("Uranus", "Ur"),
  "Axi": ("Lua", "Apollo"),
}


def default_state() -> dict:
  return {
    "status": "idle", "phase": "", "progress": [0, 0],
    "flips": [], "farms": [], "notes": [],
    "finished_at": None, "error": None,
  }


def load_saved() -> dict:
  try:
    saved = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    if saved.get("status") == "done":
      return saved
  except (OSError, ValueError):
    pass
  return default_state()


def fetch_drops_data() -> dict:
  """missionRewards + relics, avec cache disque de 24 h (fichiers volumineux)."""
  try:
    cached = json.loads(DROPS_CACHE.read_text(encoding="utf-8"))
    if time.time() - cached["timestamp"] < DROPS_TTL:
      return cached
  except (OSError, ValueError, KeyError):
    pass

  data = {"timestamp": time.time()}
  for key, path in [("missionRewards", "missionRewards.json"), ("relics", "relics.json")]:
    resp = requests.get(f"{DROPS_BASE}/{path}", timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    data[key] = payload.get(key, payload)

  try:
    DROPS_CACHE.write_text(json.dumps(data), encoding="utf-8")
  except OSError:
    pass
  return data


def iter_reward_entries(rewards) -> list:
  """Les tables de mission sont soit une liste, soit un dict de rotations A/B/C."""
  if isinstance(rewards, list):
    return rewards
  if isinstance(rewards, dict):
    out = []
    for entries in rewards.values():
      if isinstance(entries, list):
        out.extend(entries)
    return out
  return []


def parse_relic_name(item_name: str):
  """'Lith A1 Relic' -> ('Lith', 'A1'), sinon None."""
  parts = item_name.split()
  if len(parts) == 3 and parts[2] == "Relic" and parts[0] in ("Lith", "Meso", "Neo", "Axi"):
    return parts[0], parts[1]
  return None


def radiant_rewards(relics_data: list) -> dict:
  """(tier, code) -> [{itemName, chance}] à l'état Radiant."""
  out = {}
  for relic in relics_data:
    if relic.get("state") != "Radiant":
      continue
    key = (relic.get("tier"), relic.get("relicName"))
    out[key] = [
      {"itemName": r.get("itemName", ""), "chance": r.get("chance", 0)}
      for r in relic.get("rewards", [])
    ]
  return out


def currently_dropping_relics(mission_rewards: dict) -> set:
  """Toutes les reliques présentes dans les tables de drop actuelles."""
  relics = set()
  for planet in mission_rewards.values():
    if not isinstance(planet, dict):
      continue
    for node in planet.values():
      if not isinstance(node, dict):
        continue
      for entry in iter_reward_entries(node.get("rewards")):
        parsed = parse_relic_name(entry.get("itemName", ""))
        if parsed:
          relics.add(parsed)
  return relics


def farm_node_relics(mission_rewards: dict) -> dict:
  """(tier, code) -> {node, chance} pour les reliques droppant sur les 4 nodes de farm."""
  out = {}
  for tier, (planet_name, node_name) in FARM_NODES.items():
    planet = mission_rewards.get(planet_name) or {}
    for key, node in planet.items():
      if not key.startswith(node_name):
        continue
      for entry in iter_reward_entries(node.get("rewards")):
        parsed = parse_relic_name(entry.get("itemName", ""))
        if not parsed or parsed[0] != tier:
          continue
        chance = entry.get("chance", 0) or 0
        prev = out.get(parsed)
        if prev is None or chance > prev["chance"]:
          out[parsed] = {"node": f"{node_name} ({planet_name})", "chance": chance}
  return out


def resurgence_base_names(radiant: dict) -> set:
  """Noms de base ('Frost Prime') actuellement chez Varzia, à exclure des flips."""
  bases = set()
  resp = requests.get(VAULT_TRADER_URL, timeout=30)
  resp.raise_for_status()
  data = resp.json()
  for entry in data.get("inventory", []):
    name = str(entry.get("item", ""))
    parsed = parse_relic_name(name)
    if parsed:
      for reward in radiant.get(parsed, []):
        bases.add(prime_base_name(reward["itemName"]))
    elif " Prime" in name:
      bases.add(name.split(" Prime")[0] + " Prime")
  bases.discard("")
  return bases


def prime_base_name(item_name: str) -> str:
  """'Ash Prime Systems Blueprint' -> 'Ash Prime'."""
  if " Prime" not in item_name:
    return ""
  return item_name.split(" Prime")[0] + " Prime"


def run(state: dict, state_lock) -> None:
  """Analyse complète ; mutations de state['prime'] sous verrou, au fil de l'eau."""

  def update(**kwargs):
    with state_lock:
      state["prime"].update(kwargs)

  def add_result(kind: str, row: dict):
    with state_lock:
      state["prime"][kind].append(row)

  with state_lock:
    state["prime"] = {**default_state(), "status": "running", "phase": "Téléchargement des tables de drop..."}

  try:
    item_index = core.build_item_index()
    core.load_stats_cache()
    name_to_item = {v["name"].lower(): v for v in item_index.values()}

    drops = fetch_drops_data()
    radiant = radiant_rewards(drops["relics"])
    dropping = currently_dropping_relics(drops["missionRewards"])
    farm_relics = farm_node_relics(drops["missionRewards"])

    farmable_bases = set()
    for relic_key in dropping:
      for reward in radiant.get(relic_key, []):
        farmable_bases.add(prime_base_name(reward["itemName"]))
    farmable_bases.discard("")

    notes = []
    try:
      resurgence = resurgence_base_names(radiant)
      if resurgence:
        notes.append(f"{len(resurgence)} primes en Resurgence chez Varzia exclus des flips.")
    except Exception:
      resurgence = set()
      notes.append("Varzia injoignable : la Resurgence n'a pas pu être exclue des flips.")
    update(notes=notes)

    # ---------- Phase 1 : flips sur les sets vaultés ----------
    my_slug = core.slugify(appdata.ingame_name())
    sets = [v for v in item_index.values() if v["slug"].endswith("_prime_set")]
    vaulted_sets = []
    for s in sets:
      base = s["name"].replace(" Set", "").strip()
      if base in resurgence:
        continue
      if not any(fb == base for fb in farmable_bases):
        vaulted_sets.append((s, base))

    update(phase="Flips des sets vaultés", progress=[0, len(vaulted_sets)])

    for i, (s, base) in enumerate(vaulted_sets):
      update(progress=[i + 1, len(vaulted_sets)])
      top = core.api_get(f"/orders/item/{s['slug']}/top")
      if not top:
        continue
      sellers = sorted(
        (o for o in top.get("sell", [])
         if o.get("platinum") is not None
         and (o.get("user") or {}).get("status") == "ingame"
         and (o.get("user") or {}).get("slug") != my_slug),
        key=lambda o: o["platinum"],
      )
      if len(sellers) < 2:
        continue
      p1, p2 = sellers[0]["platinum"], sellers[1]["platinum"]
      p3 = sellers[2]["platinum"] if len(sellers) > 2 else None
      stats = core.get_item_stats(s["slug"], {})
      sma = round(stats["sma"]) if stats and stats.get("sma") is not None else None
      resale = min(p2, sma) if sma is not None else p2
      profit = resale - p1
      if profit < MIN_FLIP_PROFIT:
        continue
      add_result("flips", {
        "name": s["name"], "slug": s["slug"],
        "p1": p1, "seller": (sellers[0].get("user") or {}).get("ingameName", "?"),
        "p2": p2, "p3": p3, "sma": sma,
        "vol_day": stats["vol_day"] if stats else None,
        "resale": resale, "profit": profit,
      })

    # ---------- Phase 2 : score de farm des pièces ----------
    pieces = {}  # slug -> meilleure combinaison relique/node
    for relic_key, node_info in farm_relics.items():
      for reward in radiant.get(relic_key, []):
        name = reward["itemName"]
        if " Prime" not in name or name.startswith("Forma"):
          continue
        item = name_to_item.get(name.lower())
        if item is None:
          continue
        candidate = {
          "name": item["name"], "slug": item["slug"],
          "relic": f"{relic_key[0]} {relic_key[1]}",
          "node": node_info["node"],
          "piece_chance": reward["chance"],
          "relic_chance": node_info["chance"],
          "weight": (reward["chance"] or 0) * (node_info["chance"] or 0),
        }
        prev = pieces.get(item["slug"])
        if prev is None or candidate["weight"] > prev["weight"]:
          pieces[item["slug"]] = candidate

    piece_list = list(pieces.values())
    update(phase="Score de farm des pièces", progress=[0, len(piece_list)])

    for i, piece in enumerate(piece_list):
      update(progress=[i + 1, len(piece_list)])
      stats = core.get_item_stats(piece["slug"], {})
      if not stats or stats.get("sma") is None:
        continue
      sma = round(stats["sma"], 1)
      score = sma * (piece["piece_chance"] / 100) * (piece["relic_chance"] / 100)
      add_result("farms", {
        "name": piece["name"], "slug": piece["slug"],
        "relic": piece["relic"], "node": piece["node"],
        "piece_chance": piece["piece_chance"], "relic_chance": piece["relic_chance"],
        "sma": sma, "vol_day": stats.get("vol_day"),
        "score": round(score, 2),
      })

    core.save_json_cache(core.STATS_CACHE_FILE, core.stats_cache)

    with state_lock:
      state["prime"]["flips"].sort(key=lambda r: -r["profit"])
      state["prime"]["farms"].sort(key=lambda r: -r["score"])
      state["prime"].update(status="done", phase="", finished_at=time.time())
      snapshot = dict(state["prime"])
    try:
      RESULTS_CACHE.write_text(json.dumps(snapshot), encoding="utf-8")
    except OSError:
      pass

  except Exception as e:  # l'analyse ne doit jamais tuer le serveur
    import traceback
    traceback.print_exc()
    update(status="error", error=str(e), phase="")
