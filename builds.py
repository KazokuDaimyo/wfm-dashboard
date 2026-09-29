"""Builds Overframe importés via le bookmarklet, et fiches des mods/arcanes.

Overframe bloque les requêtes automatisées : ce module ne le contacte jamais.
Les builds arrivent depuis le navigateur de l'utilisateur (bookmarklet), et les
fiches combinent le wiki officiel (section d'acquisition) et warframe.market.
Aucune requête n'est faite sans clic de l'utilisateur.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

import appdata
import main as core

BUILDS_FILE = appdata.data_file("builds.json")
WIKI_CACHE_FILE = appdata.data_file("wiki_cache.json")
IMAGES_FILE = appdata.data_file("wiki_images.json")
WIKI_API = "https://wiki.warframe.com/api.php"
WIKI_BASE = "https://wiki.warframe.com"
WIKI_TTL = 7 * 24 * 3600
WIKI_CACHE_VERSION = 2  # à incrémenter quand le nettoyage du HTML change
IMAGES_TTL = 30 * 24 * 3600
KINDS = ("warframe", "melee", "primary", "secondary", "companion")
SLOT_TYPES = (0, 1, 2, 3)  # rôle d'un emplacement (codes d'Overframe) : ordinaire, aura, posture, exilus
# Catégories du wiki -> type d'objet, pour les builds importés avant le relevé du type
WIKI_KIND_CATEGORIES = (
  ("Category:Warframes", "warframe"), ("Category:Companion", "companion"),
  ("Category:Melee Weapons", "melee"), ("Category:Primary Weapons", "primary"),
  ("Category:Secondary Weapons", "secondary"),
)
MAX_WIKI_ICON = 64  # px : on garde les icônes des tableaux, pas les grandes illustrations
ACQUISITION_IDS = ("acquisition", "drop_locations", "drop_location", "locations")

_lock = threading.Lock()
_wiki_cache: dict | None = None
_item_by_name: dict | None = None


# ---------- Images des mods / arcanes : carte complète du wiki ----------

def _fetch_wiki_images(names: list) -> dict:
  """Image principale de chaque page (la carte complète), 50 titres par requête.
  Retourne nom en minuscules -> URL, ou None si la page n'a pas d'image."""
  found = {}
  for i in range(0, len(names), 50):
    chunk = names[i:i + 50]
    resp = requests.get(WIKI_API, timeout=30, headers={"User-Agent": "wfm-price-checker/2.0 (outil perso)"}, params={
      "action": "query", "titles": "|".join(chunk), "prop": "pageimages",
      "piprop": "original", "redirects": 1, "format": "json",
    })
    resp.raise_for_status()
    query = resp.json().get("query", {})
    # le wiki peut renommer un titre (casse, redirection) : on remonte au nom demandé
    renamed = {r["from"]: r["to"] for r in query.get("normalized", []) + query.get("redirects", [])}
    by_title = {p.get("title"): (p.get("original") or {}).get("source") for p in query.get("pages", {}).values()}
    for name in chunk:
      title = renamed.get(name, name)
      title = renamed.get(title, title)
      found[name.lower()] = by_title.get(title)
  return found


def _attach_images(builds: list, allow_download: bool) -> None:
  """Renseigne entry["image"] (mods, arcanes) et build["item_image"] (Warframe ou arme)
  depuis le cache ; télécharge les noms manquants si autorisé."""
  try:
    cache = json.loads(IMAGES_FILE.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    cache = {}
  entries = [e for b in builds for e in b.get("mods", []) + b.get("arcanes", [])]
  items = [b["item"] for b in builds if b.get("item")]
  now = time.time()
  missing = sorted({
    name for name in [e["name"] for e in entries] + items
    if now - cache.get(name.lower(), {}).get("timestamp", 0) >= IMAGES_TTL
  })
  if missing and allow_download:
    for key, url in _fetch_wiki_images(missing).items():
      cache[key] = {"url": url, "timestamp": now}
    try:
      IMAGES_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
      pass
  for e in entries:
    url = (cache.get(e["name"].lower()) or {}).get("url")
    if url:
      e["image"] = url
    elif not str(e.get("image", "")).startswith(WIKI_BASE):
      e.pop("image", None)  # anciennes vignettes WFCD : remplacées par la carte du wiki
  for b in builds:
    url = (cache.get(b.get("item", "").lower()) or {}).get("url")
    if url:
      b["item_image"] = url


# ---------- Type d'objet et rôle des emplacements ----------

def _derive_slot_types(build: dict) -> None:
  """Complète slot_type pour les mods qui ne l'ont pas (builds importés avant son relevé),
  d'après le type d'objet. Les slots 9 et 10 n'ont pas le même rôle selon l'objet."""
  kind = build.get("kind")
  if kind == "companion":
    build["ten_slots"] = True  # 10 emplacements ordinaires, sans aura ni exilus
  if not kind:
    return
  for i, mod in enumerate(build.get("mods", [])):
    if mod.get("slot_type") in SLOT_TYPES:
      continue
    slot = mod.get("slot") or i + 1
    if slot <= 8 or build.get("ten_slots"):
      mod["slot_type"] = 0
    elif kind == "warframe":
      mod["slot_type"] = 1 if slot == 9 else 3
    elif kind == "melee":
      mod["slot_type"] = 2 if slot == 9 else 3
    else:  # armes principales et secondaires : un seul emplacement spécial, l'exilus
      mod["slot_type"] = 3


def _fetch_wiki_kinds(names: list) -> dict:
  """Type d'objet d'après les catégories de sa page wiki. Nom en minuscules -> type ou None."""
  found = {}
  for i in range(0, len(names), 50):
    chunk = names[i:i + 50]
    resp = requests.get(WIKI_API, timeout=30, headers={"User-Agent": "wfm-price-checker/2.0 (outil perso)"}, params={
      "action": "query", "titles": "|".join(chunk), "prop": "categories", "redirects": 1, "format": "json",
      "clcategories": "|".join(c for c, _ in WIKI_KIND_CATEGORIES), "cllimit": "max",
    })
    resp.raise_for_status()
    query = resp.json().get("query", {})
    renamed = {r["from"]: r["to"] for r in query.get("normalized", []) + query.get("redirects", [])}
    cats_by_title = {p.get("title"): {c["title"] for c in p.get("categories", [])} for p in query.get("pages", {}).values()}
    for name in chunk:
      title = renamed.get(name, name)
      cats = cats_by_title.get(renamed.get(title, title), set())
      found[name.lower()] = next((kind for cat, kind in WIKI_KIND_CATEGORIES if cat in cats), None)
  return found


def _attach_kinds(builds: list) -> None:
  """Builds sans type d'objet : le demande au wiki (une fois par build), puis déduit les emplacements."""
  pending = [b for b in builds if b.get("item") and "kind" not in b and not b.get("kind_checked")]
  if not pending:
    return
  kinds = _fetch_wiki_kinds(sorted({b["item"] for b in pending}))
  for b in pending:
    kind = kinds.get(b["item"].lower())
    if kind:
      b["kind"] = kind
      _derive_slot_types(b)
    else:
      b["kind_checked"] = True  # objet non classé (archwing…) : on ne redemande pas à chaque fois


# ---------- Bibliothèque : builds et dossiers ----------
# builds.json = {"folders": [{"id", "name"}], "builds": [...]} ; un build a un
# "folder_id" (None = non classé) et un "custom_name" (None = titre d'Overframe).

def load_library() -> dict:
  try:
    data = json.loads(BUILDS_FILE.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    data = None
  if isinstance(data, list):  # ancien format : simple liste de builds
    data = {"folders": [], "builds": data}
  if not isinstance(data, dict):
    data = {}
  lib = {
    "folders": [f for f in data.get("folders", []) if isinstance(f, dict) and f.get("id")],
    "builds": [b for b in data.get("builds", []) if isinstance(b, dict) and b.get("id")],
  }
  _attach_images(lib["builds"], allow_download=False)
  return lib


def save_library(lib: dict) -> None:
  try:
    BUILDS_FILE.write_text(json.dumps(lib, ensure_ascii=False), encoding="utf-8")
  except OSError:
    pass


def _copy(lib: dict) -> dict:
  return json.loads(json.dumps(lib))


def _clean_name(value, max_len: int) -> str:
  return " ".join(str(value or "").split())[:max_len]


def _clean_entries(raw) -> list:
  out = []
  for e in (raw or [])[:30]:
    if not isinstance(e, dict):
      continue
    name = str(e.get("name", "")).strip()[:80]
    if name:
      rarity = str(e.get("rarity") or "")[:20].lower() or None
      entry = {"name": name, "rarity": rarity}
      if isinstance(e.get("slot"), int) and 1 <= e["slot"] <= 20:
        entry["slot"] = e["slot"]
      if isinstance(e.get("drain"), int) and -100 <= e["drain"] <= 100:
        entry["drain"] = e["drain"]
      if e.get("slot_type") in SLOT_TYPES:
        entry["slot_type"] = e["slot_type"]
      for key in ("slot_polarity", "mod_polarity"):
        if isinstance(e.get(key), str) and re.fullmatch(r"AP_[A-Z_]{1,20}", e[key]):
          entry[key] = e[key]
      if e.get("match") in ("match", "mismatch", "neutral"):
        entry["match"] = e["match"]
      if isinstance(e.get("drain_text"), str) and 0 < len(e["drain_text"].strip()) <= 8:
        entry["drain_text"] = e["drain_text"].strip()
      if e.get("forma") is True:
        entry["forma"] = True
      out.append(entry)
  return out


def import_build(lib: dict, payload: dict) -> tuple[dict, str]:
  """Ajoute un build, ou met à jour celui de même URL en gardant son nom et son dossier.
  Retourne la nouvelle bibliothèque et l'id du build. Lève ValueError si invalide."""
  url = str(payload.get("url", "")).split("#")[0]
  if not url.startswith("https://overframe.gg/build/"):
    raise ValueError("Ce n'est pas une page de build Overframe.")
  mods = _clean_entries(payload.get("mods"))
  arcanes = _clean_entries(payload.get("arcanes"))
  if not mods and not arcanes:
    raise ValueError("Aucun mod trouvé sur la page.")

  lib = _copy(lib)
  previous = next((b for b in lib["builds"] if b.get("url") == url), None)
  build = {
    "id": previous["id"] if previous else str(int(time.time() * 1000)),
    "item": str(payload.get("item", "")).strip()[:80],
    "title": str(payload.get("title", "")).strip()[:200] or "Build sans titre",
    "url": url,
    "mods": mods,
    "arcanes": arcanes,
    "imported_at": time.time(),
    "custom_name": previous.get("custom_name") if previous else None,
    "folder_id": previous.get("folder_id") if previous else None,
  }
  formas = payload.get("formas")
  if isinstance(formas, int) and not isinstance(formas, bool) and 0 <= formas <= 50:
    build["formas"] = formas
  if payload.get("kind") in KINDS:
    build["kind"] = payload["kind"]
  build["ten_slots"] = payload.get("ten_slots") is True
  _derive_slot_types(build)
  lib["builds"] = [build] + [b for b in lib["builds"] if b.get("url") != url]
  try:
    _attach_images(lib["builds"], allow_download=True)
  except Exception:
    pass  # les images sont un bonus : l'import réussit sans elles
  save_library(lib)
  return lib, build["id"]


def with_images(lib: dict) -> dict:
  """Copie de la bibliothèque complétée depuis le wiki : images manquantes, et type d'objet
  (donc rôle des emplacements) des builds importés avant son relevé."""
  lib = _copy(lib)
  _attach_images(lib["builds"], allow_download=True)
  _attach_kinds(lib["builds"])
  return lib


def update_build(lib: dict, build_id: str, changes: dict) -> dict:
  """Renomme (custom_name, vide = titre d'origine) et/ou déplace (folder_id, None = non classé)."""
  lib = _copy(lib)
  build = next((b for b in lib["builds"] if b.get("id") == build_id), None)
  if build is None:
    raise ValueError("Build introuvable.")
  if "custom_name" in changes:
    build["custom_name"] = _clean_name(changes["custom_name"], 120) or None
  if "folder_id" in changes:
    folder_id = changes["folder_id"]
    if folder_id is not None and not any(f["id"] == folder_id for f in lib["folders"]):
      raise ValueError("Dossier introuvable.")
    build["folder_id"] = folder_id
  save_library(lib)
  return lib


def delete_build(lib: dict, build_id: str) -> dict:
  lib = _copy(lib)
  lib["builds"] = [b for b in lib["builds"] if b.get("id") != build_id]
  save_library(lib)
  return lib


def _check_folder_name(lib: dict, name: str, exclude_id: str | None = None) -> str:
  name = _clean_name(name, 60)
  if not name:
    raise ValueError("Le nom du dossier ne peut pas être vide.")
  if any(f["name"].lower() == name.lower() and f["id"] != exclude_id for f in lib["folders"]):
    raise ValueError(f"Un dossier « {name} » existe déjà.")
  return name


def create_folder(lib: dict, name: str) -> tuple[dict, str]:
  lib = _copy(lib)
  folder = {"id": f"f{int(time.time() * 1000)}", "name": _check_folder_name(lib, name)}
  lib["folders"].append(folder)
  save_library(lib)
  return lib, folder["id"]


def rename_folder(lib: dict, folder_id: str, name: str) -> dict:
  lib = _copy(lib)
  folder = next((f for f in lib["folders"] if f["id"] == folder_id), None)
  if folder is None:
    raise ValueError("Dossier introuvable.")
  folder["name"] = _check_folder_name(lib, name, exclude_id=folder_id)
  save_library(lib)
  return lib


def delete_folder(lib: dict, folder_id: str) -> dict:
  """Supprime le dossier ; ses builds retournent dans « Non classés », rien n'est effacé."""
  lib = _copy(lib)
  lib["folders"] = [f for f in lib["folders"] if f["id"] != folder_id]
  for b in lib["builds"]:
    if b.get("folder_id") == folder_id:
      b["folder_id"] = None
  save_library(lib)
  return lib


# ---------- Wiki ----------

class _Sanitizer(HTMLParser):
  """Ne garde que des balises de mise en forme sûres ; liens rendus absolus.
  Le HTML produit est équilibré : un fragment découpé au milieu d'une page commence
  souvent par des fermetures orphelines, qui refermeraient le conteneur d'accueil."""

  ALLOWED = {"a", "b", "i", "strong", "em", "br", "p", "ul", "ol", "li", "table", "thead",
             "tbody", "tr", "td", "th", "small", "span", "div", "h3", "h4", "img"}
  DROPPED_WITH_CONTENT = {"script", "style", "sup"}

  def __init__(self) -> None:
    super().__init__(convert_charrefs=True)
    self.out: list[str] = []
    self.skip = 0
    self.open: list[str] = []

  def close(self) -> None:
    super().close()
    while self.open:
      self.out.append(f"</{self.open.pop()}>")

  def handle_starttag(self, tag, attrs):
    if tag in self.DROPPED_WITH_CONTENT:
      self.skip += 1
      return
    if self.skip or tag not in self.ALLOWED:
      return
    attrs = dict(attrs)
    if tag == "br":
      self.out.append("<br>")
      return
    if tag == "img":
      self._image(attrs)
      return
    if tag == "a":
      href = attrs.get("href") or ""
      if href.startswith("/"):
        href = WIKI_BASE + href
      if href.startswith("https://"):
        self.out.append(f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener">')
      else:
        self.out.append("<a>")
    else:
      extra = ""
      if tag in ("td", "th"):
        for key in ("colspan", "rowspan"):
          if str(attrs.get(key) or "").isdigit():
            extra += f' {key}="{attrs[key]}"'
      self.out.append(f"<{tag}{extra}>")
    self.open.append(tag)

  def _image(self, attrs: dict) -> None:
    src = attrs.get("src") or ""
    width, height = str(attrs.get("width") or ""), str(attrs.get("height") or "")
    if not src.startswith("/images/") or not width.isdigit() or not height.isdigit():
      return
    if int(width) > MAX_WIKI_ICON or int(height) > MAX_WIKI_ICON:
      return
    alt = html.escape(attrs.get("alt") or "", quote=True)
    url = html.escape(WIKI_BASE + src, quote=True)
    self.out.append(f'<img src="{url}" alt="{alt}" width="{width}" height="{height}" loading="lazy">')

  def handle_endtag(self, tag):
    if tag in self.DROPPED_WITH_CONTENT:
      self.skip = max(0, self.skip - 1)
      return
    if self.skip or tag not in self.open:
      return  # fermeture orpheline (ou balise non autorisée) : ignorée
    while self.open:
      top = self.open.pop()
      self.out.append(f"</{top}>")
      if top == tag:
        break

  def handle_data(self, data):
    if not self.skip:
      self.out.append(html.escape(data))


def _sanitize(fragment: str) -> str:
  parser = _Sanitizer()
  parser.feed(fragment)
  parser.close()
  return "".join(parser.out)


def _extract_acquisition(page_html: str) -> str | None:
  """Découpe la section d'acquisition (jusqu'au titre de niveau 2 suivant)."""
  for m in re.finditer(r"<h2[\s>].*?</h2>", page_html, re.S):
    ids = [i.lower() for i in re.findall(r'\sid="([^"]+)"', m.group(0))]
    if any(i in ACQUISITION_IDS for i in ids):
      rest = page_html[m.end():]
      nxt = re.search(r'<div class="mw-heading mw-heading2|<h2[\s>]', rest)
      section = rest[:nxt.start()] if nxt else rest
      cleaned = _sanitize(section).strip()
      return cleaned or None
  return None


def _wiki_lookup(name: str) -> dict:
  global _wiki_cache
  with _lock:
    if _wiki_cache is None:
      try:
        _wiki_cache = json.loads(WIKI_CACHE_FILE.read_text(encoding="utf-8"))
      except (OSError, ValueError):
        _wiki_cache = {}
    cached = _wiki_cache.get(name.lower())
  if (cached and cached.get("version") == WIKI_CACHE_VERSION
      and time.time() - cached.get("timestamp", 0) < WIKI_TTL):
    return cached

  resp = requests.get(WIKI_API, timeout=20, headers={"User-Agent": "wfm-price-checker/2.0 (outil perso)"}, params={
    "action": "parse", "page": name, "prop": "text", "redirects": 1,
    "disableeditsection": 1, "disablelimitreport": 1, "format": "json",
  })
  resp.raise_for_status()
  data = resp.json()
  if "parse" not in data:
    result = {"timestamp": time.time(), "version": WIKI_CACHE_VERSION, "found": False}
  else:
    title = data["parse"]["title"]
    result = {
      "timestamp": time.time(),
      "version": WIKI_CACHE_VERSION,
      "found": True,
      "title": title,
      "url": f"{WIKI_BASE}/w/{title.replace(' ', '_')}",
      "acquisition_html": _extract_acquisition(data["parse"]["text"]["*"]),
    }

  with _lock:
    _wiki_cache[name.lower()] = result
    snapshot = dict(_wiki_cache)
  try:
    WIKI_CACHE_FILE.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
  except OSError:
    pass
  return result


# ---------- warframe.market ----------

def _market_lookup(name: str) -> dict | None:
  """Prix non rangé (rang 0) : SMA 90 j et vendeur en jeu le moins cher."""
  global _item_by_name
  if _item_by_name is None:
    _item_by_name = {v["name"].lower(): v for v in core.build_item_index().values()}
  item = _item_by_name.get(name.lower())
  if item is None:
    return None

  core.load_stats_cache()
  stats = core.get_item_stats(item["slug"], {"rank": 0})
  top = core.api_get(f"/orders/item/{item['slug']}/top", params={"rank": 0}) or {}
  my_slug = core.slugify(appdata.ingame_name())
  sellers = [
    o for o in top.get("sell", [])
    if o.get("platinum") is not None
    and (o.get("user") or {}).get("status") == "ingame"
    and (o.get("user") or {}).get("slug") != my_slug
  ]
  lowest = min(sellers, key=lambda o: o["platinum"]) if sellers else None
  return {
    "slug": item["slug"],
    "url": f"https://warframe.market/items/{item['slug']}",
    "sma": round(stats["sma"]) if stats and stats.get("sma") is not None else None,
    "vol_day": stats.get("vol_day") if stats else None,
    "lowest": lowest["platinum"] if lowest else None,
    "seller": (lowest.get("user") or {}).get("ingameName") if lowest else None,
  }


def mod_info(name: str) -> dict:
  name = name.strip()[:80]
  info: dict = {"name": name}
  try:
    info["wiki"] = _wiki_lookup(name)
  except Exception as e:
    info["wiki"] = {"found": False, "error": f"Wiki injoignable : {e}"}
  try:
    info["market"] = _market_lookup(name)
  except Exception as e:
    info["market"] = {"error": f"warframe.market injoignable : {e}"}
  return info
