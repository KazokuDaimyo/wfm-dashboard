from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

try:
  import requests
except ImportError:
  sys.exit("Le module 'requests' est requis.")

import appdata

##########
# CONFIG #
##########

# Pseudo et plateforme : réglages de l'utilisateur (appdata.ingame_name(), appdata.platform()).
LANGUAGE = "en"
DELAY_BETWEEN_REQUESTS = 0.35  # intervalle minimum entre deux requêtes (rate limit API : 3/s)
MAX_WORKERS = 4                # requêtes en parallèle (le débit reste borné par le rate limit)
CACHE_TTL = 24 * 3600          # validité du cache de la liste des objets (secondes)
STATS_TTL = 12 * 3600          # validité du cache des statistiques de vente (secondes)
LOW_VOLUME = 2                 # sous ce volume moyen de ventes/jour, l'objet est signalé peu liquide

BASE_URL = "https://api.warframe.market/v2"
V1_BASE_URL = "https://api.warframe.market/v1"  # les statistiques n'existent qu'en v1
CACHE_FILE = appdata.data_file("items_cache.json")
STATS_CACHE_FILE = appdata.data_file("stats_cache.json")

HEADERS = {  # "Platform" est ajouté à chaque requête : il suit les réglages sans redémarrage
  "Language": LANGUAGE,
  "Accept": "application/json",
  "User-Agent": "wfm-price-checker/2.0",
}

############
# COULEURS #
############

os.system("")  # active les séquences ANSI dans le terminal Windows

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GREY = "\033[90m"

###########
# RÉSEAU  #
###########

class RateLimiter:
  """Espace les départs de requêtes d'au moins min_interval, tous threads confondus."""

  def __init__(self, min_interval: float):
    self.min_interval = min_interval
    self.lock = threading.Lock()
    self.next_time = 0.0

  def wait(self) -> None:
    with self.lock:
      now = time.monotonic()
      delay = self.next_time - now
      self.next_time = max(now, self.next_time) + self.min_interval
    if delay > 0:
      time.sleep(delay)


rate_limiter = RateLimiter(DELAY_BETWEEN_REQUESTS)
_thread_local = threading.local()
print_lock = threading.Lock()


def get_session() -> requests.Session:
  # requests.Session n'est pas garanti thread-safe : une session par thread
  session = getattr(_thread_local, "session", None)
  if session is None:
    session = requests.Session()
    session.headers.update(HEADERS)
    _thread_local.session = session
  return session


def api_get(path: str, params: Optional[dict] = None,
            base: str = BASE_URL, data_key: str = "data") -> Optional[object]:
  url = f"{base}{path}"
  for attempt in range(2):
    rate_limiter.wait()
    try:
      resp = get_session().get(url, params=params, timeout=15, headers={"Platform": appdata.platform()})
    except requests.RequestException as e:
      if attempt == 0:
        continue
      with print_lock:
        print(f"{RED}Erreur réseau sur {url} : {e}{RESET}")
      return None

    if resp.status_code == 404:
      return None
    if resp.status_code == 429 or resp.status_code >= 500:
      if attempt == 0:
        time.sleep(1.5)
        continue
    if not resp.ok:
      with print_lock:
        print(f"{RED}Erreur API ({resp.status_code}) sur {url}{RESET}")
      return None

    try:
      return resp.json().get(data_key)
    except ValueError:
      with print_lock:
        print(f"{RED}Réponse non-JSON reçue depuis {url}{RESET}")
      return None
  return None

##########
# CACHES #
##########

def load_json_cache(path: Path) -> Optional[dict]:
  try:
    return json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None


def save_json_cache(path: Path, data: dict) -> None:
  try:
    path.write_text(json.dumps(data), encoding="utf-8")
  except OSError:
    pass  # le cache est un bonus, on ne bloque pas si l'écriture échoue

#####################
# LISTE DES OBJETS  #
#####################

def slugify(name: str) -> str:
  return name.strip().lower().replace(" ", "-")


def build_item_index() -> dict:
  raw = load_json_cache(CACHE_FILE)
  if raw:
    try:
      if time.time() - raw["timestamp"] < CACHE_TTL and raw.get("language") == LANGUAGE:
        print(f"{GREY}Liste des objets chargée depuis le cache.{RESET}")
        return raw["index"]
    except (KeyError, TypeError):
      pass

  print("Récupération de la liste des objets échangeables...")
  items = api_get("/items")
  if not items:
    sys.exit("Impossible de récupérer la liste des objets depuis Warframe Market.")

  index = {}
  for item in items:
    item_id = item.get("id")
    slug = item.get("slug")
    if not item_id or not slug:
      continue
    i18n = item.get("i18n") or {}
    infos = i18n.get(LANGUAGE) or i18n.get("en") or {}
    name = infos.get("name") or slug
    index[item_id] = {"slug": slug, "name": name}

  save_json_cache(CACHE_FILE, {"timestamp": time.time(), "language": LANGUAGE, "index": index})
  return index

################
# STATISTIQUES #
################

stats_cache_lock = threading.Lock()
stats_cache: dict = {}


def load_stats_cache() -> None:
  global stats_cache
  raw = load_json_cache(STATS_CACHE_FILE)
  if isinstance(raw, dict):
    now = time.time()
    stats_cache = {
      k: v for k, v in raw.items()
      if isinstance(v, dict) and now - v.get("timestamp", 0) < STATS_TTL
    }


def get_item_stats(slug: str, order: dict) -> Optional[dict]:
  """SMA (moving_avg 90 jours des ventes conclues) et volume moyen/jour, avec cache."""
  rank = order.get("rank")
  subtype = order.get("subtype")
  key = f"{appdata.platform()}|{slug}|{rank}|{subtype}"  # les prix diffèrent selon la plateforme

  with stats_cache_lock:
    cached = stats_cache.get(key)
  if cached and time.time() - cached.get("timestamp", 0) < STATS_TTL:
    return cached

  payload = api_get(f"/items/{slug}/statistics", base=V1_BASE_URL, data_key="payload")
  if not payload:
    return None

  days = (payload.get("statistics_closed") or {}).get("90days") or []
  days = [e for e in days if e.get("moving_avg") is not None or e.get("median") is not None]

  # Les objets à variantes (mods rangés, reliques...) ont des entrées séparées : on
  # garde celles qui correspondent à l'ordre, en repartant du tout si rien ne matche.
  if rank is not None:
    matching = [e for e in days if e.get("mod_rank") == rank]
    days = matching or days
  if subtype:
    matching = [e for e in days if e.get("subtype") == subtype]
    days = matching or days

  if not days:
    return None

  last = days[-1]
  sma = last.get("moving_avg")
  if sma is None:
    sma = last.get("median")
  recent = days[-7:]
  vol_day = sum(e.get("volume", 0) for e in recent) / len(recent)

  result = {"timestamp": time.time(), "sma": sma, "vol_day": round(vol_day, 1)}
  with stats_cache_lock:
    stats_cache[key] = result
  return result

##########
# ORDRES #
##########

def get_my_sell_orders(slug: str) -> list:
  orders = api_get(f"/orders/user/{slug}")
  if orders is None:
    sys.exit(f"Impossible de trouver le profil '{slug}'.")
  if not isinstance(orders, list):
    sys.exit("Réponse inattendue de l'API pour tes ordres.")
  return [
    o for o in orders
    if o.get("type") == "sell" and o.get("visible", True) and o.get("platinum") is not None
  ]


def get_lowest_market_price(item_slug: str, my_slug: str, order: dict):
  params = {}
  if order.get("rank") is not None:
    params["rank"] = order["rank"]
  if order.get("subtype"):
    params["subtype"] = order["subtype"]
  if order.get("charges") is not None:
    params["charges"] = order["charges"]

  top = api_get(f"/orders/item/{item_slug}/top", params=params)
  if not top:
    return None, None

  sellers = [
    o for o in top.get("sell", [])
    if o.get("platinum") is not None
    and (o.get("user") or {}).get("slug") != my_slug
    and (o.get("user") or {}).get("status") == "ingame"
  ]

  if not sellers:
    return None, None

  best = min(sellers, key=lambda o: o["platinum"])
  return best["platinum"], (best.get("user") or {}).get("ingameName", "?")

#############
# ANALYSE   #
#############

def variant_label(order: dict) -> str:
  parts = []
  if order.get("rank") is not None:
    parts.append(f"r{order['rank']}")
  if order.get("subtype"):
    parts.append(str(order["subtype"]))
  if order.get("charges") is not None:
    parts.append(f"{order['charges']} charges")
  return f" ({', '.join(parts)})" if parts else ""


def check_order(order: dict, item_index: dict, my_slug: str) -> Optional[dict]:
  item_info = item_index.get(order.get("itemId"))
  if not item_info:
    return None

  lowest, seller = get_lowest_market_price(item_info["slug"], my_slug, order)
  stats = get_item_stats(item_info["slug"], order)

  sma = stats["sma"] if stats else None
  fair = max(1, round(sma)) if sma is not None else None

  # Prix conseillé : s'aligner sur le concurrent en jeu le moins cher (pas d'undercut),
  # sans jamais brader sous la valeur réelle (SMA des ventes conclues sur 90 jours).
  if lowest is not None:
    conseille = max(lowest, fair) if fair is not None else lowest
  else:
    conseille = fair

  return {
    "name": item_info["name"] + variant_label(order),
    "base_name": item_info["name"],
    "order_id": order.get("id"),
    "quantity": order.get("quantity"),
    "rank": order.get("rank"),
    "my_price": order["platinum"],
    "lowest": lowest,
    "seller": seller,
    "sma": fair,
    "vol_day": stats["vol_day"] if stats else None,
    "conseille": conseille,
  }


def main() -> None:
  name = appdata.ingame_name()
  if not name:
    sys.exit("Aucun pseudo configuré : lance d'abord le tableau de bord (server.py) pour le renseigner.")

  my_slug = slugify(name)
  item_index = build_item_index()
  load_stats_cache()

  print(f"\nRécupération de tes ordres de vente ({CYAN}{name}{RESET})...")
  my_orders = get_my_sell_orders(my_slug)
  if not my_orders:
    print("Aucun ordre de vente visible trouvé sur ton profil.")
    return

  total = len(my_orders)
  print(f"{total} ordre(s) de vente trouvé(s).\n")

  results = []
  done = 0
  with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    futures = [pool.submit(check_order, order, item_index, my_slug) for order in my_orders]
    for future in futures:
      result = future.result()
      done += 1
      with print_lock:
        print(f"\r  Vérification des prix... {done}/{total}", end="", flush=True)
      if result:
        results.append(result)
  print("\r" + " " * 40 + "\r", end="")

  save_json_cache(STATS_CACHE_FILE, stats_cache)

  # Les objets trop chers d'abord, puis les trop bas (plus gros écart en tête),
  # puis les prix optimaux, puis ceux sans données
  def sort_key(r: dict):
    if r["conseille"] is None:
      return (3, 0)
    diff = r["my_price"] - r["conseille"]
    if diff > 0:
      return (0, -diff)
    if diff < 0:
      return (1, diff)
    return (2, 0)

  results.sort(key=sort_key)

  header = f"{'Objet':<45} {'Ton prix':>9} {'+ bas':>7} {'SMA':>5} {'Conseil':>8}  Statut"
  print(BOLD + header + RESET)
  print("-" * len(header))

  a_baisser = []
  a_monter = []

  for r in results:
    my_price, conseille = r["my_price"], r["conseille"]
    lowest_str = str(r["lowest"]) if r["lowest"] is not None else "-"
    sma_str = str(r["sma"]) if r["sma"] is not None else "-"
    conseil_str = str(conseille) if conseille is not None else "-"

    if conseille is None:
      statut = f"{GREY}pas de données de vente{RESET}"
    elif my_price > conseille:
      diff = my_price - conseille
      statut = f"{RED}⬆️  trop cher (-{diff}p){RESET}"
      a_baisser.append(r)
    elif my_price < conseille:
      diff = conseille - my_price
      statut = f"{CYAN}💰 trop bas (+{diff}p){RESET}"
      a_monter.append(r)
    else:
      statut = f"{GREEN}✅ prix optimal{RESET}"

    if r["vol_day"] is not None and r["vol_day"] < LOW_VOLUME:
      statut += f" {GREY}(peu de ventes){RESET}"

    name = r["name"]
    display_name = name if len(name) <= 45 else name[:42] + "..."
    print(f"{display_name:<45} {my_price:>9} {lowest_str:>7} {sma_str:>5} {conseil_str:>8}  {statut}")

  def detail(r: dict) -> str:
    parts = []
    if r["lowest"] is not None:
      parts.append(f"+ bas en jeu : {r['lowest']}p")
    if r["sma"] is not None:
      parts.append(f"SMA 90j : {r['sma']}p")
    return f" ({', '.join(parts)})" if parts else ""

  if a_baisser:
    perte = sum(r["my_price"] - r["conseille"] for r in a_baisser)
    print(f"\n{BOLD}{len(a_baisser)} objet(s) à baisser{RESET} (écart cumulé : {RED}{perte}p{RESET}) :")
    for r in a_baisser:
      print(f"  - {r['name']} : {r['my_price']}p -> {YELLOW}{r['conseille']}p{RESET}{detail(r)}")

  if a_monter:
    gain = sum(r["conseille"] - r["my_price"] for r in a_monter)
    print(f"\n{BOLD}{len(a_monter)} objet(s) sous-évalué(s){RESET} (manque à gagner : {CYAN}{gain}p{RESET}) :")
    for r in a_monter:
      print(f"  - {r['name']} : {r['my_price']}p -> {CYAN}{r['conseille']}p{RESET}{detail(r)}")

  if not a_baisser and not a_monter:
    print(f"\n{GREEN}Tous tes prix sont bien positionnés{RESET}")


if __name__ == "__main__":
  main()
