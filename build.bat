@echo off
rem Fabrique dist\WFM-Dashboard.exe (un seul fichier, avec console).
rem PyInstaller est installé dans .venv-build pour ne pas toucher au Python principal.
rem Seuls le code et les deux ressources web entrent dans l'exécutable : les données
rem personnelles vivent dans %%APPDATA%%\WFM-Dashboard et n'y sont jamais incluses.
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv-build (
  echo Création de l'environnement de fabrication...
  python -m venv .venv-build || goto :error
)
call .venv-build\Scripts\activate.bat || goto :error
python -m pip install --quiet --upgrade pip || goto :error
python -m pip install --quiet -r requirements.txt pyinstaller || goto :error

python -m PyInstaller --noconfirm --clean --onefile --console ^
  --name WFM-Dashboard ^
  --add-data "index.html;." ^
  --add-data "overframe_export.js;." ^
  app.py || goto :error

echo.
echo Terminé : dist\WFM-Dashboard.exe
exit /b 0

:error
echo.
echo La fabrication a échoué.
exit /b 1
