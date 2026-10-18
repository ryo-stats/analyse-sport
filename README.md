# Analyse du jour (foot + NHL)

Site installable sur téléphone, mis à jour automatiquement deux fois par jour. Pour chaque match du jour, il affiche :

- les chances de victoire tirées des cotes ;
- le score le plus probable et les buts attendus ;
- pour chaque équipe, les 6 joueurs les plus susceptibles de marquer, avec leur chance d'être décisifs (but ou passe) ;
- pour la NHL, les gardiens probables et la fatigue (deuxième match en deux soirs).

Un onglet **Bilan** compare chaque pronostic au résultat réel. Quand un match est fini, sa ligne affiche le nombre de pronostics validés (par exemple « 3/5 ✓ »), et l'onglet Pronostics met ✓ ou ✗ devant chacun.

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
  - Foot : les blessés ne sont pas filtrés tant que les compositions ne sont pas sorties. Dès qu'ESPN publie la composition officielle (environ 1 h avant le match), elle est prise en compte au passage suivant : un remplaçant voit ses chances réduites (il joue en moyenne 25 minutes), un joueur absent tombe à 0 %, et le pronostic buteur va à un titulaire. Les passages de 12 h 20, 14 h 20, 16 h 20, 17 h 50 et 20 h 20 couvrent la plupart des coups d'envoi ; pour un match à 13 h 30 ou 18 h 30, la compo peut sortir après le dernier passage, vérifie-la alors toi-même.
- **En Ligue des champions**, les stats des joueurs viennent du championnat national du club. Pour les clubs dont ESPN ne suit pas le championnat (Azerbaïdjan, Tchéquie…), seuls les matchs européens comptent : le site le signale et les chiffres sont très incertains.
- **Les gardiens NHL sont une projection** : le gardien le plus utilisé, ou son remplaçant si l'équipe a joué la veille. Ils ne sont pas confirmés.
- **Le Bilan n'a de sens qu'après 200 à 300 matchs.**
- **GitHub met en pause les automatisations planifiées** d'un dépôt resté 60 jours sans activité. Les mises à jour quotidiennes comptent normalement comme de l'activité. Si ça s'arrête, relance-le depuis l'onglet Actions.

## Alertes en direct (site ouvert, sans appli ni compte)

- **Chacun ses favoris** : l'étoile ☆ à côté de l'heure met un match en favori, 15 au plus par téléphone. Ils sont regroupés dans l'onglet ★ Favoris. Chaque téléphone ne reçoit que les alertes de SES favoris.
- **Notifications du site** (Web Push, `docs/sw.js`) : onglet ★ Favoris, puis **Activer les notifications**. Un guide pas à pas s'y trouve, et un bandeau le signale en haut du site.
  - Android et PC : Chrome, Edge, Firefox, Samsung Internet.
  - iPhone : il faut d'abord ajouter le site à l'écran d'accueil (iOS 16.4+), puis l'ouvrir depuis l'icône.
- **Ce qui est envoyé** :
  - les buts (et les buts annulés) ;
  - le résultat final, avec le compte des pronostics validés et chaque pronostic ✅ ou ❌ : Safe, Vainqueur, Buteur, Fun et score exact ;
  - le résumé du matin vers 8 h, seulement pour ceux qui l'activent.
- **Délai** : 1 à 2 minutes, téléphone verrouillé.
- **Le robot** (`worker/`, offre gratuite Cloudflare Workers) se réveille chaque minute :
  - il suit les matchs mis en favori par au moins une personne et lit les scores ESPN ;
  - il reconnaît chaque téléphone à son adresse d'abonnement, sans compte, sans e-mail et sans code ;
  - il n'accepte que les adresses des vrais services de notification (Google, Apple, Mozilla, Microsoft) ;
  - il crée lui-même ses clés VAPID ; le chiffrement est vérifié sur l'exemple de la RFC 8291.
- **Envoi réparti** : chaque minute, au plus 10 lots de 40 notifications, chacun confié à un petit robot « Envoi ». Le surplus part la minute suivante. De quoi servir quelques centaines de personnes gratuitement (confiance moyenne). Au-delà, il faudrait l'offre payante de Cloudflare (5 $ par mois).
- **Libération des favoris** : un favori se libère 8 h après le coup d'envoi.
- **Abonnements expirés** : ils sont oubliés automatiquement.
- **Pronostics › En direct** recalcule les pourcentages dans la page à partir du score, de la minute et des cartons rouges (`docs/live.js`, partagé avec le robot). En foot, le déroulement du match compte aussi : tirs, tirs cadrés et corners (tableau des scores ESPN, toutes les 30 s) corrigent les buts attendus d'ici la fin. Le rythme d'avant-match pèse comme 2 matchs entiers, donc les tirs comptent pour environ 20 % à la mi-temps et un tiers en fin de match ; l'équipe qui a au moins 68 % des occasions est signalée « 📈 domine ». En NHL, ESPN ne donne pas les tirs du match en direct : seuls le score et le temps comptent. Ces chiffres n'entrent pas dans le Bilan.
- Pendant un match, un **Safe du moment** (75 à 90 %) et un **Fun du moment** (35 à 66 %) s'affichent sous ses pronostics (onglet Pronostics), en haut de son analyse (onglet Matchs) et dans les alertes de but.
- **Cohérence** : tous les pronostics d'un match racontent la même histoire. Avant le match, le Fun est choisi parmi les paris qui vont dans le sens de l'analyse : quand il passe, le Safe et le Vainqueur ont au moins autant de chances de passer (calcul exact sur la grille des scores, `market_fn` / `marketTest`). Le score exact est le plus probable parmi ceux où le Safe et le Vainqueur passent, et le buteur vient de l'équipe annoncée gagnante. Pendant le match, l'histoire est celle de l'équipe qui a le plus de chances de gagner à cet instant (à égalité ou presque, le Vainqueur de l'analyse) ; le Fun du moment ne contredit ni elle ni le Safe du moment. S'il n'existe aucun pari cohérent dans la fourchette, rien n'est proposé.
- **État du robot** : adresse du robot suivie de `/etat` (nombres seulement, aucune donnée personnelle).

## Présentation et bankroll

- **Menu** : sur la gauche sur ordinateur, barre en bas sur téléphone. Il donne accès à Accueil, Matchs (Foot / NHL), Pronostics, Favoris, Bankroll et Bilan.
- **Accueil** : bénéfice net total, bankroll actuelle, dépôts, retraits, courbe d'évolution (Auj. / 7j / 30j / 90j / 1a / Tout), puis les pronostics les plus sûrs du jour.
- **Bankroll**, gardée uniquement sur le téléphone (localStorage, sans sauvegarde) :
  - dépôts et retraits ;
  - paris ajoutés avec le bouton **+** à côté d'un pronostic (cote du bookmaker, ou cote juste) ou avec « Nouveau pari » ;
  - résultat automatique pour les pronostics du site, sinon ✓ Gagné / ✗ Perdu / ↺ Remboursé à cocher ;
  - **Mes paris** : tous les paris par jour, filtres Tous / En cours / Gagnés / Perdus / Remboursés, avec mise, bénéfice et ROI du filtre ; pour un pari en cours, le score et la minute en direct ;
  - indicateurs : ROI, taux de réussite, série en cours ;
  - monnaie CHF ou EUR.

## Bilan des pronostics

- **Filtre** : Tous, ⚽ Foot ou 🏒 NHL.
- **Deux cases** : le dernier jour, et le total depuis l'ouverture du site.
- **Jour par jour** : on touche un jour pour voir ses matchs, rangés par sport (Foot, puis NHL), puis par championnat (Ligue 1, Premier League, LaLiga, Serie A, Bundesliga, Ligue des champions). Chaque match affiche son score final, le compte des pronostics réussis et chaque pronostic ✓ ou ✗.
- **Pronostics vérifiés par championnat** : rangés en catégories Foot et NHL ; on touche un championnat pour voir ses pronostics. Le détail couvre les 30 derniers jours, le score toute la période.
- **Fun et score exact** : comptés sur une ligne à part, pour ne pas fausser le taux des pronostics sûrs. Dans les listes, ils sont marqués 🎲.
- **Niveau de confiance** : un tableau compare le taux de réussite annoncé et le taux réel, par nombre d'étoiles.

## Cotes, Safe, Fun et enjeu

- **Cotes du bookmaker** : les cotes d'avant-match relayées par ESPN, en décimal, sont affichées sur chaque match. Elles sont archivées pour rester visibles après le coup d'envoi, car ESPN ne fournit pas de cotes en direct.
- **Cote juste** (« @ ») : 1 ÷ la probabilité calculée, sans la marge du bookmaker. Si un bookmaker propose plus, le pari est mathématiquement intéressant.
- **Safe ✅ / Fun 🎲** :
  - pour chaque match, le pari le plus sûr et un pari plus risqué (35 à 66 %), toujours différents (et différent du vainqueur annoncé) ;
  - le Fun d'avant-match et le score exact sont choisis par le calcul automatique, archivés avant le coup d'envoi et vérifiés après le match ;
  - pendant le match, « Pronostics du moment » : Safe entre 75 et 90 % et Fun, recalculés selon le score, la minute et les cartons rouges ;
  - un pari qui profite à l'équipe à 11 contre 10 passe devant ;
  - le sous-onglet **Pronostics › Fun** les regroupe, en direct et avant le match.
- **Enjeu** dans la lecture du match :
  - foot : choc au sommet, Europe, maintien, adversaires directs, début de saison ; en Ligue des champions, top 8, barrages ou élimination ;
  - NHL : match de division, places en séries.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | Récupère les données, calcule, écrit `docs/data.json`, `docs/live.json` et `docs/history.json` |
| `docs/index.html` | Le site (onglets Foot, NHL, Pronostics, Bilan) |
| `docs/live.js` | Calcul des pronostics en direct (site et robot) |
| `worker/`, `wrangler.jsonc` | Robot d'alertes Cloudflare. Tests : `node worker/test.mjs` |
| `docs/manifest.webmanifest`, `docs/icon.svg` | Installation sur l'écran d'accueil |
| `.github/workflows/update.yml` | Lance `app.py` aux 7 passages de `horaires.py` ; un passage que GitHub lance en retard ou saute est rattrapé au lancement suivant (30 min plus tard au plus) ; en plus, recalcul toutes les 30 min de 75 à 5 min avant chaque coup d'envoi (compositions officielles, blessés de dernière minute, dernières cotes) |
| `tests/test_app.py` | Vérifie les calculs avant chaque mise à jour |

Pour tester sur ton ordinateur : `pip install -r requirements.txt`, puis `python -m unittest discover -s tests`, puis `python app.py`.
