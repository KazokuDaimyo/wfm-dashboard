# WFM Dashboard

Tableau de bord local pour [warframe.market](https://warframe.market) :

- **Mes prix** : compare tes ordres de vente aux vendeurs en jeu et à la moyenne des ventes (SMA 90 jours), et te conseille le bon prix. Avec un compte connecté, applique les prix en un clic ou automatiquement (mode ⚡ Auto).
- **Analyse Prime** : flips sur les sets vaultés et rentabilité du farm des pièces Prime.
- **Builds** : bibliothèque de builds [Overframe](https://overframe.gg) rangés en dossiers, avec pour chaque mod où l'obtenir (wiki officiel) et son prix.

Tout tourne sur ton PC : rien n'est envoyé ailleurs qu'à warframe.market, au wiki Warframe et à GitHub (vérification des mises à jour).

## Installation (Windows)

1. Télécharge **`WFM-Dashboard.exe`** depuis la page [Releases](../../releases/latest).
2. Double-clique dessus. Au premier lancement, Windows affiche sans doute
   « Windows a protégé votre ordinateur » : clique sur **Informations complémentaires**, puis
   **Exécuter quand même**. (L'exécutable n'est pas signé numériquement, d'où l'avertissement.)
3. Une console s'ouvre, puis le tableau de bord dans ton navigateur. Indique ton **pseudo Warframe** :
   la plateforme est détectée depuis ton profil warframe.market.

**Pour arrêter l'application, ferme la fenêtre de console.** Relancer l'exécutable alors qu'il tourne
déjà rouvre simplement le tableau de bord.

Quand une nouvelle version sort, une pastille ★ apparaît en haut de la page (vérification toutes les
heures) : télécharge le nouvel `.exe` et remplace l'ancien. Tes données sont conservées.

## Options

- **Modifier tes prix en un clic** : connecte ton compte warframe.market depuis l'onglet *Mes prix*.
  Ton mot de passe n'est jamais enregistré, seul le jeton de session est gardé sur ton PC.
- **Importer des builds Overframe** : installe l'extension [Tampermonkey](https://www.tampermonkey.net/),
  active « Autoriser les scripts utilisateur » dans ses détails (`chrome://extensions`), puis clique sur
  *🧩 Installer le bouton Overframe* dans l'onglet *Builds*. Un bouton « ➕ Ajouter au tableau de bord »
  apparaît alors sur chaque page de build Overframe.

## Tes données

Réglages, builds, caches et jeton de connexion sont dans `%APPDATA%\WFM-Dashboard`
(colle ce chemin dans l'Explorateur). Pour désinstaller : supprime l'exécutable et ce dossier.

## Développement

```bash
python server.py          # lance le serveur depuis les sources (http://127.0.0.1:8642)
build.bat                 # fabrique dist\WFM-Dashboard.exe
```

Publier une version : incrémenter `APP_VERSION` dans `version.py`, lancer `build.bat`, puis créer une
release GitHub nommée `vX.Y.Z` avec `dist\WFM-Dashboard.exe` en pièce jointe.
