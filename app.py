"""Point d'entrée de l'exécutable WFM-Dashboard.exe : démarre le serveur local et ouvre le
tableau de bord dans le navigateur. Fermer la fenêtre de console arrête l'application."""
from __future__ import annotations

import sys
import traceback


def _exit_with_launcher() -> None:
  """Un exécutable PyInstaller « un seul fichier » tourne dans un processus enfant de son
  lanceur. Si le lanceur disparaît (fin de tâche…), l'enfant s'arrête aussi au lieu de
  garder le port occupé en arrière-plan."""
  if not getattr(sys, "frozen", False) or sys.platform != "win32":
    return
  import ctypes
  import os
  import threading
  synchronize, infinite = 0x00100000, 0xFFFFFFFF
  handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, os.getppid())
  if not handle:
    return

  def wait() -> None:
    ctypes.windll.kernel32.WaitForSingleObject(handle, infinite)
    os._exit(0)

  threading.Thread(target=wait, daemon=True, name="launcher-watch").start()


def run() -> None:
  _exit_with_launcher()
  if sys.platform == "win32":
    import ctypes
    ctypes.windll.kernel32.SetConsoleTitleW("WFM Dashboard — ferme cette fenêtre pour quitter")
  try:
    import server
    server.main(open_browser=True)
  except KeyboardInterrupt:
    pass
  except SystemExit as e:
    if e.code not in (None, 0):  # message d'erreur au démarrage (port occupé…)
      print(e.code)
      input("\nAppuie sur Entrée pour fermer…")
  except Exception:
    # sans cela la fenêtre se fermerait immédiatement et l'erreur serait perdue
    traceback.print_exc()
    input("\nUne erreur est survenue. Appuie sur Entrée pour fermer…")


if __name__ == "__main__":
  run()
