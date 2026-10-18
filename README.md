# Analyse du jour (foot + NHL)

Site installable sur téléphone, mis à jour automatiquement deux fois par jour. Pour chaque match du jour, il affiche :

- les chances de victoire tirées des cotes ;
- le score le plus probable et les buts attendus ;
- pour chaque équipe, les 6 joueurs les plus susceptibles de marquer, avec leur chance d'être décisifs (but ou passe) ;
- pour la NHL, les gardiens probables et la fatigue (deuxième match en deux soirs).

Un onglet **Bilan** compare chaque pronostic au résultat réel.

**100 % gratuit, aucune clé d'API.** Sources :
- l'API publique d'ESPN pour le foot (Ligue 1, Premier League, LaLiga, Serie A, Bundesliga, Ligue des champions), les effectifs et stats des joueurs, et les cotes NHL ;
- l'API officielle de la NHL pour le reste.

Le calcul et l'hébergement passent par GitHub, gratuitement.

## Mise en place (15 minutes, une seule fois)

1. **Crée un compte** sur https://github.com/signup.
2. **Crée un dépôt** : bouton « + » en haut à droite, puis « New repository ».
   - Nom : `analyse-sport`.
   - Visibilité : **Public**, nécessaire pour l'hébergement gratuit.
   - Clique « Create repository ».
3. **Envoie les fichiers** : lien « uploading an existing file ». Glisse tout le contenu du dossier décompressé (`app.py`, `requirements.txt`, `README.md`, les dossiers `docs` et `tests`), puis « Commit changes ».
4. **Ajoute l'automatisation.** Le dossier `.github` est caché sur Mac et souvent oublié, donc crée-le à la main :
   - « Add file », puis « Create new file » ;
   - nom du fichier, tape exactement : `.github/workflows/update.yml` ;
   - colle le contenu du fichier `update.yml` fourni, puis « Commit changes ».
5. **Autorise l'automatisation à écrire** : Settings, Actions, General, section « Workflow permissions ». Coche « Read and write permissions », puis « Save ».
6. **Active le site** : Settings, Pages. Dans « Build and deployment », choisis Source « Deploy from a branch », Branch `main`, dossier `/docs`, puis « Save ».
7. **Lance la première analyse** : onglet Actions, workflow « Mise à jour des analyses », bouton « Run workflow ».
8. **Ouvre ton site** 2 à 3 minutes plus tard : `https://TON-PSEUDO.github.io/analyse-sport/`.
   - Sur iPhone : Partager, puis « Sur l'écran d'accueil ».
   - Sur Android : menu ⋮, puis « Ajouter à l'écran d'accueil ».

## Comment savoir si ça marche

- Dans l'onglet **Actions**, chaque passage affiche une coche verte. Une croix rouge signifie une erreur : clique dessus pour lire le message, ou envoie-le moi.
- En haut du site, la date de mise à jour doit changer chaque jour vers 10 h et 18 h 30. GitHub peut décaler un passage de quelques minutes.
- Si une source ne répond plus, l'encadré « À savoir aujourd'hui » l'indique.

## Limites à connaître

- **L'API d'ESPN n'est pas officielle.** Si ESPN change son format, la partie foot s'arrête jusqu'à correction. La NHL continue de fonctionner.
- **Les cotes viennent d'un seul bookmaker** (celui affiché par ESPN), donc elles sont un peu moins précises qu'une moyenne de plusieurs.
- **Les blessés ne sont pas vérifiés automatiquement.**
  - NHL : seuls les joueurs alignés au match précédent sont retenus.
  - Foot : les blessés ne sont pas filtrés (ESPN ne les publie pas), vérifie les compositions avant un match.
- **En Ligue des champions**, les stats des joueurs viennent du championnat national du club. Pour les clubs dont ESPN ne suit pas le championnat (Azerbaïdjan, Tchéquie…), seuls les matchs européens comptent : le site le signale et les chiffres sont très incertains.
- **Les gardiens NHL sont une projection** : le gardien le plus utilisé, ou son remplaçant si l'équipe a joué la veille. Ils ne sont pas confirmés.
- **Le Bilan n'a de sens qu'après 200 à 300 matchs.**
- **GitHub met en pause les automatisations planifiées** d'un dépôt resté 60 jours sans activité. Les mises à jour quotidiennes comptent normalement comme de l'activité. Si ça s'arrête, relance-le depuis l'onglet Actions.

## Alertes en direct (robot Cloudflare + app ntfy)

- **Étoile ☆ à côté de l'heure** : met un match en favori. Seuls les favoris déclenchent des alertes.
- **Le robot** (`worker/`, offre gratuite Cloudflare Workers) se réveille chaque minute et lit les scores ESPN. Il envoie sur l'app ntfy :
  - un rappel 15 min avant le match ;
  - les buts et les cartons rouges ;
  - un pronostic qui bascule ;
  - le résultat final ;
  - un résumé chaque matin.
- **Délai** : 1 à 2 minutes, téléphone verrouillé et PC éteint.
- **Pronostics › En direct** recalcule les pourcentages dans la page à partir du score, de la minute et des cartons rouges (`docs/live.js`, partagé avec le robot). Ces chiffres n'entrent pas dans le Bilan.
- **Le canal ntfy** (« code d'alerte ») n'est jamais dans le dépôt. Il est enregistré dans Cloudflare comme secret `NTFY_TOPIC`.
- **État du robot** : adresse du robot suivie de `/etat`.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | Récupère les données, calcule, écrit `docs/data.json`, `docs/live.json` et `docs/history.json` |
| `docs/index.html` | Le site (onglets Foot, NHL, Pronostics, Bilan) |
| `docs/live.js` | Calcul des pronostics en direct (site et robot) |
| `worker/`, `wrangler.jsonc` | Robot d'alertes Cloudflare. Tests : `node worker/test.mjs` |
| `docs/manifest.webmanifest`, `docs/icon.svg` | Installation sur l'écran d'accueil |
| `.github/workflows/update.yml` | Lance `app.py` aux 7 passages de `horaires.py` |
| `tests/test_app.py` | Vérifie les calculs avant chaque mise à jour |

Pour tester sur ton ordinateur : `pip install -r requirements.txt`, puis `python -m unittest discover -s tests`, puis `python app.py`.
