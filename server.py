"""Petit serveur web local servant le tableau de bord (http://127.0.0.1:8642).
Aucun scan n'est lancé sans action de l'utilisateur : bouton Rescanner ou mode auto."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

import appdata
import builds
import main as core
import prime_analysis
import updates
from version import APP_VERSION

HOST = "127.0.0.1"
PORT = int(os.environ.get("WFM_PORT", 8642))  # autre port : tester une copie sans arrêter l'application
FAST_IDLE_INTERVAL = 60   # cadence du mode auto quand tout est déjà optimal
MAX_CHANGES = 200         # taille du journal des changements

ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}

INDEX_FILE = appdata.resource("index.html")
EXPORT_FILE = appdata.resource("overframe_export.js")

USERSCRIPT_TEMPLATE = """// ==UserScript==
// @name         Warframe Market — Ajouter le build au tableau de bord
// @namespace    wfm-price-checker
// @version      {version}
// @description  Ajoute un bouton sur les pages de build Overframe pour envoyer le build au tableau de bord local.
// @match        https://overframe.gg/*
// @grant        none
// @sandbox      raw
// @updateURL    {dash}/wfm-overframe.user.js
// @downloadURL  {dash}/wfm-overframe.user.js
// ==/UserScript==
(function () {{
  "use strict";
  const DASH = {dash_json};
{export_code}
  const BUTTON_ID = "wfm-add-build";
  // Vérifié chaque seconde : le bouton suit la page même si Overframe change d'adresse sans recharger.
  const sync = () => {{
    const onBuild = /^\\/build\\/\\d+\\//.test(location.pathname);
    const existing = document.getElementById(BUTTON_ID);
    if (onBuild && !existing && document.body) {{
      const btn = document.createElement("button");
      btn.id = BUTTON_ID;
      btn.type = "button";
      btn.textContent = "➕ Ajouter au tableau de bord";
      btn.style.cssText = "position:fixed;right:24px;bottom:24px;z-index:2147483647;padding:12px 20px;"
        + "border-radius:10px;border:1px solid #e0b45c;background:#2a2416;color:#e0b45c;cursor:pointer;"
        + "font:600 14px 'Segoe UI',system-ui,sans-serif;box-shadow:0 6px 20px rgba(0,0,0,.5);";
      btn.addEventListener("click", () => overframeExport(DASH));
      document.body.appendChild(btn);
    }} else if (!onBuild && existing) {{
      existing.remove();
    }}
  }};
  sync();
  setInterval(sync, 1000);
}})();
"""
TOKEN_FILE = appdata.data_file("wfm_token.txt")  # jeton JWT warframe.market, propre à chaque utilisateur

rescan_event = threading.Event()
state_lock = threading.Lock()
state = {
  "version": APP_VERSION,
  "update": None,  # renseigné par updates.py quand une nouvelle version est publiée
  "scan_count": 0,
  "last_scan_at": None,
  "next_scan_at": None,
  "scanning": False,
  "progress": [0, 0],
  "error": None,
  "results": [],
  "summary": None,
  "changes": [],
  "prime": prime_analysis.load_saved(),
  "fast_mode": False,
  "library": builds.load_library(),
}


def save_settings(name: str, platform) -> tuple[int, dict]:
  """Vérifie le pseudo sur warframe.market puis enregistre les réglages. Sans plateforme
  choisie, reprend celle du profil. Retourne (code HTTP, réponse JSON)."""
  name = " ".join(name.split())
  if not name:
    return 400, {"ok": False, "message": "Indique ton pseudo Warframe."}
  if platform is not None and platform not in appdata.PLATFORMS:
    return 400, {"ok": False, "message": "Plateforme inconnue."}

  core.rate_limiter.wait()
  try:
    resp = requests.get(f"{core.BASE_URL}/user/{core.slugify(name)}", timeout=15, headers={
      "Accept": "application/json", "User-Agent": f"WFM-Dashboard/{APP_VERSION}",
      "Platform": platform or "pc",
    })
  except requests.RequestException as e:
    return 502, {"ok": False, "message": f"warframe.market injoignable : {e}"}
  if resp.status_code == 404:
    return 404, {"ok": False, "message": f"Aucun joueur « {name} » sur warframe.market : vérifie l'orthographe."}
  if not resp.ok:
    return 502, {"ok": False, "message": f"warframe.market a répondu {resp.status_code}, réessaie."}

  profile = resp.json().get("data") or {}
  real_name = profile.get("ingameName") or name
  detected = profile.get("platform")
  chosen = platform or (detected if detected in appdata.PLATFORMS else "pc")

  previous = appdata.settings()
  appdata.save_settings(real_name, chosen)
  if previous["ingame_name"] and core.slugify(previous["ingame_name"]) != core.slugify(real_name):
    logout()  # le jeton enregistré appartient à l'ancien compte
  if (previous["ingame_name"], previous["platform"]) != (real_name, chosen):
    with state_lock:  # les résultats affichés concernaient un autre compte ou une autre plateforme
      state.update(fast_mode=False, results=[], summary=None, changes=[], scan_count=0,
                   last_scan_at=None, next_scan_at=None, error=None)
  return 200, {"ok": True, "ingame_name": real_name, "platform": chosen, "detected_platform": detected}


def read_token() -> str | None:
  try:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
  except OSError:
    return None
  if token.startswith("JWT "):  # tolère un jeton collé avec son préfixe v1
    token = token[4:].strip()
  return token or None


def signin(email: str, password: str) -> tuple[int, str]:
  """Échange email/mot de passe contre un jeton JWT (flux v1, comme AlecaFrame).
  Le mot de passe n'est jamais stocké : seul le jeton est écrit dans wfm_token.txt."""
  core.rate_limiter.wait()
  try:
    resp = requests.post(
      f"{core.V1_BASE_URL}/auth/signin",
      json={"auth_type": "header", "email": email, "password": password},
      timeout=15,
      headers={
        "Authorization": "JWT",  # exigé par l'API, même vide, pour obtenir un jeton
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Platform": appdata.platform(),
        "Language": core.LANGUAGE,
      },
    )
  except requests.RequestException as e:
    return 502, f"Erreur réseau : {e}"

  if resp.status_code in (400, 401, 403):
    return 401, "Identifiants refusés par warframe.market. (Compte créé via Google/Discord ? Utilise la méthode du cookie JWT.)"
  if not resp.ok:
    return resp.status_code, f"Échec de la connexion ({resp.status_code})."

  token = resp.headers.get("Authorization", "")
  if token.startswith("JWT "):
    token = token[4:].strip()
  if not token:
    return 502, "Connexion acceptée mais aucun jeton reçu — utilise la méthode du cookie JWT."

  try:
    TOKEN_FILE.write_text(token, encoding="utf-8")
  except OSError as e:
    return 500, f"Impossible d'écrire wfm_token.txt : {e}"
  return 200, "ok"


def logout() -> None:
  try:
    TOKEN_FILE.unlink(missing_ok=True)
  except OSError:
    pass


def apply_price(order_id: str, platinum: int, source: str = "via le dashboard") -> tuple[int, str]:
  """Modifie le prix d'un de nos ordres sur warframe.market (API v1 authentifiée)."""
  token = read_token()
  if not token:
    return 400, "Aucun jeton configuré : crée le fichier wfm_token.txt à côté de server.py."

  with state_lock:
    r = next((x for x in state["results"] if x.get("order_id") == order_id), None)
  if r is None:
    return 404, "Ordre introuvable dans le dernier scan."

  core.rate_limiter.wait()
  try:
    resp = requests.patch(
      f"{core.BASE_URL}/order/{order_id}",
      json={"platinum": platinum},
      timeout=15,
      headers={
        "Authorization": f"Bearer {token}",
        "Platform": appdata.platform(),
        "Language": core.LANGUAGE,
        "Accept": "application/json",
        "Content-Type": "application/json",
      },
    )
  except requests.RequestException as e:
    return 502, f"Erreur réseau : {e}"

  if resp.status_code in (401, 403):
    return 401, "Jeton invalide ou expiré — reconnecte-toi sur warframe.market et mets à jour wfm_token.txt."
  if not resp.ok:
    return resp.status_code, f"L'API a refusé la modification ({resp.status_code})."

  # met à jour l'état local sans attendre le prochain scan
  now = time.time()
  with state_lock:
    old = r["my_price"]
    r["my_price"] = platinum
    r["statut"] = status_of(r)
    state["results"].sort(key=sort_key)
    state["summary"] = summarize(state["results"])
    state["changes"].insert(0, {
      "time": now, "item": r["name"], "kind": "applied",
      "text": f"prix modifié {source} : {old}p → {platinum}p",
    })
    del state["changes"][MAX_CHANGES:]
  return 200, "ok"


def status_of(r: dict) -> str:
  if r["conseille"] is None:
    return "no_data"
  if r["my_price"] > r["conseille"]:
    return "trop_cher"
  if r["my_price"] < r["conseille"]:
    return "trop_bas"
  return "optimal"


def sort_key(r: dict):
  if r["conseille"] is None:
    return (3, 0)
  diff = r["my_price"] - r["conseille"]
  if diff > 0:
    return (0, -diff)
  if diff < 0:
    return (1, diff)
  return (2, 0)


def summarize(results: list) -> dict:
  a_baisser = [r for r in results if r["statut"] == "trop_cher"]
  a_monter = [r for r in results if r["statut"] == "trop_bas"]
  return {
    "total": len(results),
    "a_baisser": len(a_baisser),
    "perte": sum(r["my_price"] - r["conseille"] for r in a_baisser),
    "a_monter": len(a_monter),
    "gain": sum(r["conseille"] - r["my_price"] for r in a_monter),
    "optimal": sum(1 for r in results if r["statut"] == "optimal"),
  }


def diff_changes(prev: list, new: list, now: float) -> list:
  """Compare deux scans et produit les entrées du journal des changements."""
  changes = []
  prev_by_name = {r["name"]: r for r in prev}
  new_by_name = {r["name"]: r for r in new}
  fields = [
    ("my_price", "ton prix"),
    ("lowest", "+ bas en jeu"),
    ("sma", "SMA"),
    ("conseille", "conseillé"),
  ]

  for name, r in new_by_name.items():
    old = prev_by_name.get(name)
    if old is None:
      changes.append({"time": now, "item": name, "kind": "new", "text": "nouvel ordre détecté"})
      continue
    for field, label in fields:
      if old.get(field) != r.get(field):
        fmt = lambda v: f"{v}p" if v is not None else "-"
        kind = "conseil" if field == "conseille" else "change"
        changes.append({
          "time": now, "item": name, "kind": kind,
          "text": f"{label} : {fmt(old.get(field))} → {fmt(r.get(field))}",
        })

  for name in prev_by_name:
    if name not in new_by_name:
      changes.append({"time": now, "item": name, "kind": "sold", "text": "ordre disparu (vendu ?)"})

  return changes


def run_scan(item_index: dict, my_slug: str) -> list:
  orders = core.get_my_sell_orders(my_slug)
  total = len(orders)
  with state_lock:
    state["progress"] = [0, total]

  results = []
  with ThreadPoolExecutor(max_workers=core.MAX_WORKERS) as pool:
    futures = [pool.submit(core.check_order, o, item_index, my_slug) for o in orders]
    for i, future in enumerate(futures):
      r = future.result()
      with state_lock:
        state["progress"] = [i + 1, total]
      if r:
        r["statut"] = status_of(r)
        results.append(r)

  results.sort(key=sort_key)
  return results


def auto_apply() -> int:
  """Mode auto : applique tous les prix conseillés. Retourne le nombre de succès."""
  with state_lock:
    targets = [
      (r["order_id"], r["conseille"])
      for r in state["results"]
      if r.get("order_id") and r.get("conseille") is not None and r["conseille"] != r["my_price"]
    ]
  applied = 0
  for order_id, price in targets:
    status, _ = apply_price(order_id, price, source="(mode auto)")
    if status == 200:
      applied += 1
    elif status == 401:
      with state_lock:
        state["fast_mode"] = False
        state["changes"].insert(0, {
          "time": time.time(), "item": "Mode auto", "kind": "sold",
          "text": "désactivé automatiquement : jeton expiré, reconnecte ton compte",
        })
      break
  return applied


def scan_loop() -> None:
  """Aucun scan automatique : la boucle attend un déclencheur explicite
  (bouton Rescanner) et n'enchaîne les cycles que tant que le mode auto est actif."""
  core.load_stats_cache()
  item_index = None  # construit au premier scan, pour ne rien télécharger au démarrage

  while True:
    with state_lock:
      fast = state["fast_mode"]
    if fast:
      triggered = rescan_event.wait(timeout=FAST_IDLE_INTERVAL)
    else:
      triggered = rescan_event.wait()  # bloque tant que l'utilisateur ne demande rien
    rescan_event.clear()

    with state_lock:
      fast = state["fast_mode"]
    if not triggered and not fast:
      continue  # cadence auto expirée mais le mode vient d'être coupé : on ne scanne pas

    with state_lock:
      state["scanning"] = True
      state["error"] = None
      prev = state["results"]

    try:
      name = appdata.ingame_name()  # relu à chaque scan : le pseudo peut changer dans les réglages
      if not name:
        raise ValueError("Aucun pseudo configuré : renseigne-le dans les réglages (⚙️).")
      if item_index is None:
        item_index = core.build_item_index()
      results = run_scan(item_index, core.slugify(name))
      now = time.time()
      new_changes = diff_changes(prev, results, now) if prev else []
      core.save_json_cache(core.STATS_CACHE_FILE, core.stats_cache)
      with state_lock:
        state["results"] = results
        state["summary"] = summarize(results)
        state["changes"] = (new_changes + state["changes"])[:MAX_CHANGES]
        state["scan_count"] += 1
        state["last_scan_at"] = now
    except (SystemExit, Exception) as e:  # le serveur ne doit jamais mourir sur un scan raté
      traceback.print_exc()
      with state_lock:
        state["error"] = str(e) or "Erreur pendant le scan"

    with state_lock:
      fast = state["fast_mode"]
    applied = auto_apply() if fast else 0

    with state_lock:
      fast = state["fast_mode"]  # peut avoir été coupé pendant l'application (jeton expiré)
      state["scanning"] = False
      if fast:
        state["next_scan_at"] = time.time() + (0 if applied else FAST_IDLE_INTERVAL)
      else:
        state["next_scan_at"] = None  # pas de mode auto : aucun scan planifié

    if applied and fast:
      rescan_event.set()  # des prix ont bougé : re-scan immédiat pour vérifier la position


class Handler(BaseHTTPRequestHandler):
  def do_GET(self) -> None:
    if self.path in ("/", "/index.html"):
      try:
        body = INDEX_FILE.read_bytes()
      except OSError:
        self.send_error(500, "index.html introuvable")
        return
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)
    elif self.path == "/api/state":
      settings = appdata.settings()
      with state_lock:
        body = json.dumps({
          **state, "now": time.time(), "can_update": read_token() is not None,
          "ingame_name": settings["ingame_name"], "platform": settings["platform"],
          "platforms": appdata.PLATFORMS, "needs_setup": not settings["ingame_name"],
        }).encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)
    elif self.path == "/overframe-export.js":
      try:
        body = EXPORT_FILE.read_bytes()
      except OSError:
        self.send_error(500, "overframe_export.js introuvable")
        return
      self.send_bytes(body, "text/javascript; charset=utf-8")
    elif self.path == "/wfm-overframe.user.js":
      try:
        export_code = EXPORT_FILE.read_text(encoding="utf-8")
        version = f"1.{int(EXPORT_FILE.stat().st_mtime)}"
      except OSError:
        self.send_error(500, "overframe_export.js introuvable")
        return
      # l'adresse du tableau de bord est celle par laquelle le script a été installé
      dash = f"http://{self.headers.get('Host', '')}"
      if dash not in ALLOWED_ORIGINS:
        dash = f"http://{HOST}:{PORT}"
      script = USERSCRIPT_TEMPLATE.format(
        version=version, dash=dash, dash_json=json.dumps(dash),
        export_code="\n".join("  " + line if line else "" for line in export_code.splitlines()),
      )
      self.send_bytes(script.encode("utf-8"), "text/javascript; charset=utf-8")
    elif self.path.startswith("/api/mod-info?"):
      name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0].strip()
      if not name:
        self.send_json(400, {"ok": False, "message": "Nom manquant."})
        return
      self.send_json(200, {"ok": True, **builds.mod_info(name)})
    else:
      self.send_error(404)

  def send_bytes(self, body: bytes, content_type: str) -> None:
    self.send_response(200)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)

  def library_op(self, operation, reply: bool = True) -> bool:
    """Applique operation(bibliothèque, requête JSON) sous verrou ; répond 400 avec le message
    en cas de requête invalide. Retourne True si l'opération a réussi."""
    try:
      req = self.read_json_body()
      if not isinstance(req, dict):
        raise ValueError("Requête invalide.")
      with state_lock:
        state["library"] = operation(state["library"], req)
    except (ValueError, KeyError, TypeError) as e:
      message = str(e) if isinstance(e, ValueError) and str(e) else "Requête invalide."
      self.send_json(400, {"ok": False, "message": message})
      return False
    if reply:
      self.send_json(200, {"ok": True})
    return True

  def read_json_body(self):
    length = int(self.headers.get("Content-Length", 0))
    return json.loads(self.rfile.read(length))

  def send_json(self, status: int, obj: dict) -> None:
    body = json.dumps(obj).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def do_POST(self) -> None:
    # Un autre site ouvert dans le navigateur ne doit pas pouvoir piloter le serveur
    # (lancer un scan, modifier un prix…) : seules les requêtes du tableau de bord passent.
    origin = self.headers.get("Origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
      self.send_json(403, {"ok": False, "message": "Origine refusée."})
      return

    if self.path == "/api/rescan":
      with state_lock:
        already = state["scanning"]
      if not already:
        print(f"[{time.strftime('%H:%M:%S')}] Scan demandé : bouton Rescanner", flush=True)
        rescan_event.set()
      self.send_json(200, {"ok": True, "already_scanning": already})
    elif self.path == "/api/set-price":
      try:
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        order_id = str(req["order_id"])
        platinum = int(req["platinum"])
        if not (1 <= platinum <= 999999):
          raise ValueError
      except (ValueError, KeyError, TypeError):
        self.send_json(400, {"ok": False, "message": "Requête invalide."})
        return
      status, message = apply_price(order_id, platinum)
      self.send_json(status, {"ok": status == 200, "message": message})
    elif self.path == "/api/login":
      try:
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        email = str(req["email"]).strip()
        password = str(req["password"])
        if not email or not password:
          raise ValueError
      except (ValueError, KeyError, TypeError):
        self.send_json(400, {"ok": False, "message": "Email et mot de passe requis."})
        return
      status, message = signin(email, password)
      self.send_json(status, {"ok": status == 200, "message": message})
    elif self.path == "/api/logout":
      logout()
      self.send_json(200, {"ok": True})
    elif self.path == "/api/fast-mode":
      try:
        length = int(self.headers.get("Content-Length", 0))
        enabled = bool(json.loads(self.rfile.read(length))["enabled"])
      except (ValueError, KeyError, TypeError):
        self.send_json(400, {"ok": False, "message": "Requête invalide."})
        return
      if enabled and read_token() is None:
        self.send_json(400, {"ok": False, "message": "Connecte ton compte warframe.market d'abord."})
        return
      with state_lock:
        state["fast_mode"] = enabled
      print(f"[{time.strftime('%H:%M:%S')}] Mode auto {'activé' if enabled else 'désactivé'}", flush=True)
      if enabled:
        rescan_event.set()  # démarre un cycle tout de suite
      self.send_json(200, {"ok": True, "enabled": enabled})
    elif self.path == "/api/settings":
      try:
        req = self.read_json_body()
        status, reply = save_settings(str(req.get("ingame_name", "")), req.get("platform"))
      except (ValueError, TypeError, AttributeError):
        status, reply = 400, {"ok": False, "message": "Requête invalide."}
      self.send_json(status, reply)
    elif self.path == "/api/builds/import":
      try:
        payload = self.read_json_body()
        if not isinstance(payload, dict):
          raise ValueError("Requête invalide.")
        with state_lock:
          state["library"], build_id = builds.import_build(state["library"], payload)
      except (ValueError, TypeError) as e:
        self.send_json(400, {"ok": False, "message": str(e) or "Requête invalide."})
        return
      self.send_json(200, {"ok": True, "id": build_id})
    elif self.path == "/api/builds/images":
      try:
        with state_lock:
          snapshot = json.loads(json.dumps(state["library"]))
        updated = {b["id"]: b for b in builds.with_images(snapshot)["builds"]}
        with state_lock:
          # Seules les images sont reprises de la copie : un renommage ou un déplacement
          # fait pendant le téléchargement ne doit pas être écrasé.
          lib = json.loads(json.dumps(state["library"]))
          for b in lib["builds"]:
            fresh = updated.get(b["id"])
            if not fresh:
              continue
            if fresh.get("item_image"):
              b["item_image"] = fresh["item_image"]
            for key in ("kind", "ten_slots", "kind_checked"):
              if key in fresh:
                b[key] = fresh[key]
            images = {e["name"]: e.get("image") for e in fresh["mods"] + fresh["arcanes"]}
            slot_types = {e["name"]: e.get("slot_type") for e in fresh["mods"]}
            for e in b["mods"] + b["arcanes"]:
              if images.get(e["name"]):
                e["image"] = images[e["name"]]
              if slot_types.get(e["name"]) is not None:
                e["slot_type"] = slot_types[e["name"]]
          state["library"] = lib
          builds.save_library(lib)
      except Exception as e:
        self.send_json(502, {"ok": False, "message": f"Images indisponibles : {e}"})
        return
      self.send_json(200, {"ok": True})
    elif self.path == "/api/builds/delete":
      self.library_op(lambda lib, req: builds.delete_build(lib, str(req["id"])))
    elif self.path == "/api/builds/update":
      # {"id", "custom_name"?, "folder_id"?} : seules les clés présentes sont modifiées
      self.library_op(lambda lib, req: builds.update_build(
        lib, str(req["id"]), {k: req[k] for k in ("custom_name", "folder_id") if k in req}))
    elif self.path == "/api/folders/create":
      created = {}
      def create(lib, req):
        lib, created["id"] = builds.create_folder(lib, req["name"])
        return lib
      if self.library_op(create, reply=False):
        self.send_json(200, {"ok": True, "id": created["id"]})
    elif self.path == "/api/folders/rename":
      self.library_op(lambda lib, req: builds.rename_folder(lib, str(req["id"]), req["name"]))
    elif self.path == "/api/folders/delete":
      self.library_op(lambda lib, req: builds.delete_folder(lib, str(req["id"])))
    elif self.path == "/api/prime-scan":
      with state_lock:
        running = state["prime"].get("status") == "running"
      if not running:
        threading.Thread(
          target=prime_analysis.run, args=(state, state_lock), daemon=True,
        ).start()
      self.send_json(200, {"ok": True, "already_running": running})
    else:
      self.send_error(404)

  def log_message(self, fmt: str, *args) -> None:
    pass  # pas de log par requête, le polling rendrait la console illisible


def already_running() -> bool:
  try:
    resp = requests.get(f"http://{HOST}:{PORT}/api/state", timeout=2)
    return resp.ok and "version" in resp.json()
  except (requests.RequestException, ValueError):
    return False


def on_update(info: dict) -> None:
  with state_lock:
    already_known = (state["update"] or {}).get("version") == info["version"]
    state["update"] = info
  if not already_known:
    print(f"\n★ Nouvelle version {info['version']} disponible : {info['page']}\n", flush=True)


def main(open_browser: bool = False) -> None:
  url = f"http://{HOST}:{PORT}"
  if already_running():
    print(f"Le tableau de bord tourne déjà : {url}")
    if open_browser:
      webbrowser.open(url)
    return
  try:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
  except OSError:
    raise SystemExit(f"Le port {PORT} est déjà utilisé par un autre programme : impossible de démarrer.")

  threading.Thread(target=scan_loop, daemon=True).start()
  updates.start(on_update)
  print(f"WFM Dashboard {APP_VERSION} — tableau de bord sur {url}")
  print("Aucun scan automatique : utilise le bouton Rescanner ou le mode auto.")
  print("Pour arrêter l'application, ferme cette fenêtre (ou Ctrl+C).", flush=True)
  if open_browser:
    threading.Timer(0.3, webbrowser.open, [url]).start()
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\nArrêt du serveur.")


if __name__ == "__main__":
  main()
