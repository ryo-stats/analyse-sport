#!/usr/bin/env python3
"""Analyse sportive automatique : foot (5 grands championnats, Ligue des champions, Europa League) et NHL.

Sources gratuites, sans clé :
  - API publique d'ESPN : matchs, résultats, classements, effectifs et stats des joueurs, cotes (foot et NHL) ;
  - API officielle de la NHL : calendrier, classement, stats des joueurs, composition du dernier match ;
  - MoneyPuck.com (buts attendus NHL). L'Elo des clubs de foot est calculé ici, à partir des résultats ESPN.

Chaque exécution :
  1. complète les résultats des jours passés dans docs/history.json ;
  2. récupère les matchs du jour et calcule l'analyse maison (Elo, buts de la saison et des 10 derniers matchs,
     xG au hockey) ; les cotes ne servent que de garde-fou invisible (gros désaccord : pas de Safe) ;
  3. calcule buts attendus, scores probables, buteurs et joueurs décisifs ;
  4. calcule le bilan : les probabilités annoncées se vérifient-elles ?
  5. écrit docs/data.json, que le site docs/index.html affiche.

Usage : python app.py
"""
import csv
import datetime as dt
import difflib
import io
import json
import math
import os
import re
import sys
import time
import traceback
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
from scipy.optimize import brentq, fsolve, least_squares
from scipy.stats import poisson

PARIS = ZoneInfo("Europe/Paris")
ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA_FILE = os.path.join(DOCS, "data.json")
HIST_FILE = os.path.join(DOCS, "history.json")
PICKS_FILE = os.path.join(DOCS, "picks.json")
LIVE_FILE = os.path.join(DOCS, "live.json")      # version compacte lue chaque minute par le robot d'alertes

# (nom affiché, code de championnat ESPN)
LEAGUES = [
    ("Ligue 1", "fra.1"),
    ("Premier League", "eng.1"),
    ("LaLiga", "esp.1"),
    ("Serie A", "ita.1"),
    ("Bundesliga", "ger.1"),
    ("Ligue des champions", "uefa.champions"),
    ("Europa League", "uefa.europa"),
]
# coupes d'Europe : phase de ligue à 36, statistiques des joueurs prises dans leur championnat national
EUROPE = {"uefa.champions": "Ligue des champions", "uefa.europa": "Europa League"}
# bandeau de chaque championnat sur le site : pays et logo ESPN
LEAGUE_INFO = {
    "fra.1": ("France", 9), "eng.1": ("Angleterre", 23), "esp.1": ("Espagne", 15),
    "ita.1": ("Italie", 12), "ger.1": ("Allemagne", 10), "uefa.champions": ("Europe", 2), "uefa.europa": ("Europe", 2310),
}
LEAGUE_LOGO = "https://a.espncdn.com/i/leaguelogos/soccer/500/{}.png"
NHL_LOGO = "https://assets.nhle.com/logos/nhl/svg/{}_{}.svg"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer/{league}/standings"
ESPN_LEADERS = "https://site.web.api.espn.com/apis/site/v2/sports/soccer/{league}/statistics"
ESPN_ROSTER = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{team}/roster"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/summary"
LINEUP_BEFORE = dt.timedelta(hours=2)   # les compositions sortent environ 1 h avant le coup d'envoi
BENCH_SHARE = 0.3                       # un remplaçant qui entre joue en moyenne ~25 minutes
ESPN_PLAYER_STATS = ("https://sports.core.api.espn.com/v2/sports/soccer/leagues/{league}/seasons/{season}"
                     "/types/1/athletes/{athlete}/statistics")
SQUAD_SIZE = 6        # joueurs affichés par équipe
# part a priori des buts (et des buts + passes) de l'équipe selon le poste, pour un joueur qui joue tout
GOAL_PRIOR = {"F": 0.22, "M": 0.08, "D": 0.03}
DEC_PRIOR = {"F": 0.35, "M": 0.25, "D": 0.08}
POSTES = {"F": "Attaquant", "M": "Milieu", "D": "Défenseur"}
NHL_API = "https://api-web.nhle.com/v1"
# buts attendus MoneyPuck (NHL) ; Elo maison (foot), calculé à partir des résultats ESPN
MONEYPUCK_TEAMS = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{season}/regular/teams.csv"
ELO_HFA = 65          # avantage du terrain en points Elo (ordre de grandeur des classements Elo publiés)
ELO_DRAW = 0.28       # part de nuls entre deux équipes de même niveau
SPLIT_GAP = 0.12      # le marché des paris juge le Safe moins sûr d'au moins 12 points : avis partagés
NHL_SHRINK = 12       # début de saison : les xG d'une équipe pèsent comme ses matchs, face à 12 matchs « moyens »

NHL_TIE = 0.22        # part des matchs NHL à égalité après 60 minutes
NHL_TOTAL = 6.2       # buts par match NHL quand aucune ligne de buts n'est disponible
DAYS_BACK = 3         # nombre de jours passés relus pour compléter les résultats
POSITIONS = {"C": "Centre", "L": "Ailier", "R": "Ailier", "D": "Défenseur"}
HEADERS = {"User-Agent": "analyse-sport/1.0 (usage personnel)"}

NOTES = []


def note(msg):
    """Message affiché en haut du site (source indisponible, donnée manquante...)."""
    print(msg, file=sys.stderr)
    if msg not in NOTES:
        NOTES.append(msg)


def step(name, fn, fallback=None):
    """Une étape du passage : en cas d'erreur inattendue (format d'une source qui change, bug), le détail part
    dans le journal GitHub, une ligne s'affiche sur le site et le passage continue avec fallback, au lieu de
    ne rien publier du tout."""
    try:
        return fn()
    except Exception as exc:
        traceback.print_exc()
        note(f"{name} : erreur inattendue ({exc.__class__.__name__}), le reste du site est à jour.")
        return fallback


# ---------------------------------------------------------------- accès réseau

def get_json(url, params=None):
    """GET avec 3 tentatives. Renvoie le JSON ou None, sans jamais lever d'exception."""
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as exc:
            print(f"Réseau : {exc.__class__.__name__} sur {url}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code != 404:
            print(f"HTTP {r.status_code} sur {url}", file=sys.stderr)
        return None
    return None


FETCH_LOG = {}        # résultat des lectures de fichiers texte (diagnostic publié dans data.json › second)


def get_text(url, headers=None, tries=3, timeout=30):
    """GET d'un fichier texte (CSV). Renvoie le texte ou None ; le résultat est noté dans FETCH_LOG."""
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            print(f"Réseau : {exc.__class__.__name__} sur {url}", file=sys.stderr)
            FETCH_LOG[url] = exc.__class__.__name__
            time.sleep(3 * (attempt + 1))
            continue
        FETCH_LOG[url] = f"HTTP {r.status_code}, {len(r.text)} car., début : {r.text[:60]!r}"
        if r.status_code == 200:
            return r.text
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(5 * (attempt + 1))
            continue
        print(f"HTTP {r.status_code} sur {url}", file=sys.stderr)
        return None
    return None


def get_many(urls, workers=8):
    """Plusieurs GET en parallèle ; renvoie les réponses dans l'ordre des adresses (None si échec)."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(get_json, urls))


# ---------------------------------------------------------------- outils

STOP_WORDS = {"fc", "cf", "ac", "as", "sc", "afc", "ssc", "club", "de", "sv", "vfb", "vfl", "tsg", "rc",
              "ogc", "stade", "olympique", "calcio", "bc", "ud", "rcd", "sd", "cd", "ca", "us", "hsc",
              "fk", "1899", "1909", "1913", "1846", "1907", "04", "05"}


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    for ch in "-.'&/()":
        s = s.replace(ch, " ")
    words = [w for w in s.split() if w not in STOP_WORDS]
    return " ".join(words) if words else s.strip()


def similarity(a, b):
    """Ressemblance entre deux noms d'équipe, de 0 à 1."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    if short in long_ and len(short) / len(long_) >= 0.5:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def parse_time(s):
    s = s.replace("Z", "+00:00")
    if len(s) == 22 and s[16] == "+":          # format ESPN "2026-10-11T15:30+00:00"
        s = s[:16] + ":00" + s[16:]
    return dt.datetime.fromisoformat(s)


def paris(t):
    return t.astimezone(PARIS)


def implied(american):
    """Cote américaine (+150, -200, EVEN) → probabilité implicite brute."""
    if american in (None, ""):
        return None
    s = str(american).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        o = 100
    else:
        try:
            o = float(s.replace("+", ""))
        except ValueError:
            return None
    if o == 0:
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def pct(x):
    return round(x * 100)


def plural(n, word, word_pl=None):
    return f"{n} {word if n <= 1 else (word_pl or word + 's')}"


# ---------------------------------------------------------------- modèles

def score_grid(lh, la, n=11):
    ph = [poisson.pmf(i, lh) for i in range(n)]
    pa = [poisson.pmf(j, la) for j in range(n)]
    return [[ph[i] * pa[j] for j in range(n)] for i in range(n)]


def outcome_probs(lh, la, n=11):
    """Victoire domicile, égalité, victoire extérieur selon deux lois de Poisson."""
    g = score_grid(lh, la, n)
    home = sum(g[i][j] for i in range(n) for j in range(i))
    away = sum(g[i][j] for i in range(n) for j in range(i + 1, n))
    return home, 1 - home - away, away


def p_over(total, line):
    """Chance que le total de buts (loi de Poisson de moyenne total) dépasse la ligne du bookmaker.
    Ligne entière (3,0) : remboursé si pile la ligne. Quart de ligne (2,75) : moitié sur chaque ligne voisine."""
    frac = round((line % 1) * 4) / 4
    if frac in (0.25, 0.75):
        return (p_over(total, line - 0.25) + p_over(total, line + 0.25)) / 2
    if frac == 0.5:
        return 1 - poisson.cdf(int(line), total)
    k = int(round(line))
    eq = poisson.pmf(k, total)
    return (1 - poisson.cdf(k, total)) / max(1e-9, 1 - eq)


def foot_lambdas(p_home, p_away, over=None):
    """Buts attendus de chaque équipe qui reproduisent les probabilités des cotes.
    over = {"line": 2.5, "p": 0.58} : ligne de buts du bookmaker (« plus de 2,5 buts », marge retirée).
    Sans elle, le total de buts se déduit du seul 1N2, ce qui confond un match fermé et un match ouvert."""
    if over and over.get("line") and over.get("p"):
        base = foot_lambdas(p_home, p_away)
        def g(v):
            lh, la = float(v[0]), float(v[1])
            h, _, a = outcome_probs(lh, la)
            return [h - p_home, a - p_away, p_over(lh + la, over["line"]) - over["p"]]
        try:
            r = least_squares(g, base, bounds=([0.05, 0.05], [6, 6]))
            if r.success:
                return float(r.x[0]), float(r.x[1])
        except ValueError:
            pass
        return base
    def f(v):
        h, _, a = outcome_probs(abs(v[0]), abs(v[1]))
        return [h - p_home, a - p_away]
    # Avec un gros favori, deux couples de buts attendus collent aux cotes (ex. 1,1-3,7 et 2,2-5,8) :
    # on essaie plusieurs points de départ et on garde la solution exacte au total de buts le plus bas.
    found = []
    for start in ([1.4, 1.1], [0.8, 0.8], [0.6, 2.5], [2.5, 0.6]):
        v, info, ok, _ = fsolve(f, start, full_output=True)
        lh, la = abs(v[0]), abs(v[1])
        if ok == 1 and max(abs(x) for x in f([lh, la])) < 1e-4:
            found.append((lh + la, lh, la))
    if found:
        _, lh, la = min(found)
        return lh, la
    v = fsolve(f, [1.4, 1.1])
    return abs(v[0]), abs(v[1])


# ---------------------------------------------------------------- analyse maison (sans les cotes)
# Foot : attaque et défense de chaque équipe tirées de ses buts (saison + 10 derniers matchs, comptés deux fois),
# ramenées vers la moyenne du championnat tant que peu de matchs sont joués, plus l'avantage du terrain.
# Coupes d'Europe : chaque équipe est notée dans son championnat, corrigé par le niveau de ce championnat.
SRC_FOOT = "Analyse maison : buts marqués et encaissés (saison et 10 derniers matchs), avantage du terrain"
SRC_FOOT_ELO = "Analyse maison : classement maison (tous les résultats depuis deux saisons) et buts marqués et encaissés (saison et 10 derniers matchs), avantage du terrain"
SRC_FOOT_ELO_ONLY = "Analyse maison : classement maison (tous les résultats depuis deux saisons), avantage du terrain"
ELO_WEIGHT = 0.5         # part du classement Elo dans le verdict 1N2 (l'autre moitié : les buts)
SRC_NHL = "Analyse maison : buts attendus (xG) de chaque équipe, données MoneyPuck.com, fatigue des matchs rapprochés"
B2B_FACTOR = 0.95        # NHL : une équipe qui a joué la veille marque environ 5 % de moins
HOME_ADV = 1.12
FORM_K = 6
LEAGUE_STRENGTH = {"eng.1": 1.00, "esp.1": 0.97, "ita.1": 0.95, "ger.1": 0.94, "fra.1": 0.89}   # ordre de grandeur (UEFA)
OTHER_STRENGTH = 0.80
EURO_GPG = 1.45          # buts par équipe et par match en coupe d'Europe (ordre de grandeur)
TABLES = {}


ELO_FILE = os.path.join(ROOT, "cache", "elo.json")   # résultats des saisons terminées (ils ne changent plus)
ELO_CACHE, ELO_RATINGS, ELO_STATS, ELO_DIAG = {}, {}, {}, {}
ELO_SEASONS = 3          # la saison en cours et les deux précédentes (la plus ancienne sert de rodage)
ELO_K = (30, 20)         # sensibilité : rodage, puis régime normal
ELO_NEW = -80            # club qui arrive dans le championnat (promu) : 80 points sous la moyenne
ELO_REGRESS = 0.2        # entre deux saisons, 20 % de retour vers la moyenne (mercato, entraîneurs)
ELO_REFRESH = 6 * 3600   # saison en cours relue au plus toutes les 6 h (secondes)
ELO_MIN = 150            # matchs lus au minimum pour se fier au classement
LEAGUE_ELO = {"eng.1": 0, "esp.1": -15, "ita.1": -25, "ger.1": -30, "fra.1": -55}   # niveau des championnats
OTHER_ELO = -110


def season_of(day):
    """Saison de foot européenne (année de début) : de juillet à juin."""
    return day.year if day.month >= 7 else day.year - 1


def result_rows(events):
    """Matchs terminés d'une liste d'événements ESPN (tableau des scores ou calendrier d'équipe), sans doublon."""
    rows, seen = [], set()
    for e in events:
        if not e.get("id") or e["id"] in seen:
            continue
        st = ((e.get("status") or (e.get("competitions") or [{}])[0].get("status") or {}).get("type") or {})
        if not (st.get("completed") or st.get("state") == "post"):
            continue
        try:
            _, h, a = competitors(e)
            rows.append([e["date"][:10], str(h["team"]["id"]), str(a["team"]["id"]), score_value(h), score_value(a)])
            seen.add(e["id"])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return sorted(rows)


def league_results(code, season, current):
    """Résultats d'une saison d'un championnat (ESPN) : [[date, id domicile, id extérieur, buts, buts], …], lus
    dans le calendrier de chaque club du classement (le tableau des scores ESPN sur une saison ou un mois revient
    vide, essai du 27/10). Les saisons terminées sont gardées dans cache/elo.json ; la saison en cours est relue
    au plus toutes les 6 heures (20 appels par championnat : pas à chaque passage, pour ménager ESPN).
    ELO_DIAG garde le nombre de matchs lus (data.json › second.foot.elo_fetch)."""
    store = ELO_CACHE.setdefault(code, {})
    if not current and str(season) in store:
        return store[str(season)]
    cur = store.get("cur") or {}
    if current and cur.get("s") == season and CLOCK[0].timestamp() - cur.get("t", 0) < ELO_REFRESH:
        return cur["rows"]
    events = []
    for doc in get_many([ESPN_TEAM_SCHEDULE.format(league=code, team=t) + f"?season={season}" for t in table_of(code)[0]]):
        events += (doc or {}).get("events") or []
    rows = result_rows(events)
    ELO_DIAG.setdefault(code, {})[str(season)] = len(rows)
    if rows and not current:
        store[str(season)] = rows
    if rows and current:
        store["cur"] = dict(s=season, t=CLOCK[0].timestamp(), rows=rows)
    return rows


def house_elo(code, today):
    """Classement Elo maison d'un championnat : {id ESPN: (points, moyenne des clubs de la saison)}, ou {} si
    trop peu de matchs. Chaque match déplace des points du perdant au gagnant, d'autant plus que le résultat
    surprend (avantage du terrain compris) et que l'écart de buts est large."""
    if not code:
        return {}
    if code in ELO_RATINGS:
        return ELO_RATINGS[code]
    s0, r, n, started = season_of(today), {}, 0, False
    for season in range(s0 - ELO_SEASONS + 1, s0 + 1):
        rows = league_results(code, season, season == s0)
        if not rows:
            continue
        if started:
            r = {t: 1500 + (v - 1500) * (1 - ELO_REGRESS) for t, v in r.items()}
        k = ELO_K[1] if started else ELO_K[0]
        for _, h, a, gh, ga in rows:
            rh = r.setdefault(h, 1500 + (ELO_NEW if started else 0))
            ra = r.setdefault(a, 1500 + (ELO_NEW if started else 0))
            e = 1 / (1 + 10 ** (-(rh + ELO_HFA - ra) / 400))
            gd = abs(gh - ga)
            mult = 1 if gd <= 1 else 1.5 if gd == 2 else (11 + gd) / 8
            delta = k * mult * ((1 if gh > ga else 0.5 if gh == ga else 0) - e)
            r[h], r[a] = rh + delta, ra - delta
            n += 1
        started = True
        last = {x for row in rows for x in row[1:3]}
    ELO_STATS[code] = n
    if n < ELO_MIN:
        ELO_RATINGS[code] = {}
        return {}
    mean = sum(r[t] for t in last) / len(last)
    ELO_RATINGS[code] = {t: (v, mean) for t, v in r.items()}
    return ELO_RATINGS[code]


def elo_probs(code_h, code_a, hid, aid, today):
    """Victoire, nul, défaite selon l'Elo maison, et les deux classements ; None faute de données.
    Coupes d'Europe : chaque club est situé dans son championnat, corrigé du niveau de ce championnat."""
    th, ta = house_elo(code_h, today), house_elo(code_a, today)
    if hid not in th or aid not in ta:
        return None
    (rh, mh), (ra, ma) = th[hid], ta[aid]
    if code_h != code_a:
        rh, ra = rh - mh + 1500 + LEAGUE_ELO.get(code_h, OTHER_ELO), ra - ma + 1500 + LEAGUE_ELO.get(code_a, OTHER_ELO)
    return dict(p=elo_wdl(rh - ra + ELO_HFA), h=round(rh), a=round(ra))


def load_elo():
    ELO_CACHE.clear()
    ELO_CACHE.update(load_json(ELO_FILE, {}))
    ELO_RATINGS.clear()
    ELO_STATS.clear()
    ELO_DIAG.clear()


def save_elo(today):
    keep = {str(y) for y in range(season_of(today) - ELO_SEASONS + 1, season_of(today))}
    save_json(ELO_FILE, {c: {y: v for y, v in d.items() if y in keep or y == "cur"} for c, d in ELO_CACHE.items()})


def table_of(code):
    """Classement d'un championnat (lu une fois par passage) et sa moyenne de buts par équipe et par match."""
    if code not in TABLES:
        t = espn_standings(code) if code else {}
        gp = sum(r["gp"] for r in t.values())
        TABLES[code] = (t, (sum(r["gf"] for r in t.values()) / gp) if gp else 1.4)
    return TABLES[code]


def team_strength(st, rec, mu):
    """Attaque et défense d'une équipe, 1 = moyenne de son championnat (plus haut = plus fort en attaque,
    plus haut = plus faible en défense)."""
    gp, gf, ga = (st or {}).get("gp", 0), (st or {}).get("gf", 0), (st or {}).get("ga", 0)
    n, f10, a10 = (rec or {}).get("n10", 0), (rec or {}).get("gf10", 0), (rec or {}).get("ga10", 0)
    if not gp and not n:
        return None
    return ((gf + f10 + mu * FORM_K) / (gp + n + FORM_K) / mu,
            (ga + a10 + mu * FORM_K) / (gp + n + FORM_K) / mu)


def foot_model(st_h, st_a, rec_h, rec_a, mu_h, mu_a=None, code_h=None, code_a=None):
    """Buts attendus (domicile, extérieur) de l'analyse maison, ou None faute de données.
    code_h / code_a : championnats nationaux des deux équipes (coupes d'Europe), sinon le même championnat."""
    th, ta = team_strength(st_h, rec_h, mu_h), team_strength(st_a, rec_a, mu_a or mu_h)
    if not th or not ta:
        return None
    if code_h or code_a:
        s_h, s_a = LEAGUE_STRENGTH.get(code_h, OTHER_STRENGTH), LEAGUE_STRENGTH.get(code_a, OTHER_STRENGTH)
        (ah, dh), (aa, da), mu = (th[0] * s_h, th[1] / s_h), (ta[0] * s_a, ta[1] / s_a), EURO_GPG
    else:
        (ah, dh), (aa, da), mu = th, ta, mu_h
    clamp = lambda x: min(4.5, max(0.2, x))
    return clamp(mu * ah * da * HOME_ADV), clamp(mu * aa * dh / HOME_ADV)


def table_lambdas(sh, sa, league_gpg):
    """Sans cotes : attaque et défense tirées du classement, ramenées vers la moyenne (petits échantillons)."""
    def rate(goals, games):
        return (goals + league_gpg * 6) / (games + 6)
    att_h, def_h = rate(sh["gf"], sh["gp"]) / league_gpg, rate(sh["ga"], sh["gp"]) / league_gpg
    att_a, def_a = rate(sa["gf"], sa["gp"]) / league_gpg, rate(sa["ga"], sa["gp"]) / league_gpg
    return league_gpg * att_h * def_a * 1.12, league_gpg * att_a * def_h * 0.88


def top_scores(lh, la, k=3, n=9, exclude_draws=False):
    g = score_grid(lh, la, n)
    cells = sorted(((g[i][j], f"{i}-{j}") for i in range(n) for j in range(n)
                    if not (exclude_draws and i == j)), reverse=True)[:k]
    return [c[1] for c in cells], [pct(c[0]) for c in cells]


def nhl_split(win_home, total):
    """Vainqueur final → (domicile 60 min, égalité 60 min, extérieur 60 min, buts attendus dom., ext.)."""
    lean = 0.5 + 0.5 * (win_home - 0.5)          # la prolongation est proche du pile ou face
    p_h = max(0.02, win_home - NHL_TIE * lean)
    p_a = max(0.02, (1 - win_home) - NHL_TIE * (1 - lean))
    p_d = max(0.0, 1 - p_h - p_a)
    target = p_h / p_a

    def f(s):
        h, _, a = outcome_probs(total * s, total * (1 - s), n=15)
        return h / a - target
    try:
        s = brentq(f, 0.12, 0.88)
    except ValueError:
        s = 0.5
    return p_h, p_d, p_a, total * s, total * (1 - s)


# ---------------------------------------------------------------- ESPN

def espn_events(sport, league, day):
    data = get_json(ESPN_SCOREBOARD.format(sport=sport, league=league), {"dates": day.strftime("%Y%m%d")})
    if data is None:
        return None
    return data.get("events", []) or []


def espn_odds(comp, three_way):
    """Cotes ESPN → probabilités (marge retirée), bookmaker et ligne de buts."""
    for o in comp.get("odds") or []:
        if not o:
            continue
        ml = o.get("moneyline") or {}

        def line(side):
            d = ml.get(side) or {}
            for k in ("close", "current", "open"):
                v = (d.get(k) or {}).get("odds")
                if v not in (None, ""):
                    return v
            return None
        home = line("home") or (o.get("homeTeamOdds") or {}).get("moneyLine")
        away = line("away") or (o.get("awayTeamOdds") or {}).get("moneyLine")
        values = [home, away]
        if three_way:
            draw = line("draw") or (o.get("drawOdds") or {}).get("moneyLine")
            values = [home, draw, away]
        probs = [implied(v) for v in values]
        if any(p is None or p <= 0 for p in probs):
            continue
        s = sum(probs)
        return dict(p=[p / s for p in probs],
                    dec=[round(1 / p, 2) for p in probs],       # cotes décimales du bookmaker (marge comprise)
                    provider=((o.get("provider") or {}).get("name") or "un bookmaker"),
                    total=o.get("overUnder"), over=total_odds(o))
    return None


def total_odds(o):
    """Ligne « plus / moins de X buts » et chance du « plus » (marge retirée), seulement si le bookmaker donne
    les cotes des deux côtés : la ligne seule ne dit rien (en foot, elle vaut presque toujours 2,5)."""
    line, t = o.get("overUnder"), o.get("total") or {}

    def side(name):
        d = t.get(name) or {}
        for k in ("close", "current", "open"):
            x = d.get(k) or {}
            if x.get("odds") not in (None, ""):
                return x.get("odds"), x.get("line")
        return None, None
    ov, ol = side("over")
    un, _ = side("under")
    if ov is None or un is None:
        ov, un = o.get("overOdds"), o.get("underOdds")
    if ol and line in (None, ""):
        try:
            line = float(str(ol).lower().lstrip("ou"))
        except ValueError:
            line = None
    po, pu = implied(ov), implied(un)
    try:
        line = float(line)
    except (TypeError, ValueError):
        return None
    if not po or not pu or not 0.5 <= line <= 9.5:
        return None
    return dict(line=line, p=round(po / (po + pu), 4))


def espn_standings(league):
    data = get_json(ESPN_STANDINGS.format(league=league)) or {}
    rows = {}
    groups = data.get("children") or [data]
    for grp in groups:
        for e in (grp.get("standings") or {}).get("entries", []):
            st = {s.get("name"): s.get("value") for s in e.get("stats", [])}
            rows[str(e["team"]["id"])] = dict(
                rank=int(st.get("rank") or 0), gp=int(st.get("gamesPlayed") or 0),
                w=int(st.get("wins") or 0), d=int(st.get("ties") or 0), l=int(st.get("losses") or 0),
                gf=int(st.get("pointsFor") or 0), ga=int(st.get("pointsAgainst") or 0),
                pts=int(st.get("points") or 0))
    return rows


def foot_stakes(league, home, away, sh, sa, table):
    """Enjeu du match d'après le classement : sommet, Europe, maintien, ou début de saison (texte court)."""
    if not sh or not sa:
        return ""
    n = len(table) or 20
    gp = min(sh["gp"], sa["gp"])
    def nth(r):
        return "1er" if r == 1 else f"{r}e"
    if league in EUROPE.values():
        # phase de ligue à 36 (Ligue des champions et Europa League) : 1-8 en 8es, 9-24 barrages, 25-36 éliminés
        def zone(r):
            return "top 8, qualification directe pour les 8es" if r <= 8 else "place de barrage" if r <= 24 else "zone d'élimination"
        out = f"{home} ({nth(sh['rank'])}) : {zone(sh['rank'])}. {away} ({nth(sa['rank'])}) : {zone(sa['rank'])}."
        if gp <= 2:
            out += " Classement encore très provisoire."
        return out
    pts_at = {r["rank"]: r["pts"] for r in table.values()}
    safe_line = pts_at.get(n - 3)                    # dernière place hors zone rouge (3 derniers)
    parts = []
    if sh["rank"] <= 4 and sa["rank"] <= 4:
        parts.append(f"Choc au sommet : {nth(sh['rank'])} contre {nth(sa['rank'])}.")
    else:
        for name, st in ((home, sh), (away, sa)):
            r = st["rank"]
            if r == 1:
                parts.append(f"{name} est leader ({plural(st['pts'], 'point')}).")
            elif r <= 4:
                parts.append(f"{name} ({nth(r)}) défend une place qualificative pour la Ligue des champions.")
            elif r <= 7:
                parts.append(f"{name} ({nth(r)}) est dans la course à l'Europe.")
            elif r > n - 3:
                parts.append(f"{name} ({nth(r)}) est en zone de relégation : match vital.")
            elif r > n - 6 and safe_line is not None:
                gap = st["pts"] - pts_at.get(n - 2, safe_line)
                parts.append(f"{name} ({nth(r)}) lutte pour le maintien"
                             + (f", {plural(gap, 'point')} d'avance sur la zone rouge." if gap >= 0 else "."))
        if not parts:
            parts.append("Match de milieu de tableau : enjeu modéré au classement.")
    d = abs(sh["pts"] - sa["pts"])
    if d <= 3 and abs(sh["rank"] - sa["rank"]) <= 4:
        parts.append("Adversaires directs au classement : " + ("même nombre de points." if d == 0 else f"{plural(d, 'point')} d'écart."))
    if gp < 6:
        parts.append(f"Début de saison ({plural(gp, 'match', 'matchs')} joués) : le classement compte encore peu.")
    return " ".join(parts)


def nhl_stakes(home, away, sh, sa):
    """Enjeu NHL : rivalité de division (match « à 4 points ») et course aux séries."""
    if not sh or not sa:
        return ""
    parts = []
    if sh.get("divisionName") and sh.get("divisionName") == sa.get("divisionName"):
        parts.append(f"Match de division ({sh['divisionName']}) : battre un rival direct compte double au classement.")
    gp = min(sh.get("gamesPlayed") or 0, sa.get("gamesPlayed") or 0)
    if gp < 20:
        parts.append(f"Début de saison ({plural(gp, 'match', 'matchs')} joués) : le classement bouge encore beaucoup.")
    else:
        for name, st in ((home, sh), (away, sa)):
            div, wc = st.get("divisionSequence") or 99, st.get("wildcardSequence") or 99
            parts.append(f"{name} est " + ("qualifié pour les séries s'ils commençaient ce soir." if div <= 3 or wc <= 2
                                            else "hors des places en séries pour l'instant."))
    return " ".join(parts)


def espn_leaders(league):
    """Meilleurs buteurs et passeurs du championnat, regroupés par équipe (identifiant ESPN)."""
    data = get_json(ESPN_LEADERS.format(league=league)) or {}
    players = {}
    for cat in data.get("stats", []):
        name = (cat.get("name") or "").lower()
        kind = "goals" if "goal" in name else ("assists" if "assist" in name else None)
        if not kind:
            continue
        for item in cat.get("leaders", []):
            a = item.get("athlete") or {}
            pid, team = a.get("id"), (a.get("team") or {}).get("id")
            if not pid or not team:
                continue
            p = players.setdefault(pid, dict(name=a.get("displayName", ""), team=str(team), goals=0, assists=0, apps=0))
            p[kind] = max(p[kind], int(item.get("value") or 0))
            for s in a.get("statistics") or []:
                sname, val = s.get("name"), int(s.get("value") or 0)
                if sname == "appearances":
                    p["apps"] = max(p["apps"], val)
                elif sname in ("totalGoals", "goals"):
                    p["goals"] = max(p["goals"], val)
                elif sname in ("goalAssists", "assists"):
                    p["assists"] = max(p["assists"], val)
    by_team = {}
    for p in players.values():
        by_team.setdefault(p["team"], []).append(p)
    return by_team


SQUADS = {}
CACHE_FILE = os.path.join(ROOT, "cache", "squads.json")

# ---------------------------------------------------------------- forme récente des joueurs (10 derniers matchs)
# Feuilles de match gardées d'un passage à l'autre (un match terminé ne change plus) : {"foot": {id: {...}}, "nhl": {...}}
CLOCK = [dt.datetime.now(dt.timezone.utc)]   # heure du passage (fixée par main, pour des calculs reproductibles)
FORM_FILE = os.path.join(ROOT, "cache", "form.json")
FORM = {}
FORM_GAMES = 10
FORM_STATS = {"foot": 0, "nhl": 0}
ESPN_TEAM_SCHEDULE = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{team}/schedule"


def load_form():
    FORM.clear()
    FORM.update(load_json(FORM_FILE, {}))
    FORM.setdefault("foot", {})
    FORM.setdefault("nhl", {})
    FORM_STATS.update(foot=0, nhl=0)


def save_form(today):
    cut = (today - dt.timedelta(days=150)).isoformat()
    for sp in ("foot", "nhl"):
        FORM[sp] = {k: v for k, v in FORM.get(sp, {}).items() if v.get("date", "") >= cut}
    save_json(FORM_FILE, FORM)


def summary_players(data):
    """Feuille d'un match de foot ESPN → {id équipe: {id joueur: [buts, passes]}} (joueurs entrés en jeu)."""
    out = {}
    for r in (data or {}).get("rosters") or []:
        tid = str((r.get("team") or {}).get("id") or "")
        players = {}
        for x in r.get("roster") or []:
            pid = str((x.get("athlete") or {}).get("id") or "")
            st = {s.get("name"): s.get("value") for s in (x.get("stats") or []) if isinstance(s, dict)}
            if not pid or not (x.get("starter") or x.get("subbedIn") or st.get("appearances")):
                continue
            players[pid] = [int(st.get("totalGoals") or 0), int(st.get("goalAssists") or 0)]
        if tid and players:
            out[tid] = players
    return out


def recent_of(sport, records, team):
    """Additionne les feuilles de match d'une équipe : {joueur: [matchs joués, buts, passes]}, nombre de matchs, buts."""
    per, games, goals = {}, 0, 0
    for rec in records:
        t = (rec or {}).get("teams", {}).get(str(team))
        if not t:
            continue
        games += 1
        for pid, (g, a) in t.items():
            x = per.setdefault(str(pid), [0, 0, 0])
            x[0] += 1
            x[1] += g
            x[2] += a
            goals += g
    return dict(players=per, games=games, goals=goals) if games >= 3 else None


def foot_recent(league, team_id):
    """Les 10 derniers matchs de championnat d'une équipe de foot (feuilles de match ESPN), ou None."""
    sched = get_json(ESPN_TEAM_SCHEDULE.format(league=league, team=team_id))
    done = []
    for e in (sched or {}).get("events") or []:
        st = (((e.get("competitions") or [{}])[0].get("status") or {}).get("type") or {})
        if e.get("id") and (st.get("completed") or st.get("state") == "post"):
            done.append(e)
    done = sorted(done, key=lambda e: e.get("date", ""), reverse=True)[:FORM_GAMES]
    need = [e for e in done if str(e["id"]) not in FORM["foot"]]
    docs = get_many([ESPN_SUMMARY.format(league=league) + f"?event={e['id']}" for e in need]) if need else []
    for e, doc in zip(need, docs):
        teams = summary_players(doc)
        if teams:
            FORM["foot"][str(e["id"])] = dict(date=str(e.get("date", ""))[:10], teams=teams)
    out = recent_of("foot", [FORM["foot"].get(str(e["id"])) for e in done], team_id)
    if out:
        FORM_STATS["foot"] += 1
    # forme de l'équipe : buts marqués et encaissés sur ces 10 matchs (tableau des scores, sans feuille de match)
    gf = ga = n = 0
    for e in done:
        cs = (e.get("competitions") or [{}])[0].get("competitors") or []
        me = next((c for c in cs if str((c.get("team") or {}).get("id") or c.get("id")) == str(team_id)), None)
        other = next((c for c in cs if c is not me), None)
        try:
            a, b = score_value(me), score_value(other)
        except (TypeError, ValueError):
            continue
        gf, ga, n = gf + a, ga + b, n + 1
    team = dict(n10=n, gf10=gf, ga10=ga) if n >= 3 else {}
    if out or team:
        return {**(out or dict(players={}, games=0, goals=0)), **team}
    return None


def score_value(c):
    """Score d'une équipe dans un match ESPN (« 2 » ou {"value": 2.0})."""
    v = (c or {}).get("score")
    if isinstance(v, dict):
        v = v.get("value", v.get("displayValue"))
    return int(float(v))


def nhl_recent(abbr):
    """Les 10 derniers matchs NHL d'une équipe (feuilles de match officielles), ou None."""
    sched = nhl_get(f"/club-schedule-season/{abbr}/now") or {}
    done = sorted([g for g in sched.get("games", []) if g.get("gameState") in ("OFF", "FINAL")],
                  key=lambda g: g.get("startTimeUTC", ""), reverse=True)[:FORM_GAMES]
    need = [g for g in done if str(g["id"]) not in FORM["nhl"]]
    boxes = get_many([f"{NHL_API}/gamecenter/{g['id']}/boxscore" for g in need]) if need else []
    for g, box in zip(need, boxes):
        teams = {}
        for side in ("homeTeam", "awayTeam"):
            ab = ((box or {}).get(side) or {}).get("abbrev")
            st = (((box or {}).get("playerByGameStats") or {}).get(side) or {})
            pl = {str(x["playerId"]): [int(x.get("goals") or 0), int(x.get("assists") or 0)]
                  for grp in ("forwards", "defense") for x in st.get(grp, []) if x.get("playerId")}
            if ab and pl:
                teams[ab] = pl
        if teams:
            FORM["nhl"][str(g["id"])] = dict(date=str(g.get("gameDate") or g.get("startTimeUTC", ""))[:10], teams=teams)
    out = recent_of("nhl", [FORM["nhl"].get(str(g["id"])) for g in done], abbr)
    if out:
        FORM_STATS["nhl"] += 1
    return out


def load_squad_cache(today):
    """Effectifs déjà téléchargés aujourd'hui : évite ~1 000 requêtes ESPN à chaque passage."""
    cache = load_json(CACHE_FILE, {})
    if cache.get("date") == today.isoformat():
        for key, squad in (cache.get("squads") or {}).items():
            league, team = key.split("|", 1)
            SQUADS[(league, team)] = squad


def save_squad_cache(today):
    save_json(CACHE_FILE, dict(date=today.isoformat(),
                               squads={f"{lg}|{tm}": sq for (lg, tm), sq in SQUADS.items() if sq}))


def stat_values(doc):
    out = {}
    for cat in ((doc or {}).get("splits") or {}).get("categories", []):
        for s in cat.get("stats", []):
            if s.get("name") not in out:
                out[s.get("name")] = s.get("value") or 0
    return out


def espn_squad(league, team_id):
    """Effectif complet d'une équipe avec, pour chaque joueur de champ, matchs, minutes, buts et passes.

    Pour les coupes d'Europe, les stats viennent du championnat national du club (plus de matchs,
    donc plus fiables) : on le déduit du championnat principal des joueurs de l'effectif.
    Renvoie None si ESPN ne répond pas.
    """
    if (league, team_id) in SQUADS:
        return SQUADS[(league, team_id)]
    roster = get_json(ESPN_ROSTER.format(league=league, team=team_id))
    athletes = (roster or {}).get("athletes") or []
    if not athletes:
        SQUADS[(league, team_id)] = None
        return None
    season = ((roster.get("season") or {}).get("year")) or dt.date.today().year
    domestic = league
    if league in EUROPE:
        refs = [((a.get("defaultLeague") or {}).get("$ref") or "").split("/leagues/")[-1].split("?")[0].split("/")[0]
                for a in athletes]
        counts = Counter(r for r in refs if r and r not in EUROPE)
        if counts:
            top = [lg for lg, _ in counts.most_common() if lg.endswith(".1")]
            domestic = top[0] if top else counts.most_common(1)[0][0]
    field = [a for a in athletes if ((a.get("position") or {}).get("abbreviation") or "") in GOAL_PRIOR]
    docs = get_many([ESPN_PLAYER_STATS.format(league=domestic, season=season, athlete=a["id"]) for a in field])
    if domestic != league and not any(docs):
        domestic = league
        docs = get_many([ESPN_PLAYER_STATS.format(league=league, season=season, athlete=a["id"]) for a in field])
    players = []
    for a, doc in zip(field, docs):
        s = stat_values(doc)
        apps = int(s.get("appearances") or 0)
        if not apps:
            continue
        players.append(dict(name=a.get("displayName", ""), pos=a["position"]["abbreviation"], apps=apps,
                            minutes=float(s.get("minutes") or 0), goals=int(s.get("totalGoals") or 0),
                            assists=int(s.get("goalAssists") or 0), pid=str(a.get("id") or "")))
    squad = dict(players=players, league=domestic, season=season) if players else None
    SQUADS[(league, team_id)] = squad
    return squad


def espn_lineup(league, event_id):
    """Composition officielle (titulaires, remplaçants) de chaque équipe, ou None si pas encore publiée."""
    data = get_json(ESPN_SUMMARY.format(league=league), {"event": event_id})
    out = {}
    for r in (data or {}).get("rosters") or []:
        side = {"home": "h", "away": "a"}.get(r.get("homeAway"))
        players = r.get("roster") or []
        start = {norm((x.get("athlete") or {}).get("displayName", "")) for x in players if x.get("starter")}
        bench = {norm((x.get("athlete") or {}).get("displayName", "")) for x in players if not x.get("starter")}
        if side and len(start) >= 11:
            out[side] = (start - {""}, bench - {""})
    return out or None


def in_group(name, group):
    """Le joueur figure-t-il dans une liste de noms de la feuille de match (nom complet, ou même nom de famille) ?"""
    n = norm(name)
    last = n.split()[-1] if n else ""
    return n in group or (len(last) > 3 and any(g.split()[-1] == last for g in group if g))


ABSENT_WEIGHT = 0.5   # un buteur absent est en partie remplacé : l'équipe perd la moitié de sa part des buts
ABSENT_CAP = 0.25     # au plus −25 % d'attaque, même si plusieurs titulaires manquent


def lineup_loss(squad, lineup, recent=None):
    """Part des buts de la saison que l'équipe perd d'après la compo officielle : joueurs absents (toute leur part)
    et remplaçants (la part du temps qu'ils ne jouent pas). Renvoie (part perdue, noms des buteurs concernés).
    recent : feuilles des derniers matchs. Un joueur qui n'y jouait déjà presque plus (blessé depuis des semaines)
    ne compte pas : les buts récents de l'équipe tiennent déjà compte de son absence."""
    if not squad or not lineup:
        return 0.0, []
    rec = (recent or {}).get("players") or {}
    # même garde-fou que squad_rows : les feuilles de match ne servent que si l'effectif s'y retrouve
    usable = rec and (recent or {}).get("games", 0) >= 3 and sum(1 for p in squad.get("players") or [] if str(p.get("pid")) in rec) >= 3
    regular = (lambda p: (rec.get(str(p.get("pid"))) or [0])[0] * 2 >= recent["games"]) if usable else (lambda p: True)
    start, bench = lineup
    players = squad.get("players") or []
    goals = sum(p["goals"] for p in players)
    if goals < 5:                      # début de saison : trop peu de buts pour juger qui compte
        return 0.0, []
    lost, who = 0.0, []
    for p in players:
        if not p["goals"] or in_group(p["name"], start) or not regular(p):
            continue
        part = p["goals"] * ((1 - BENCH_SHARE) if in_group(p["name"], bench) else 1)
        lost += part
        if p["goals"] / goals >= 0.1:
            who.append(p["name"])
    return lost / goals, who


def apply_lineup(rows, lineup):
    """Titulaire : inchangé. Remplaçant : chances réduites (il joue peu). Hors du groupe : 0 %.
    Renvoie les noms des joueurs importants (≥ 15 % de marquer) qui ne commencent pas le match."""
    if not lineup:
        return []
    start, bench = lineup
    missing = []
    for r in rows:
        if in_group(r["name"], start):
            r["lineup"] = "start"
            continue
        r["lineup"] = "bench" if in_group(r["name"], bench) else "out"
        if r.get("p_goal", 0) >= 15:
            missing.append(f"{r['name']} ({'remplaçant' if r['lineup'] == 'bench' else 'absent'})")
        for k in ("p_goal", "p_dec"):
            if k in r:
                r[k] = pct(1 - (1 - r[k] / 100) ** BENCH_SHARE) if r["lineup"] == "bench" else 0
    return missing


def squad_rows(squad, lam, side, team, lineup=None, missing=None, recent=None):
    """Probabilité de marquer et d'être décisif (but ou passe) pour chaque joueur de champ.
    recent : 10 derniers matchs de l'équipe (forme). Ils comptent deux fois (saison + forme), et un joueur absent
    de ces matchs voit ses chances réduites comme un remplaçant (blessé, suspendu ou écarté)."""
    players = squad["players"]
    team_games = max(p["apps"] for p in players)
    team_goals = max(sum(p["goals"] for p in players), 1)
    k = 8          # lissage : l'équivalent d'environ 6 matchs de buts « moyens » réparti selon le poste
    rec = (recent or {}).get("players") or {}
    # garde-fou : la forme ne sert que si les joueurs de l'effectif se retrouvent bien dans les feuilles de match
    if recent and (not recent.get("games") or sum(1 for p in players if str(p.get("pid")) in rec) < 5):
        recent, rec = None, {}
    r_games, r_goals = (recent or {}).get("games", 0), (recent or {}).get("goals", 0)
    rows = []
    for p in players:
        mfrac = min(1.0, p["minutes"] / (90 * team_games)) if p["minutes"] else min(1.0, p["apps"] / team_games) * 0.6
        r = rec.get(str(p.get("pid")), [0, 0, 0])
        share_goal = (p["goals"] + r[1] + k * GOAL_PRIOR[p["pos"]] * mfrac) / (team_goals + r_goals + k)
        share_dec = min(1.2, (p["goals"] + p["assists"] + r[1] + r[2] + k * DEC_PRIOR[p["pos"]] * mfrac) / (team_goals + r_goals + k))
        pg, pd = 1 - math.exp(-lam * share_goal), 1 - math.exp(-lam * share_dec)
        if recent and r_games >= 5 and r[0] == 0:          # n'a joué aucun des derniers matchs
            pg, pd = 1 - (1 - pg) ** BENCH_SHARE, 1 - (1 - pd) ** BENCH_SHARE
        rows.append(dict(name=p["name"], pos=POSTES[p["pos"]], side=side, team=team,
                         goals=p["goals"], assists=p["assists"], apps=p["apps"],
                         l10=dict(n=r_games, gp=r[0], g=r[1], a=r[2]) if recent else None,
                         p_goal=pct(pg),
                         p_dec=pct(pd),
                         # pour vérifier « buteur ou passeur » après le match (statistiques ESPN du joueur)
                         pid=p.get("pid"), stats_league=squad.get("league"), season=squad.get("season")))
    gone = apply_lineup(rows, lineup)
    if missing is not None:
        missing += gone
    rows.sort(key=lambda r: (-r["p_goal"], -r["p_dec"]))
    return rows[:SQUAD_SIZE]


# ---------------------------------------------------------------- foot

FORM_FR = {"W": "V", "D": "N", "L": "D"}


def foot(today, history):
    rows = []
    failures = 0
    for league, code in LEAGUES:
        try:
            got = foot_league(today, history, league, code)
            if got is None:
                failures += 1
            else:
                rows += got
        except Exception as exc:          # une ligue en erreur ne doit pas bloquer les autres
            failures += 1
            note(f"{league} : erreur inattendue ({exc.__class__.__name__}).")
    if failures == len(LEAGUES):
        note("Foot : l'API d'ESPN ne répond pas ou a changé de format. Aucune donnée foot aujourd'hui.")
    rows.sort(key=lambda r: r["kickoff"])
    return rows


def foot_league(today, history, league, code):
    TABLES.pop(code, None)
    # 1. résultats des jours précédents
    for back in range(1, DAYS_BACK + 1):
        for e in espn_events("soccer", code, today - dt.timedelta(days=back)) or []:
            resolve_foot(e, history)
    # 2. matchs du jour
    events = espn_events("soccer", code, today)
    if events is None:
        return None
    for e in events:
        resolve_foot(e, history)
    todays = [e for e in events if paris(parse_time(e["date"])).date() == today]
    if not todays:
        return []
    table = espn_standings(code)
    gp = sum(r["gp"] for r in table.values())
    league_gpg = (sum(r["gf"] for r in table.values()) / gp) if gp else 1.4
    leaders = {}
    rows = []
    for e in todays:
        _, H, A = competitors(e)
        squads = {s: espn_squad(code, str(c["team"]["id"])) for s, c in (("h", H), ("a", A))}
        recents = {}
        for s, c in (("h", H), ("a", A)):
            try:      # forme sur le championnat national (aussi pour les coupes d'Europe)
                recents[s] = foot_recent((squads.get(s) or {}).get("league") or code, str(c["team"]["id"]))
            except Exception:
                recents[s] = None
        state = ((e.get("status") or {}).get("type") or {}).get("state", "pre")
        soon = parse_time(e["date"]) - CLOCK[0] <= LINEUP_BEFORE
        lineup = espn_lineup(code, e["id"]) if state == "in" or (state == "pre" and soon) else None
        if not all(squads.values()) and not leaders:
            leaders = espn_leaders(code)          # repli : top 50 des buteurs et passeurs du championnat
            if not leaders:
                note(f"{league} : joueurs indisponibles aujourd'hui.")
        hid_, aid_ = str(H["team"]["id"]), str(A["team"]["id"])
        try:
            if code in EUROPE:      # deux championnats différents : chaque équipe notée dans le sien
                ch, ca = ((squads.get(s) or {}).get("league") for s in ("h", "a"))
                (th, mh), (ta, ma) = table_of(ch), table_of(ca)
                lam = foot_model(th.get(hid_), ta.get(aid_), recents.get("h"), recents.get("a"), mh, ma, ch or "?", ca or "?")
            else:
                ch = ca = code
                lam = foot_model(table.get(hid_), table.get(aid_), recents.get("h"), recents.get("a"), league_gpg or 1.4)
        except Exception as err:
            print(f"Analyse maison {code} {e.get('id')} : {err!r}")
            ch = ca = lam = None
        try:
            elo = elo_probs(ch, ca, hid_, aid_, today) if ch and ca else None
        except Exception as err:
            print(f"Elo maison {code} {e.get('id')} : {err!r}")
            elo = None
        model = dict(lam=lam, elo=elo)
        rows.append(foot_match(e, league, table, league_gpg or 1.4, leaders, history, squads, lineup, recents, model))
    return rows


def competitors(e):
    comp = e["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    return comp, teams["home"], teams["away"]


def resolve_foot(e, history):
    key = f"foot-{e['id']}"
    if key not in history or history[key].get("result") is not None:
        return
    if not ((e.get("status") or {}).get("type") or {}).get("completed"):
        return
    _, h, a = competitors(e)
    try:
        sh, sa = int(h["score"]), int(a["score"])
    except (KeyError, TypeError, ValueError):
        return
    history[key]["score"] = f"{sh}-{sa}"
    history[key]["result"] = 0 if sh > sa else (1 if sh == sa else 2)
    # buteurs (hors contre-son-camp) : servent à vérifier les pronostics « X marque »
    comp = (e.get("competitions") or [{}])[0]
    if "details" in comp:          # sans ce détail, les pronostics buteur restent non vérifiables
        goals = [d for d in comp.get("details") or [] if d.get("scoringPlay")]
        if goals:                  # qui a ouvert le score : pour « Premier but : X »
            tid = str((goals[0].get("team") or {}).get("id", ""))
            history[key]["first"] = "h" if tid == str(h["team"]["id"]) else "a" if tid == str(a["team"]["id"]) else None
        ht = [0, 0]
        for d in goals:
            m = re.match(r"(\d+)", ((d.get("clock") or {}).get("displayValue") or ""))
            if m and int(m.group(1)) <= 45:          # « 45'+2' » compte encore pour la première période
                tid = str((d.get("team") or {}).get("id", ""))
                ht[0 if tid == str(h["team"]["id"]) else 1] += 1
        history[key]["ht"] = ht
        history[key]["scorers"] = [((d.get("athletesInvolved") or [{}])[0] or {}).get("displayName", "")
                                   for d in comp.get("details") or []
                                   if d.get("scoringPlay") and not d.get("ownGoal")]


def foot_players(squad, team_goals, lam):
    rows = []
    for p in squad:
        g, a = p["goals"], p["assists"]
        share_goal = (g + 0.12 * 5) / (team_goals + 5)
        share_dec = min(1.5, (g + a + 0.25 * 5) / (team_goals + 5))
        rows.append(dict(name=p["name"], goals=g, assists=a, pos="", apps=p.get("apps") or None,
                         p_goal=pct(1 - math.exp(-lam * share_goal * 0.85)),
                         p_dec=pct(1 - math.exp(-lam * share_dec * 0.85))))
    return rows


def foot_match(e, league, table, league_gpg, leaders, history, squads=None, lineup=None, recents=None, model=None):
    comp, H, A = competitors(e)
    kickoff = parse_time(e["date"])
    hid, aid = str(H["team"]["id"]), str(A["team"]["id"])
    home = H["team"].get("shortDisplayName") or H["team"].get("displayName")
    away = A["team"].get("shortDisplayName") or A["team"].get("displayName")
    sh, sa = table.get(hid), table.get(aid)
    state = ((e.get("status") or {}).get("type") or {}).get("state", "pre")
    key = f"foot-{e['id']}"
    saved = history.get(key, {}).get("p")
    odds = espn_odds(comp, three_way=True)
    over = None
    absents = []
    if state != "pre" and saved and len(saved) == 3:
        # après le coup d'envoi : on garde l'analyse d'avant-match
        p_h, p_d, p_a = saved
        over = history[key].get("over")
        lh, la = history[key].get("lam") or foot_lambdas(p_h, p_a, over)
        source = history[key].get("source", "Analyse d'avant-match")
    elif state == "pre" and model and (model.get("lam") or model.get("elo")):
        # analyse maison : buts (saison + 10 derniers matchs) et Elo maison (deux saisons et demie de résultats),
        # moitié-moitié ; le total de buts attendu reste celui des statistiques de buts
        elo = model.get("elo")
        SECOND_STATS["foot"][0] += bool(elo)
        if model.get("lam"):
            lh, la = model["lam"]
            p_h, p_d, p_a = outcome_probs(lh, la)
            source = SRC_FOOT
            if elo:
                p_h, p_d, p_a = (ELO_WEIGHT * e_ + (1 - ELO_WEIGHT) * m_ for e_, m_ in zip(elo["p"], (p_h, p_d, p_a)))
                lh, la = foot_lambdas(p_h, p_a, dict(line=2.5, p=p_over(lh + la, 2.5)))
                source = SRC_FOOT_ELO
        else:
            lh, la = foot_lambdas(elo["p"][0], elo["p"][2])
            source = SRC_FOOT_ELO_ONLY
        # compo officielle connue : un buteur important absent fait baisser l'attaque de son équipe
        for side in ("h", "a"):
            loss, who = lineup_loss((squads or {}).get(side), (lineup or {}).get(side), (recents or {}).get(side))
            cut = min(ABSENT_CAP, ABSENT_WEIGHT * loss)
            if cut >= 0.03:
                if side == "h":
                    lh *= 1 - cut
                else:
                    la *= 1 - cut
                absents.append((home if side == "h" else away, who, cut))
        p_h, p_d, p_a = outcome_probs(lh, la)
    elif odds and state == "pre":
        p_h, p_d, p_a = odds["p"]
        over = odds.get("over")
        lh, la = foot_lambdas(p_h, p_a, over)
        source = "Marché des paris (l'analyse maison manque de données pour ce match)"
    elif sh and sa:
        lh, la = table_lambdas(sh, sa, league_gpg)
        p_h, p_d, p_a = outcome_probs(lh, la)
        source = ("Modèle maison fondé sur le classement (pas de cotes disponibles)" if state == "pre"
                  else "Cotes d'avant-match non archivées : estimation fondée sur le classement")
    else:
        lh, la = 1.45, 1.15
        p_h, p_d, p_a = outcome_probs(lh, la)
        source = "Estimation par défaut (ni cotes ni classement)"
    scores, scorep = top_scores(lh, la)
    g = score_grid(lh, la)
    over25 = 1 - sum(g[i][j] for i in range(3) for j in range(3) if i + j <= 2)
    btts = (1 - math.exp(-lh)) * (1 - math.exp(-la))

    squads, lineup = squads or {}, lineup or {}
    team_rows, stats_from, thin, missing = {}, set(), [], []
    for side, tid, st, lam, name in (("h", hid, sh, lh, home), ("a", aid, sa, la, away)):
        if squads.get(side):
            team_rows[side] = squad_rows(squads[side], lam, side, name, lineup.get(side), missing, (recents or {}).get(side))
            stats_from.add(squads[side]["league"])
            if max(p["apps"] for p in squads[side]["players"]) < 3:
                thin.append(name)
        else:
            rows = foot_players(leaders.get(tid, []), max((st or {}).get("gf", 0), 1), lam)
            for r in rows:
                r.update(side=side, team=name)
            missing += apply_lineup(rows, lineup.get(side))
            team_rows[side] = sorted(rows, key=lambda r: -r["p_goal"])[:SQUAD_SIZE]

    # cotes du bookmaker : lues avant le match, archivées pour rester affichées après le coup d'envoi
    book = (dict(h=odds["dec"][0], d=odds["dec"][1], a=odds["dec"][2], by=odds["provider"])
            if odds and state == "pre" else history.get(key, {}).get("odds"))
    if state == "pre":
        old = history.get(key, {})
        history[key] = dict(sport="foot", league=league, date=paris(kickoff).date().isoformat(),
                            home=home, away=away, p=[round(p_h, 4), round(p_d, 4), round(p_a, 4)],
                            lam=[round(lh, 3), round(la, 3)], source=source, result=old.get("result"), score=old.get("score"))
        if odds:          # marché des paris : garde-fou invisible et point de comparaison du Bilan
            history[key]["p_mkt"] = [round(x, 4) for x in odds["p"]]
        if (model or {}).get("elo"):
            history[key]["elo"] = [model["elo"]["h"], model["elo"]["a"]]
        if book:
            history[key]["odds"] = book
        if over:
            history[key]["over"] = over

    ctx = []
    for name, st in ((home, sh), (away, sa)):
        if st:
            ctx.append(f"{name} : {st['rank']}{'er' if st['rank'] == 1 else 'e'}, {plural(st['pts'], 'point')}, "
                       f"{plural(st['gf'], 'but')} marqué{'s' if st['gf'] > 1 else ''} et "
                       f"{st['ga']} encaissé{'s' if st['ga'] > 1 else ''} en {plural(st['gp'], 'match', 'matchs')}.")
    for name, rec in ((home, (recents or {}).get("h")), (away, (recents or {}).get("a"))):
        if rec and rec.get("n10"):
            ctx.append(f"{name} : {plural(rec['gf10'], 'but')} marqué{'s' if rec['gf10'] > 1 else ''} et "
                       f"{rec['ga10']} encaissé{'s' if rec['ga10'] > 1 else ''} sur ses {rec['n10']} derniers matchs de championnat.")
    for name, who, cut in absents:
        ctx.append(f"{name} : attaque revue à la baisse (−{pct(cut)} %) d'après la compo officielle"
                   + (f" ({', '.join(who)} ne commence{'nt' if len(who) > 1 else ''} pas)." if who else "."))
    if not team_rows["h"] and not team_rows["a"]:
        ctx.append("Statistiques des joueurs indisponibles pour ce match.")
    if lineup:
        ctx.append("Compositions officielles prises en compte" + (f" : {', '.join(missing)} ne commence{'nt' if len(missing) > 1 else ''} pas le match."
                                                                 if missing else " : les joueurs clés sont titulaires."))
    for name in thin:
        ctx.append(f"{name} : ESPN ne connaît que 1 ou 2 matchs de ses joueurs, chiffres des joueurs très incertains.")
    if league in EUROPE.values() and stats_from - set(EUROPE):
        ctx.append(f"Joueurs : statistiques de la saison en championnat national (plus de matchs que la {league}).")

    picks = foot_picks(home, away, p_h, p_d, p_a, over25, btts, team_rows, scores, scorep, lh, la)
    # « pourquoi » figé avant le coup d'envoi (après, les derniers matchs incluraient celui-ci)
    if state == "pre":
        why = foot_why(home, away, (recents or {}).get("h"), (recents or {}).get("a"), sh, sa)
        if key in history:
            history[key]["why"] = why
    else:
        why = history.get(key, {}).get("why")
    if why and picks.get("main") and not picks["main"].get("why"):
        picks["main"]["why"] = why
    # garde-fou : le marché des paris (jamais affiché) ; s'il contredit nettement l'analyse, pas de Safe
    mkt = history.get(key, {}).get("p_mkt")
    second = dict(src="marché des paris", p=mkt) if mkt and source in (SRC_FOOT, SRC_FOOT_ELO, SRC_FOOT_ELO_ONLY) else None
    agree = concord("foot", picks, (p_h, p_d, p_a), second)

    fav, pf = (home, p_h) if p_h >= p_a else (away, p_a)
    if pf >= 0.6:
        reading = f"{fav} est nettement favori ({pct(pf)} %)."
    elif pf >= 0.45:
        reading = f"{fav} est favori sans écraser le match ({pct(pf)} %)."
    else:
        reading = "Match très ouvert : aucune équipe ne dépasse 45 %."
    if p_d >= 0.29:
        reading += f" Le nul est une issue sérieuse ({pct(p_d)} %)."
    stakes = foot_stakes(league, home, away, sh, sa, table)

    def form(c):
        return "".join(FORM_FR.get(ch, "") for ch in (c.get("form") or "")[-5:])

    return dict(
        id=key, sport="foot", league=league, kickoff=kickoff.isoformat(), time=paris(kickoff).strftime("%H:%M"),
        state=state, score=(f"{H.get('score')}-{A.get('score')}" if state != "pre" else None),
        home=home, away=away, form_h=form(H), form_a=form(A),
        rank_h=(sh or {}).get("rank"), rank_a=(sa or {}).get("rank"),
        pH=pct(p_h), pD=pct(p_d), pA=pct(p_a), source=source,
        xgH=round(lh, 2), xgA=round(la, 2), scores=scores, scorep=scorep,
        over25=pct(over25), btts=pct(btts), squad_h=team_rows["h"], squad_a=team_rows["a"],
        ctx=ctx, reading=reading, stakes=stakes, picks=picks, second=agree,
        **foot_visuals(league, H, A))


# ---------------------------------------------------------------- Elo → probabilités, xG NHL (MoneyPuck)
# Garde-fou : l'analyse maison est comparée au marché des paris (concord) ; en cas de désaccord, pas de Safe.

SECOND = {}           # sources lues une fois par passage
SECOND_STATS = {"foot": [0, []], "nhl": [0, []]}     # matchs couverts par l'Elo / les xG, équipes non trouvées


def read_csv(text):
    try:
        return list(csv.DictReader(io.StringIO(text or "")))
    except csv.Error:
        return []


def elo_wdl(dr):
    """Victoire, nul, défaite à partir de l'écart Elo (avantage du terrain compris). Score attendu Elo :
    1 / (1 + 10^(-dr/400)) ; les nuls sont plus rares quand l'écart grandit."""
    e = 1 / (1 + 10 ** (-dr / 400))
    d = ELO_DRAW * math.exp(-(dr / 600) ** 2)
    h = max(0.01, min(0.98, e - d / 2))
    return h, d, max(0.01, 1 - h - d)


MP_ABBR = {"TBL": "TB", "NJD": "NJ", "SJS": "SJ", "LAK": "LA"}


def mp_rates(season):
    """xG pour et contre par heure de jeu de chaque équipe (situation « all », corrigés du score et du terrain)."""
    key = f"mp-{season}"
    if key not in SECOND:
        out = {}
        for r in read_csv(get_text(MONEYPUCK_TEAMS.format(season=season))):
            if r.get("situation") != "all":
                continue
            try:
                ice = float(r["iceTime"]) / 3600
                xf = float(r.get("scoreVenueAdjustedxGoalsFor") or r["xGoalsFor"])
                xa = float(r.get("scoreVenueAdjustedxGoalsAgainst") or r["xGoalsAgainst"])
                gp = int(float(r["games_played"]))
            except (KeyError, TypeError, ValueError):
                continue
            if ice > 0:
                out[(r.get("team") or "").replace(".", "")] = (xf / ice, xa / ice, gp)
        SECOND[key] = out
    return SECOND[key]


def nhl_model(H, A, season, b2b_h=False, b2b_a=False):
    """Analyse maison NHL : buts attendus de chaque équipe d'après les xG de la saison (MoneyPuck), ramenés vers
    la saison passée tant que peu de matchs sont joués ; avantage de la glace ; fatigue si l'équipe a joué la veille.
    Renvoie (buts attendus domicile, extérieur) ou None."""
    r = nhl_xg(H, A, season)
    if not r:
        return None
    lh, la = r
    return lh * (B2B_FACTOR if b2b_h else 1), la * (B2B_FACTOR if b2b_a else 1)


def nhl_xg(H, A, season):
    """Buts attendus (domicile, extérieur) d'après les xG pour et contre de chaque équipe (MoneyPuck), ramenés
    vers la saison passée (ou la moyenne) tant que peu de matchs sont joués ; léger avantage de la glace."""
    year = int(str(season)[:4])
    cur, prev = mp_rates(year), mp_rates(year - 1)
    teams = list(cur.values()) or list(prev.values())
    if not teams:
        return None
    avg = sum(x[0] for x in teams) / len(teams)

    def rate(abbr):
        # MoneyPuck écrit « SJS » ou « S.J » selon les fichiers : on essaie le code NHL tel quel, puis sa variante
        k = next((x for x in (abbr, MP_ABBR.get(abbr, abbr)) if x in cur or x in prev), abbr)
        base = prev.get(k, (avg, avg, 0))
        c = cur.get(k)
        if not c and k not in prev:
            return None
        if not c:
            return base[0], base[1]
        w = c[2] / (c[2] + NHL_SHRINK)
        return w * c[0] + (1 - w) * base[0], w * c[1] + (1 - w) * base[1]
    rh, ra = rate(H), rate(A)
    if not rh or not ra:
        SECOND_STATS["nhl"][1].extend(x for x, r in ((H, rh), (A, ra)) if not r)
        return None
    return rh[0] * ra[1] / avg * 1.03, ra[0] * rh[1] / avg / 1.03


def concord(sport, picks, odds_p, second):
    """Compare l'analyse maison (odds_p) au marché des paris (second, jamais affiché). Désaccord (pas le même
    favori, ou Safe jugé au moins 12 points moins sûr par le marché) : le Safe est retiré (picks["held"]) et le
    match affiche « avis partagés »."""
    if not second:
        return None
    if sport == "foot":
        h2, d2, a2 = second["p"]
        h1, d1, a1 = odds_p
        prob = lambda h, d, a: {"1": h, "2": a, "X": d, "1N": h + d, "N2": d + a, "12": h + a}
        out = dict(src=second["src"])
    else:
        h2, a2, h1, a1 = second["winH"], 1 - second["winH"], odds_p[0], 1 - odds_p[0]
        prob = lambda h, d, a: {"win_h": h, "win_a": a}
        d1 = d2 = 0
        out = dict(src=second["src"])
    fav1, fav2 = ("h" if h1 >= a1 else "a"), ("h" if h2 >= a2 else "a")
    clash = bool(fav1 != fav2 and abs(h2 - a2) >= 0.05 and abs(h1 - a1) >= 0.05)
    main = picks.get("main") or {}
    p1, p2 = prob(h1, d1, a1).get(main.get("market")), prob(h2, d2, a2).get(main.get("market"))
    gap = (p1 - p2) if p1 is not None and p2 is not None else 0
    out["agree"] = bool(not clash and gap < SPLIT_GAP)
    if clash:
        out["note"] = "l'analyse et le marché des paris ne désignent pas le même favori"
    elif gap >= SPLIT_GAP:
        out["note"] = f"« {main['label']} » : {pct(p1)} % selon l'analyse, le marché des paris est nettement moins confiant"
    if not out["agree"] and main and (p2 is not None or clash):
        picks["held"] = dict(main, note=out["note"])
        del picks["main"]
    return out


# ---------------------------------------------------------------- pronostics

STAR_STEPS = (0.5, 0.6, 0.7, 0.8)      # 1 étoile sous 50 %, 5 étoiles à partir de 80 %


def make_pick(market, label, p, **extra):
    p = float(p)          # les calculs scipy renvoient des types numpy, refusés par json
    return dict(market=market, label=label, p=round(p, 4), prob=int(pct(p)),
                fair=round(1 / max(p, 0.01), 2),          # « cote juste » : 1 / probabilité, sans marge de bookmaker
                stars=1 + sum(int(p >= s) for s in STAR_STEPS), **extra)


# Paris « fun » : les vrais paris à cote, ceux que les bookmakers mettent en avant (mi-temps/fin de match,
# résultat + buts, buteur + victoire, écart de buts, puck line NHL…). Sur ces marchés, la marge du bookmaker
# est forte (15 à 20 % en mi-temps/fin de match, contre 6 à 8 % sur le résultat simple) : pour une cote réelle
# d'au moins 2, la cote juste doit dépasser ~2,4, soit une probabilité de 42 % au plus. En dessous de 18 %
# (cote juste > 5,5), le pari n'est plus « plausible ».
FUN_LO, FUN_HI = 0.18, 0.42
FUN_MAX = 3            # paris fun proposés par match (familles différentes)
HALF1 = 0.45           # part des buts marqués en première période (football)
ENG_SHIFT = 0.25       # NHL : part des victoires d'un but qui deviennent +2 grâce au but dans la cage vide


def market_fn(sport, market, q=0.5):
    """Événement d'un pronostic sur le score final (h, a) : 1 s'il passe, 0 sinon. En NHL, une égalité après
    60 minutes va en prolongation : q = part des prolongations gagnées par l'équipe à domicile. None = pas un
    pari sur le score (buteur…). Même table que market dans docs/live.js."""
    line = re.fullmatch(r"([ou])(\d)5", market or "")
    ot = 1 if sport == "nhl" else 0             # NHL : le but de la prolongation compte dans le total
    if line:
        k = int(line.group(2))
        return (lambda h, a: h + a + ot * (h == a) > k) if line.group(1) == "o" else (lambda h, a: h + a + ot * (h == a) <= k)
    combo = re.fullmatch(r"w([ha])_(btts|nil|by1|o(\d)5)", market or "")
    if combo:                                   # victoire + autre condition (paris fun)
        side, cond, k = combo.group(1), combo.group(2), combo.group(3)

        def f(h, a):
            mine, other = (h, a) if side == "h" else (a, h)
            win = 1 if mine > other else (q if side == "h" else 1 - q) if (sport == "nhl" and h == a) else 0
            if cond == "btts":
                return win * (other > 0)
            if cond == "nil":
                return win * (other == 0)
            if cond == "by1":                   # NHL : une prolongation se gagne toujours d'un but
                return float(mine - other == 1) + (win if sport == "nhl" and h == a else 0)
            return win * (h + a + ot * (h == a) > int(k))
        return f
    if market in ("htft_h", "htft_a"):          # implique la victoire de cette équipe (sert à la cohérence)
        return (lambda h, a: h > a) if market == "htft_h" else (lambda h, a: a > h)
    if market == "btts_o25":
        return lambda h, a: h > 0 and a > 0 and h + a > 2
    if sport == "nhl":
        return {"win_h": lambda h, a: 1 if h > a else (q if h == a else 0),
                "win_a": lambda h, a: 1 if a > h else (1 - q if h == a else 0),
                "h2": lambda h, a: h - a >= 2, "a2": lambda h, a: a - h >= 2,
                "h3": lambda h, a: h - a >= 3, "a3": lambda h, a: a - h >= 3,
                "reg_h": lambda h, a: h > a, "reg_a": lambda h, a: a > h, "ot": lambda h, a: h == a}.get(market)
    return {"1": lambda h, a: h > a, "2": lambda h, a: a > h, "X": lambda h, a: h == a,
            "1N": lambda h, a: h >= a, "N2": lambda h, a: a >= h, "12": lambda h, a: h != a,
            "btts_y": lambda h, a: h > 0 and a > 0, "btts_n": lambda h, a: h == 0 or a == 0,
            "h2": lambda h, a: h - a >= 2, "a2": lambda h, a: a - h >= 2,
            "h3": lambda h, a: h - a >= 3, "a3": lambda h, a: a - h >= 3,
            "cs_h": lambda h, a: a == 0, "cs_a": lambda h, a: h == 0,
            # premier but : avec deux lois de Poisson, chaque but est de l'équipe à domicile avec la probabilité h / (h + a)
            "fg_h": lambda h, a: h / (h + a) if h + a else 0, "fg_a": lambda h, a: a / (h + a) if h + a else 0}.get(market)


def coherent(P, anchors, f):
    """Un pronostic est cohérent avec l'analyse s'il ne la contredit pas : quand il passe, chaque pronostic
    d'appui (Safe, Vainqueur) a au moins autant de chances de passer (corrélation positive ou nulle)."""
    pf = P(f)
    return all(P(lambda h, a, g=g: g(h, a) * f(h, a)) >= P(g) * pf - 1e-9 for g in anchors)


def fun_picks(sport, home, away, lh, la, q, exclude=(), anchors=(), scorer=None):
    """Paris « fun » d'avant-match : cote juste d'au moins ~2,4 (probabilité 18 à 42 %), une famille de pari
    chacun, les plus probables d'abord, tous dans le sens de l'analyse (cohérents avec le Safe et le Vainqueur :
    anchors = leurs marchés), jamais le même pari qu'eux. scorer = buteur proposé {name, team, side, p}.
    Même logique que funPicks dans docs/live.js (qui recalcule pendant le match)."""
    n = 11
    ph = [poisson.pmf(i, lh) for i in range(n)]
    pa = [poisson.pmf(j, la) for j in range(n)]
    z = sum(ph) * sum(pa)

    def P(f):
        return sum(ph[i] * pa[j] * f(i, j) for i in range(n) for j in range(n)) / z
    names = {"h": home, "a": away}
    c = []                                  # (famille, marché, libellé, probabilité ou None = calcul sur la grille, f, extra)
    if sport == "foot":
        for s in ("h", "a"):
            N = names[s]
            c += [("win_btts", f"w{s}_btts", f"{N} gagne et les deux équipes marquent", None),
                  ("win_o25", f"w{s}_o25", f"{N} gagne et plus de 2,5 buts", None),
                  ("win_nil", f"w{s}_nil", f"{N} gagne sans encaisser de but", None),
                  ("by1", f"w{s}_by1", f"{N} gagne d'un seul but", None),
                  ("margin", f"{s}2", f"{N} gagne avec 2 buts d'écart ou plus", None),
                  ("margin3", f"{s}3", f"{N} gagne avec 3 buts d'écart ou plus", None)]
            # mi-temps / fin de match : deux mi-temps indépendantes (45 % des buts avant la pause)
            h1 = [poisson.pmf(i, lh * HALF1) for i in range(8)]
            a1 = [poisson.pmf(j, la * HALF1) for j in range(8)]
            h2 = [poisson.pmf(i, lh * (1 - HALF1)) for i in range(8)]
            a2 = [poisson.pmf(j, la * (1 - HALF1)) for j in range(8)]
            lead = (lambda i, j: i > j) if s == "h" else (lambda i, j: j > i)
            p_htft = sum(h1[i] * a1[j] * h2[k] * a2[l] for i in range(8) for j in range(8) if lead(i, j)
                         for k in range(8) for l in range(8) if lead(i + k, j + l))
            c.append(("htft", f"htft_{s}", f"{N} mène à la mi-temps et gagne le match", p_htft))
        c += [("btts_o25", "btts_o25", "Les deux équipes marquent et plus de 2,5 buts", None),
              ("ou", "o35", "Plus de 3,5 buts", None), ("draw", "X", "Match nul", None)]
        if scorer and scorer.get("p"):
            s, lam = scorer["side"], (lh if scorer["side"] == "h" else la)
            lp = -math.log(max(1e-9, 1 - scorer["p"]))           # buts attendus du joueur
            share = min(0.95, lp / lam) if lam else 0
            mine = (lambda i, j: i) if s == "h" else (lambda i, j: j)
            win = (lambda i, j: i > j) if s == "h" else (lambda i, j: j > i)
            p_sw = P(lambda i, j: win(i, j) * (1 - (1 - share) ** mine(i, j)))
            extra = dict(player=scorer["name"], team=scorer.get("team"), side=s)
            c.append(("player_win", "scw", f"{scorer['name']} marque et {scorer.get('team') or names[s]} gagne", p_sw, extra))
            c.append(("player2", "sc2", f"{scorer['name']} marque au moins 2 buts", 1 - math.exp(-lp) * (1 + lp), extra))
    else:
        for s in ("h", "a"):
            N = names[s]
            diff = (lambda i, j: i - j) if s == "h" else (lambda i, j: j - i)
            p_pl = P(lambda i, j: diff(i, j) >= 2) + ENG_SHIFT * P(lambda i, j: diff(i, j) == 1)
            p_pl3 = P(lambda i, j: diff(i, j) >= 3) + 0.4 * ENG_SHIFT * P(lambda i, j: diff(i, j) == 2)
            c += [("win_o55", f"w{s}_o55", f"{N} gagne et plus de 5,5 buts", None),
                  ("win_o65", f"w{s}_o65", f"{N} gagne et plus de 6,5 buts", None),
                  ("puckline", f"{s}2", f"{N} gagne par 2 buts d'écart ou plus", p_pl),
                  ("puckline3", f"{s}3", f"{N} gagne par 3 buts d'écart ou plus", p_pl3),
                  ("by1", f"w{s}_by1", f"{N} gagne d'un seul but (prolongation comprise)",
                   (1 - ENG_SHIFT) * P(lambda i, j: diff(i, j) == 1) + P(lambda i, j: (i == j) * (q if s == "h" else 1 - q))),
                  ("reg", f"reg_{s}", f"{N} gagne dans le temps réglementaire", None)]
        c += [("ot", "ot", "Il y aura prolongation", None), ("ou", "o65", "Plus de 6,5 buts", None)]
    anchor_fns = [g for g in (market_fn(sport, mk, q) for mk in anchors) if g]
    out = []
    for fam, market, label, prob, *rest in c:
        f = market_fn(sport, market, q)
        if market == "sc2":                          # 2 buts du joueur : il faut que son équipe en marque 2
            f = (lambda h, a: h >= 2) if rest[0]["side"] == "h" else (lambda h, a: a >= 2)
        elif market == "scw":
            f = market_fn(sport, "1" if rest[0]["side"] == "h" else "2", q)
        prob = P(f) if prob is None else prob
        if FUN_LO <= prob <= FUN_HI and label not in exclude and market not in anchors and coherent(P, anchor_fns, f):
            out.append((prob, fam, market, label, rest[0] if rest else {}))
    best = {}
    for prob, fam, market, label, extra in sorted(out, key=lambda x: -x[0]):
        best.setdefault(fam, make_pick(market, label, prob, **extra))
    return sorted(best.values(), key=lambda x: -x["p"])[:FUN_MAX]


def side_rows(team_rows, side, keys):
    """Joueurs de l'équipe annoncée gagnante (s'il y en a), sinon de tout le match."""
    own = [r for k in keys if k.endswith(side) for r in team_rows.get(k) or []]
    return own or [r for k in keys for r in team_rows.get(k) or []]


def coherent_score(lh, la, anchors, n=9):
    """Score exact le plus probable parmi ceux où le Safe et le Vainqueur passent (pas de « Victoire Lyon »
    avec un score exact 1-1)."""
    g = score_grid(lh, la, n)
    ok = [(g[i][j], i, j) for i in range(n) for j in range(n) if all(f(i, j) >= 0.5 for f in anchors)]
    if not ok:
        return None
    p, i, j = max(ok)
    return f"{i}-{j}", p


def foot_why(home, away, rec_h, rec_a, sh, sa):
    """Une ligne qui explique le Safe : buts marqués et encaissés des deux équipes sur leurs derniers matchs
    (repli : la saison)."""
    if rec_h and rec_a and rec_h.get("n10") and rec_a.get("n10"):
        n1, n2 = rec_h["n10"], rec_a["n10"]
        head = f"{n1} derniers matchs" if n1 == n2 else "Derniers matchs"
        a, b = (rec_h["gf10"], rec_h["ga10"]), (rec_a["gf10"], rec_a["ga10"])
    elif sh and sa and sh.get("gp") and sa.get("gp"):
        head, a, b = "Saison", (sh["gf"], sh["ga"]), (sa["gf"], sa["ga"])
    else:
        return None
    return f"{head} : {home} {a[0]} marqués / {a[1]} encaissés · {away} {b[0]} / {b[1]}"


def nhl_why(home, away, sh, sa):
    """Une ligne qui explique le Safe NHL : victoires sur les 10 derniers matchs (repli : buts par match)."""
    sh, sa = sh or {}, sa or {}
    if sh.get("l10Wins") is not None and sa.get("l10Wins") is not None:
        return f"10 derniers matchs : {home} {plural(sh['l10Wins'], 'victoire')} · {away} {sa['l10Wins']}"
    if sh.get("gamesPlayed") and sa.get("gamesPlayed"):
        f = lambda st: f"{st.get('goalFor', 0) / st['gamesPlayed']:.1f}".replace(".", ",")
        return f"Saison : {home} {f(sh)} buts par match · {away} {f(sa)}"
    return None


def foot_picks(home, away, p_h, p_d, p_a, over25, btts, team_rows, scores, scorep, lh=None, la=None):
    """Le pronostic le plus sûr du match, le vainqueur probable, le buteur le plus probable, le score."""
    team = [make_pick("1", f"Victoire {home}", p_h), make_pick("2", f"Victoire {away}", p_a),
            make_pick("1N", f"{home} ou nul", p_h + p_d), make_pick("N2", f"Nul ou {away}", p_d + p_a),
            # « pas de match nul » est volontairement exclu : souvent le plus probable, il ne dit pas qui gagne
            make_pick("o25", "Plus de 2,5 buts", over25), make_pick("u25", "Moins de 2,5 buts", 1 - over25),
            make_pick("btts_y", "Les deux équipes marquent", btts),
            make_pick("btts_n", "Au moins une équipe ne marque pas", 1 - btts)]
    picks = dict(main=max(team, key=lambda c: c["p"]),
                 winner=team[0] if p_h >= p_a else team[1],
                 score=make_pick("exact", f"Score exact {scores[0]}", scorep[0] / 100, value=scores[0]))
    anchors = (picks["main"]["market"], picks["winner"]["market"])
    fav_h = p_h >= p_a
    best = pick_scorer(side_rows(team_rows, "h" if fav_h else "a", ("h", "a")))
    if lh is not None:
        sc = dict(name=best["name"], team=best.get("team"), side=best.get("side", "h" if fav_h else "a"),
                  p=best["p_goal"] / 100) if best and best.get("p_goal") else None
        funs = fun_picks("foot", home, away, lh, la, 0.5, {picks["main"]["label"], picks["winner"]["label"]}, anchors, sc)
        if funs:
            picks["fun"], picks["fun_alt"] = funs[0], funs[1:]
        best_score = coherent_score(lh, la, [market_fn("foot", mk) for mk in anchors])
        if best_score:
            picks["score"] = make_pick("exact", f"Score exact {best_score[0]}", best_score[1], value=best_score[0])
    if best:
        extra = {k: best[k] for k in ("lineup", "pid", "stats_league", "season") if best.get(k)}
        extra["why"] = scorer_why(best, (home, p_h, lh) if best.get("side", "h" if fav_h else "a") == "h" else (away, p_a, la))
        picks["player"] = make_pick("scorer", f"{best['name']} marque", best["p_goal"] / 100,
                                    player=best["name"], team=best["team"], **extra)
        if best.get("p_dec"):
            picks["decisive"] = make_pick("dec", f"{best['name']} buteur ou passeur", best["p_dec"] / 100,
                                          player=best["name"], team=best["team"],
                                          base=[best.get("apps") or 0, best.get("goals") or 0, best.get("assists") or 0], **extra)
    return picks


def scorer_why(r, team):
    """Pourquoi ce joueur : sa réussite cette saison et ce que dit l'analyse du match."""
    name, p_win, lam = team
    bits = []
    if r.get("apps"):
        bits.append(f"{plural(r.get('goals', 0), 'but')} et {plural(r.get('assists', 0), 'passe')} en {plural(r['apps'], 'match', 'matchs')}")
    l10 = r.get("l10")
    same = l10 and (l10.get("gp"), l10.get("g"), l10.get("a")) == (r.get("apps"), r.get("goals"), r.get("assists"))
    if l10 and l10.get("n") and not same:        # début de saison : les 10 derniers matchs = toute la saison, rien à ajouter
        if not l10.get("gp"):
            bits.append(f"absent de ses {l10['n']} derniers matchs")
        elif r.get("sport") == "nhl" or "p_point" in r:
            bits.append(f"forme : {plural(l10['g'] + l10['a'], 'point')} ({plural(l10['g'], 'but')}) sur ses {plural(l10['gp'], 'dernier match', 'derniers matchs')}")
        else:
            bits.append(f"forme : {plural(l10['g'], 'but')} et {plural(l10['a'], 'passe')} sur ses {plural(l10['gp'], 'dernier match', 'derniers matchs')}")
    if r.get("lineup") == "start":
        bits.append("titulaire")
    bits.append(f"{name} {'favori' if p_win >= 0.45 else 'annoncé gagnant'} ({pct(p_win)} %)")
    if lam:
        bits.append(f"{str(round(lam, 1)).replace('.', ',')} but{'s' if lam >= 1.5 else ''} attendu{'s' if lam >= 1.5 else ''} pour {name}")
    return " · ".join(bits)


def pick_scorer(rows):
    """Le buteur proposé : dans l'équipe annoncée gagnante, un titulaire quand la composition est connue,
    un défenseur seulement s'il dépasse nettement (×1,3) le meilleur attaquant ou milieu."""
    if any(r.get("lineup") for r in rows):
        rows = [r for r in rows if r.get("lineup") == "start"] or rows
    if not rows:
        return None
    best = max(rows, key=lambda r: r["p_goal"])
    others = [r for r in rows if r.get("pos") != POSTES["D"]]
    if best.get("pos") == POSTES["D"] and others:
        alt = max(others, key=lambda r: r["p_goal"])
        if best["p_goal"] < 1.3 * alt["p_goal"]:
            best = alt
    return best


def nhl_picks(hname, aname, win_h, lh, la, team_rows, q=0.5):
    total = lh + la
    over55 = 1 - sum(poisson.pmf(k, total) for k in range(6))     # les égalités sont paires : la prolongation ne change rien
    team = [make_pick("win_h", f"Victoire {hname}", win_h), make_pick("win_a", f"Victoire {aname}", 1 - win_h),
            make_pick("o55", "Plus de 5,5 buts", over55), make_pick("u55", "Moins de 5,5 buts", 1 - over55)]
    picks = dict(main=max(team, key=lambda c: c["p"]), winner=team[0] if win_h >= 0.5 else team[1])
    funs = fun_picks("nhl", hname, aname, lh, la, q, {picks["main"]["label"], picks["winner"]["label"]},
                     (picks["main"]["market"], picks["winner"]["market"]))
    if funs:
        picks["fun"], picks["fun_alt"] = funs[0], funs[1:]
    home_wins = win_h >= 0.5
    players = side_rows(team_rows, "h" if home_wins else "a", ("best_h", "best_a"))
    if players:
        side = (hname if home_wins else aname, win_h if home_wins else 1 - win_h, lh if home_wins else la)
        why = lambda r: scorer_why({**r, "apps": r.get("gp")}, side)     # même texte « Pourquoi lui » que le foot
        best = max(players, key=lambda r: r["p_point"])
        picks["player"] = make_pick("point", f"{best['name']} : au moins 1 point", best["p_point"] / 100,
                                    player=best["name"], pid=best.get("pid"), team=best["team"], why=why(best))
        scorer = max(players, key=lambda r: r["p_goal"])
        picks["scorer"] = make_pick("goal", f"{scorer['name']} marque", scorer["p_goal"] / 100,
                                    player=scorer["name"], pid=scorer.get("pid"), team=scorer["team"], why=why(scorer))
    return picks


FUN_KINDS = {"fun", "score"}          # rangés à part : ils ne doivent pas fausser le bilan des pronostics sûrs


def pick_group(p):
    kinds = set(p.get("kinds") or [])
    return p.get("group") or ("fun" if kinds and kinds <= FUN_KINDS else "safe")


def record_picks(rows, store):
    """Archive les pronostics affichés tant que le match n'a pas commencé (les dernières cotes l'emportent)."""
    for m in rows:
        if m["state"] not in ("pre", "FUT", "PRE"):
            continue
        for kind in ("main", "winner", "player", "decisive", "scorer", "fun", "score"):
            pk = (m.get("picks") or {}).get(kind)
            if not pk or not pk.get("market"):
                continue
            pid = f"{m['id']}|{pk['market']}|{pk.get('player', '')}"
            old = store.get(pid, {})
            kinds = sorted(set(old.get("kinds", [])) | {kind})
            store[pid] = dict(match=m["id"], sport=m["sport"], league=m["league"], date=m["kickoff"][:10],
                              home=m["home"], away=m["away"], market=pk["market"], label=pk["label"], p=pk["p"],
                              stars=pk["stars"], player=pk.get("player"), pid_player=pk.get("pid"), value=pk.get("value"),
                              stats_league=pk.get("stats_league"), season=pk.get("season"), base=pk.get("base"),
                              side=pk.get("side"),
                              group="fun" if set(kinds) <= FUN_KINDS else "safe", kinds=kinds, result=old.get("result"))
        # un ancien pronostic que les nouvelles cotes ont remplacé n'est plus « affiché » : on le retire
        shown = {f"{m['id']}|{pk['market']}|{pk.get('player', '')}" for pk in (m.get("picks") or {}).values()
                 if isinstance(pk, dict) and pk.get("market")}
        for pid in [k for k, v in store.items() if v["match"] == m["id"] and v.get("result") is None and k not in shown]:
            del store[pid]


def goals_of(score):
    nums = [int(x) for x in str(score).split(" ")[0].split("-")]
    return nums[0], nums[1]


def resolve_picks(store, history):
    """Compare chaque pronostic au résultat réel. Renvoie le nombre de pronostics vérifiés."""
    boxes = {}
    done = 0
    for pk in store.values():
        if pk.get("result") is not None:
            continue
        h = history.get(pk["match"]) or {}
        if h.get("result") is None or not h.get("score"):
            continue
        gh, ga = goals_of(h["score"])
        mk, ok = pk["market"], None
        line = re.fullmatch(r"([ou])(\d)5", mk or "")
        if line:                                         # o25 = plus de 2,5 buts, u35 = moins de 3,5 buts…
            k = int(line.group(2))
            ok = gh + ga > k if line.group(1) == "o" else gh + ga <= k
        elif mk == "exact":
            ok = f"{gh}-{ga}" == pk.get("value")
        elif pk["sport"] == "foot":
            ok = {"1": gh > ga, "2": ga > gh, "X": gh == ga, "1N": gh >= ga, "N2": ga >= gh, "12": gh != ga,
                  "btts_y": gh > 0 and ga > 0, "btts_n": gh == 0 or ga == 0,
                  "h2": gh - ga >= 2, "a2": ga - gh >= 2, "h3": gh - ga >= 3, "a3": ga - gh >= 3,
                  "cs_h": ga == 0, "cs_a": gh == 0}.get(mk)
            if mk in ("fg_h", "fg_a"):                   # 0-0 : personne n'a marqué, pari perdu
                ok = False if gh + ga == 0 else (h["first"] == mk[-1] if h.get("first") else None)
            if mk == "scorer" and h.get("scorers") is not None:
                ok = norm(pk["player"]) in {norm(s) for s in h["scorers"]}
            if mk == "dec":
                ok = decisive_result(pk, h)
            combo = re.fullmatch(r"w([ha])_(btts|nil|by1|o(\d)5)", mk or "")
            if combo:
                mine, other = (gh, ga) if combo.group(1) == "h" else (ga, gh)
                cond = combo.group(2)
                ok = mine > other and (other > 0 if cond == "btts" else other == 0 if cond == "nil"
                                       else mine - other == 1 if cond == "by1" else gh + ga > int(combo.group(3)))
            if mk == "btts_o25":
                ok = gh > 0 and ga > 0 and gh + ga > 2
            if mk in ("htft_h", "htft_a") and h.get("ht"):
                hh, ha = h["ht"]
                ok = (hh > ha and gh > ga) if mk == "htft_h" else (ha > hh and ga > gh)
            if mk in ("scw", "sc2") and h.get("scorers") is not None:
                n_goals = sum(norm(s) == norm(pk["player"]) for s in h["scorers"])
                won = (gh > ga) if pk.get("side") == "h" else (ga > gh) if pk.get("side") == "a" else None
                ok = (n_goals >= 2) if mk == "sc2" else (None if won is None else n_goals >= 1 and won)
        else:
            ot = "prol" in h["score"] or "tirs" in h["score"]
            ok = {"win_h": h["result"] == 0, "win_a": h["result"] == 1, "ot": ot,
                  "reg_h": h["result"] == 0 and not ot, "reg_a": h["result"] == 1 and not ot,
                  "h2": gh - ga >= 2, "a2": ga - gh >= 2, "h3": gh - ga >= 3, "a3": ga - gh >= 3}.get(mk)
            combo = re.fullmatch(r"w([ha])_(by1|o(\d)5)", mk or "")
            if combo:
                won = h["result"] == (0 if combo.group(1) == "h" else 1)
                ok = won and (abs(gh - ga) == 1 if combo.group(2) == "by1" else gh + ga > int(combo.group(3)))
            if mk in ("point", "goal") and pk.get("pid_player"):
                gid = pk["match"].split("-", 1)[1]
                if gid not in boxes:
                    boxes[gid] = nhl_player_lines(gid)
                line = boxes[gid].get(pk["pid_player"])
                if boxes[gid]:
                    g, a = line if line else (0, 0)      # absent de la feuille : pronostic perdu
                    ok = (g + a >= 1) if mk == "point" else (g >= 1)
        if ok is not None:
            pk["result"] = bool(ok)
            done += 1
    return done


def decisive_result(pk, h):
    """« X buteur ou passeur » : validé s'il a marqué (feuille de match) ; sinon on compare ses buts + passes
    de la saison avant et après le match (statistiques ESPN). None = pas encore vérifiable."""
    if h.get("scorers") and norm(pk["player"]) in {norm(s) for s in h["scorers"]}:
        return True
    base = pk.get("base")
    if not (pk.get("pid_player") and pk.get("stats_league") and pk.get("season") and base):
        return None
    s = stat_values(get_json(ESPN_PLAYER_STATS.format(league=pk["stats_league"], season=pk["season"], athlete=pk["pid_player"])))
    if not s:
        return None
    apps, dec = int(s.get("appearances") or 0), int(s.get("totalGoals") or 0) + int(s.get("goalAssists") or 0)
    if apps == base[0] + 1:
        return dec > base[1] + base[2]
    days = (dt.date.today() - dt.date.fromisoformat(pk["date"])).days
    if apps == base[0] and days >= 2:          # n'a pas joué (statistiques à jour depuis longtemps) : perdu
        return False
    return None                                # pas encore à jour, ou un autre match joué depuis


def nhl_player_lines(game_id):
    """Buts et passes de chaque joueur d'un match NHL terminé : {playerId: (buts, passes)}."""
    box = nhl_get(f"/gamecenter/{game_id}/boxscore") or {}
    out = {}
    for side in ((box.get("playerByGameStats") or {}).get("homeTeam") or {},
                 (box.get("playerByGameStats") or {}).get("awayTeam") or {}):
        for grp in ("forwards", "defense"):
            for p in side.get(grp, []):
                out[p.get("playerId")] = (p.get("goals", 0) or 0, p.get("assists", 0) or 0)
    return out


def selection(rows, n=10):
    """Sélection du jour : les pronostics les plus sûrs des matchs pas encore commencés (2 au plus par match)."""
    cands = []
    for m in rows:
        if m["state"] not in ("pre", "FUT", "PRE"):
            continue
        for kind in ("main", "player"):
            pk = (m.get("picks") or {}).get(kind)
            if pk:
                cands.append(dict(pk, kind=kind, id=m["id"], sport=m["sport"], league=m["league"], time=m["time"],
                                  kickoff=m["kickoff"], home=m["home"], away=m["away"],
                                  logo_h=m.get("logo_h"), logo_a=m.get("logo_a"),
                                  logo_h_dark=m.get("logo_h_dark"), logo_a_dark=m.get("logo_a_dark")))
    return sorted(cands, key=lambda c: -c["p"])[:n]


def picks_bilan(store, history=None):
    every = [p for p in store.values() if p.get("result") is not None]
    done = [p for p in every if pick_group(p) == "safe"]
    fun_done = [p for p in every if pick_group(p) == "fun"]
    if not done:
        return dict(n=0)
    hits = sum(p["result"] for p in done)
    by_stars = []
    for s in range(1, 6):
        grp = [p for p in done if p["stars"] == s]
        if grp:
            by_stars.append(dict(stars=s, n=len(grp), announced=pct(sum(p["p"] for p in grp) / len(grp)),
                                 actual=pct(sum(p["result"] for p in grp) / len(grp))))
    def agg(rows):
        if not rows:
            return dict(n=0, ok=0)
        ok = sum(bool(r["result"]) for r in rows)
        return dict(n=len(rows), ok=ok, rate=pct(ok / len(rows)), expected=pct(sum(r["p"] for r in rows) / len(rows)))

    def by_sport(rows):
        return dict(all=agg(rows), foot=agg([r for r in rows if r["sport"] == "foot"]),
                    nhl=agg([r for r in rows if r["sport"] == "nhl"]))

    def league_of(p):
        return p.get("league") or ("NHL" if p["sport"] == "nhl" else "?")

    # bilan jour par jour (60 derniers jours), total depuis l'ouverture, par championnat
    dates = sorted({p["date"] for p in done}, reverse=True)
    days = [dict(date=d, **by_sport([p for p in done if p["date"] == d]),
                 fun=by_sport([p for p in fun_done if p["date"] == d])) for d in dates[:60]]
    leagues = [dict(sport=s, league=lg, **agg([p for p in done if p["sport"] == s and league_of(p) == lg]))
               for s, lg in {(p["sport"], league_of(p)) for p in done}]
    leagues.sort(key=lambda x: (x["sport"] != "foot", -x["n"], x["league"]))
    # détail des pronostics vérifiés des 30 derniers jours (pour les listes dépliables du site)
    last = set(dates[:30])
    details = sorted((p for p in every if p["date"] in last), key=lambda p: (p["date"], p["home"]), reverse=True)
    return dict(n=len(done), rate=pct(hits / len(done)), expected=pct(sum(p["p"] for p in done) / len(done)),
                since=dates[-1], total=by_sport(done), fun=by_sport(fun_done), days=days, leagues=leagues,
                by_stars=by_stars,
                details=[dict(date=p["date"], sport=p["sport"], league=league_of(p), mid=p["match"],
                              score=((history or {}).get(p["match"]) or {}).get("score"),
                              match=f"{p['home']} – {p['away']}", label=p["label"], prob=pct(p["p"]),
                              stars=p["stars"], ok=bool(p["result"]), fun=pick_group(p) == "fun") for p in details])


def foot_visuals(league, H, A):
    """Logos des équipes et du championnat, pays affiché dans le bandeau."""
    code = dict(LEAGUES).get(league, "")
    country, lid = LEAGUE_INFO.get(code, ("", None))
    return dict(country=country, code=code, league_logo=LEAGUE_LOGO.format(lid) if lid else "",
                logo_h=(H.get("team") or {}).get("logo", ""), logo_a=(A.get("team") or {}).get("logo", ""))


# ---------------------------------------------------------------- NHL

def nhl_get(path):
    return get_json(f"{NHL_API}{path}")


def toi_minutes(toi):
    try:
        m, s = str(toi).split(":")
        return int(m) + int(s) / 60
    except ValueError:
        return 0.0


def last_lineup(abbr):
    """Joueurs alignés lors du dernier match de l'équipe (sert à écarter les blessés) et gardien titulaire."""
    sched = nhl_get(f"/club-schedule-season/{abbr}/now") or {}
    done = [g for g in sched.get("games", []) if g.get("gameState") in ("OFF", "FINAL")]
    if not done:
        return None
    game = max(done, key=lambda g: g.get("startTimeUTC", ""))
    box = nhl_get(f"/gamecenter/{game['id']}/boxscore") or {}
    side = "homeTeam" if (box.get("homeTeam") or {}).get("abbrev") == abbr else "awayTeam"
    stats = (box.get("playerByGameStats") or {}).get(side) or {}
    skaters = {p["playerId"] for grp in ("forwards", "defense") for p in stats.get(grp, [])}
    goalies = [p for p in stats.get("goalies", []) if toi_minutes(p.get("toi")) > 0]
    starter = next((p for p in goalies if p.get("starter")), None) or \
        max(goalies, key=lambda p: toi_minutes(p.get("toi")), default=None)
    # deux gardiens ayant joué au moins 10 minutes : le titulaire a été remplacé (blessure ou contre-performance)
    relieved = None
    if len([p for p in goalies if toi_minutes(p.get("toi")) >= 10]) >= 2 and starter and starter.get("starter"):
        relieved = starter["playerId"]
    return dict(skaters=skaters, starter=(starter or {}).get("playerId"), relieved=relieved,
                goalies_dressed={p["playerId"] for p in stats.get("goalies", [])},
                date=game.get("gameDate"))


def team_profile(abbr, season):
    cur = nhl_get(f"/club-stats/{abbr}/{season}/2") or {}
    prev = nhl_get(f"/club-stats/{abbr}/{season - 10001}/2") or {}
    prev_by_id = {p["playerId"]: p for p in prev.get("skaters", [])}
    last = last_lineup(abbr)
    dressed = last["skaters"] if last and last["skaters"] else None
    try:
        recent = nhl_recent(abbr)
    except Exception:
        recent = None
    rec = (recent or {}).get("players") or {}
    skaters = []
    for p in cur.get("skaters", []):
        if dressed is not None and p["playerId"] not in dressed:
            continue
        q = prev_by_id.get(p["playerId"], {})
        gp = p.get("gamesPlayed", 0) + q.get("gamesPlayed", 0)
        g = p.get("goals", 0) + q.get("goals", 0)
        a = p.get("assists", 0) + q.get("assists", 0)
        cg, ca, cgp = p.get("goals", 0), p.get("assists", 0), p.get("gamesPlayed", 0)
        # forme : les 10 derniers matchs de l'équipe comptent une deuxième fois
        rgp, rg, ra = rec.get(str(p["playerId"]), [0, 0, 0])
        skaters.append(dict(
            name=f"{p['firstName']['default']} {p['lastName']['default']}",
            pos=POSITIONS.get(p.get("positionCode"), ""),
            cur=f"{plural(cg, 'but')}, {plural(ca, 'passe')} en {cgp}", goals=cg, assists=ca, gp=cgp, pid=p["playerId"],
            gpg=(g + rg + 0.10 * 20) / (gp + rgp + 20), ppg=(g + a + rg + ra + 0.35 * 20) / (gp + rgp + 20),
            apg=(a + ra + 0.20 * 20) / (gp + rgp + 20), new=not q,
            l10=dict(n=recent["games"], gp=rgp, g=rg, a=ra) if recent else None))
    return dict(skaters=skaters, goalies=cur.get("goalies", []),
                prev_goals=sum(p.get("goals", 0) for p in prev.get("skaters", [])),
                has_prev=bool(prev.get("skaters")), last=last)


def projected_goalie(profile, back_to_back):
    goalies = [g for g in profile["goalies"] if g.get("gamesStarted", 0) > 0] or profile["goalies"]
    if not goalies:
        return None
    # un gardien absent de la feuille du dernier match est probablement blessé
    dressed = (profile["last"] or {}).get("goalies_dressed") or set()
    pool = [g for g in goalies if g["playerId"] in dressed] or goalies
    goalies = pool
    relieved = (profile["last"] or {}).get("relieved")
    note_relief = ""
    if relieved:
        rest = [g for g in goalies if g["playerId"] != relieved]
        if rest:
            goalies, note_relief = rest, "le titulaire a été remplacé en cours de match la dernière fois (blessure possible)"
    starter = (profile["last"] or {}).get("starter")
    pool = goalies
    if back_to_back and starter:
        pool = [g for g in goalies if g["playerId"] != starter] or goalies
    best = max(pool, key=lambda g: (g.get("gamesStarted", 0), g.get("savePercentage") or 0))
    sv = best.get("savePercentage")
    return dict(name=f"{best['firstName']['default']} {best['lastName']['default']}",
                starts=best.get("gamesStarted", 0),
                sv=(f"{sv * 100:.1f}".replace(".", ",") + " % d'arrêts") if sv else "pas encore joué",
                note=note_relief or ("le titulaire d'hier devrait souffler" if back_to_back and starter else ""))


def streak_text(st):
    code, n = st.get("streakCode"), st.get("streakCount") or 0
    if not code or not n:
        return ""
    if code == "W":
        return f"{plural(n, 'victoire')} de suite"
    if code == "L":
        return f"{plural(n, 'défaite')} de suite"
    return f"{plural(n, 'défaite')} en prolongation de suite"


def nhl(today, history):
    # 1. résultats des jours précédents
    yesterday_teams = set()
    for back in range(1, DAYS_BACK + 2):
        day = today - dt.timedelta(days=back)
        games = (nhl_get(f"/score/{day.isoformat()}") or {}).get("games", [])
        for g in games:
            try:
                if back == 1:
                    yesterday_teams |= {g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"]}
                resolve_nhl(g, history)
            except (KeyError, TypeError, ValueError) as exc:
                print(f"NHL, résultat illisible ({exc!r})", file=sys.stderr)
    # 2. matchs du jour
    sched = nhl_get(f"/schedule/{today.isoformat()}")
    if sched is None:
        note("NHL : l'API officielle ne répond pas. Aucune donnée NHL aujourd'hui.")
        return []
    day = next((d for d in sched.get("gameWeek", []) if d.get("date") == today.isoformat()), None)
    games = (day or {}).get("games", [])
    if not games:
        return []
    standings = {(s.get("teamAbbrev") or {}).get("default"): s
                 for s in (nhl_get("/standings/now") or {}).get("standings", [])}
    espn = espn_events("hockey", "nhl", today) or []
    if not espn:
        note("NHL : ESPN indisponible, l'analyse tourne sans le garde-fou du marché des paris.")
    rows = []
    for g in games:
        try:
            rows.append(nhl_game(g, standings, espn, yesterday_teams, history))
        except Exception as exc:
            note(f"NHL {g['awayTeam']['abbrev']} contre {g['homeTeam']['abbrev']} : "
                 f"erreur inattendue ({exc.__class__.__name__}).")
    rows.sort(key=lambda r: r["kickoff"])
    return rows


def resolve_nhl(g, history):
    key = f"nhl-{g['id']}"
    if key not in history or history[key].get("result") is not None:
        return
    if g.get("gameState") not in ("OFF", "FINAL"):
        return
    hs, as_ = g["homeTeam"].get("score"), g["awayTeam"].get("score")
    if hs is None or as_ is None:
        return
    period = (g.get("gameOutcome") or {}).get("lastPeriodType", "REG")
    suffix = {"OT": " (prol.)", "SO": " (tirs au but)"}.get(period, "")
    history[key]["score"] = f"{hs}-{as_}{suffix}"
    history[key]["result"] = 0 if hs > as_ else 1


def nhl_game(g, standings, espn, yesterday_teams, history):
    season = int(g["season"])
    H, A = g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"]
    sh, sa = standings.get(H, {}), standings.get(A, {})
    hname = (sh.get("teamName") or {}).get("default") or H
    aname = (sa.get("teamName") or {}).get("default") or A
    start = parse_time(g["startTimeUTC"])

    odds, espn_id = None, None
    for e in espn:
        comp, eh, ea = competitors(e)
        if similarity(hname, eh["team"].get("displayName", "")) >= 0.8 and \
                similarity(aname, ea["team"].get("displayName", "")) >= 0.8:
            odds = espn_odds(comp, three_way=False)
            espn_id = str(e.get("id") or "")      # sert au site pour suivre le score en direct
            break
    total = NHL_TOTAL
    started = g.get("gameState", "FUT") not in ("FUT", "PRE")
    saved = history.get(f"nhl-{g['id']}", {})
    b2b_h, b2b_a = H in yesterday_teams, A in yesterday_teams
    model = None
    if not started:
        try:
            model = nhl_model(H, A, season, b2b_h, b2b_a)
        except Exception:
            model = None
    lam = None
    if started and saved.get("p"):
        # match commencé : on garde l'analyse d'avant-match
        odds = None
        win_h = saved["p"][0]
        lam = saved.get("lam")
        source = saved.get("source", "Analyse d'avant-match")
    elif started:
        odds = None
    SECOND_STATS["nhl"][0] += bool(model)
    if started and saved.get("p"):
        pass
    elif model:
        lam = model
        h_, d_, a_ = outcome_probs(lam[0], lam[1], n=15)
        win_h = h_ + d_ * 0.52                    # prolongation et tirs au but : presque un pile ou face
        source = SRC_NHL
    elif odds:
        win_h = odds["p"][0]
        source = "Marché des paris (l'analyse maison manque de données pour ce match)"
        try:
            ov = odds.get("over")
            if ov:      # total de buts qui donne au « plus » la même chance que les cotes du bookmaker
                total = brentq(lambda t: p_over(t, ov["line"]) - ov["p"], 2.0, 12.0)
            elif odds.get("total"):
                total = max(4.5, float(odds["total"]) - 0.2)
        except (TypeError, ValueError):
            pass
    else:
        ph_, pa_ = sh.get("pointPctg") or 0.5, sa.get("pointPctg") or 0.5
        win_h = min(0.75, max(0.25, 0.5 + (ph_ - pa_) * 0.9 + 0.04))
        source = "Modèle maison fondé sur le classement (pas de cotes disponibles)"
    win_a = 1 - win_h
    if lam:
        lh, la = lam
        p_h, p_d, p_a = outcome_probs(lh, la, n=15)
        total = lh + la
    else:
        p_h, p_d, p_a, lh, la = nhl_split(win_h, total)
    scores, scorep = top_scores(lh, la, n=10, exclude_draws=True)

    prof_h, prof_a = team_profile(H, season), team_profile(A, season)

    def ref(st, prof):
        gf, gp = st.get("goalFor", 0), st.get("gamesPlayed", 0)
        if prof["has_prev"]:
            return (gf + prof["prev_goals"]) / (gp + 82)
        return gf / gp if gp else total / 2

    team_rows = {}
    for side, prof, lam, st, name in (("h", prof_h, lh, sh, hname), ("a", prof_a, la, sa, aname)):
        k = lam / max(ref(st, prof), 1.5)
        rows = []
        for p in prof["skaters"]:
            rows.append(dict(name=p["name"], pos=p["pos"], cur=p["cur"], new=p["new"], side=side, team=name,
                             goals=p["goals"], assists=p["assists"], gp=p["gp"], pid=p["pid"],
                             l10=p.get("l10"),
                             p_goal=pct(1 - math.exp(-p["gpg"] * k)),
                             p_point=pct(1 - math.exp(-p["ppg"] * k)),
                             p_assist=pct(1 - math.exp(-p["apg"] * k))))
        team_rows[side] = sorted(rows, key=lambda r: (-r["p_goal"], -r["p_point"]))[:SQUAD_SIZE]
        # récapitulatif de la soirée : les 3 joueurs les plus susceptibles de marquer ou de faire une passe
        team_rows["best_" + side] = sorted(rows, key=lambda r: (-r["p_point"], -r["p_goal"]))[:3]

    state = g.get("gameState", "FUT")
    key = f"nhl-{g['id']}"
    book = (dict(h=odds["dec"][0], a=odds["dec"][1], by=odds["provider"], total=odds.get("total"))
            if odds else history.get(key, {}).get("odds"))
    if state in ("FUT", "PRE"):
        old = history.get(key, {})
        history[key] = dict(sport="nhl", league="NHL", date=paris(start).date().isoformat(),
                            home=hname, away=aname, p=[round(win_h, 4), round(win_a, 4)],
                            lam=[round(lh, 3), round(la, 3)], source=source, result=old.get("result"), score=old.get("score"))
        if odds:          # marché des paris : garde-fou invisible et point de comparaison du Bilan
            history[key]["p_mkt"] = [round(odds["p"][0], 4), round(1 - odds["p"][0], 4)]
        if book:
            history[key]["odds"] = book

    def record(st):
        if not st:
            return ""
        txt = f"{st.get('wins', 0)}-{st.get('losses', 0)}-{st.get('otLosses', 0)} · " \
              f"{st.get('divisionSequence', '?')}e {st.get('divisionName', '')}"
        streak = streak_text(st)
        return txt + (f" · {streak}" if streak else "")

    ctx = []
    for name, st, b2b in ((hname, sh, b2b_h), (aname, sa, b2b_a)):
        if st and st.get("gamesPlayed"):
            ctx.append(f"{name} : {st.get('goalFor', 0)} buts marqués et {st.get('goalAgainst', 0)} encaissés "
                       f"en {plural(st['gamesPlayed'], 'match', 'matchs')}.")
        if b2b:
            ctx.append(f"{name} a joué hier soir : deuxième match en deux soirs, fatigue probable.")
    if not b2b_h and not b2b_a:
        ctx.append("Aucune des deux équipes n'a joué hier.")
    ctx.append("Blessés : non vérifiés automatiquement. Seuls les joueurs alignés au dernier match sont retenus.")

    fav, pf = (hname, win_h) if win_h >= win_a else (aname, win_a)
    if pf >= 0.65:
        reading = f"{fav} est nettement favori ({pct(pf)} % en comptant la prolongation)."
    elif pf >= 0.55:
        reading = f"{fav} est favori ({pct(pf)} % en comptant la prolongation)."
    else:
        reading = "Match très équilibré : au hockey, c'est presque un pile ou face."

    picks = nhl_picks(hname, aname, win_h, lh, la, team_rows,
                      q=min(0.75, max(0.25, (win_h - p_h) / p_d)) if p_d > 0 else 0.5)
    if state in ("FUT", "PRE"):
        why = nhl_why(hname, aname, sh, sa)
        if key in history:
            history[key]["why"] = why
    else:
        why = history.get(key, {}).get("why")
    if why and picks.get("main") and not picks["main"].get("why"):
        picks["main"]["why"] = why
    mkt = history.get(key, {}).get("p_mkt")
    second = dict(src="marché des paris", winH=mkt[0]) if mkt and source == SRC_NHL else None
    agree = concord("nhl", picks, (win_h,), second)

    live = state in ("LIVE", "CRIT", "OFF", "FINAL")
    return dict(
        id=key, sport="nhl", league="NHL", kickoff=start.isoformat(), time=paris(start).strftime("%H:%M"),
        state=state, score=(f"{g['homeTeam'].get('score', 0)}-{g['awayTeam'].get('score', 0)}" if live else None),
        home=hname, away=aname, rec_h=record(sh), rec_a=record(sa),
        winH=pct(win_h), winA=pct(win_a), pH=pct(p_h), pD=pct(p_d), pA=pct(p_a), source=source,
        xgH=round(lh, 2), xgA=round(la, 2), total=round(total, 1), scores=scores, scorep=scorep,
        goalie_h=projected_goalie(prof_h, b2b_h), goalie_a=projected_goalie(prof_a, b2b_a),
        squad_h=team_rows["h"], squad_a=team_rows["a"],
        ctx=ctx, reading=reading, stakes=nhl_stakes(hname, aname, sh, sa),
        espn_id=espn_id, picks=picks, second=agree,
        logo_h=g["homeTeam"].get("logo") or NHL_LOGO.format(H, "light"),
        logo_a=g["awayTeam"].get("logo") or NHL_LOGO.format(A, "light"),
        logo_h_dark=g["homeTeam"].get("darkLogo") or NHL_LOGO.format(H, "dark"),
        logo_a_dark=g["awayTeam"].get("darkLogo") or NHL_LOGO.format(A, "dark"))


# ---------------------------------------------------------------- bilan

def bilan(history):
    done = [h for h in history.values() if h.get("result") is not None and h.get("p")]
    out = {}
    for sport in ("foot", "nhl"):
        rows = [h for h in done if h["sport"] == sport]
        if not rows:
            out[sport] = dict(n=0)
            continue
        hits, brier, bins = 0, 0.0, {}
        for h in rows:
            p, r = h["p"], h["result"]
            fav = max(range(len(p)), key=lambda i: p[i])
            hits += fav == r
            brier += sum((p[i] - (1 if i == r else 0)) ** 2 for i in range(len(p)))
            lo = min(int(p[fav] * 10), 9) * 10
            b = bins.setdefault(lo, [0, 0, 0.0])
            b[0] += 1
            b[1] += fav == r
            b[2] += p[fav]
        n = len(rows)
        # même note d'erreur pour le marché des paris, sur les matchs où il était connu
        both = [h for h in rows if h.get("p_mkt") and len(h["p_mkt"]) == len(h["p"])]
        bm = lambda key: sum(sum((h[key][i] - (1 if i == h["result"] else 0)) ** 2 for i in range(len(h[key]))) for h in both) / len(both)
        out[sport] = dict(
            n=n, fav_rate=pct(hits / n), fav_expected=pct(sum(max(h["p"]) for h in rows) / n),
            brier=round(brier / n, 3), brier_naive=0.667 if sport == "foot" else 0.5,
            vs_mkt=dict(n=len(both), model=round(bm("p"), 3), mkt=round(bm("p_mkt"), 3)) if both else None,
            bins=[dict(range=f"{lo}-{lo + 10} %", n=v[0], announced=pct(v[2] / v[0]), actual=pct(v[1] / v[0]))
                  for lo, v in sorted(bins.items())])
    return out


# ---------------------------------------------------------------- programme principal

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


LIVE_KEYS = ("id", "sport", "league", "code", "kickoff", "time", "home", "away", "state",
             "xgH", "xgA", "pH", "pD", "pA", "winH", "espn_id")


def live_feed(now, foot_rows, nhl_rows, store, today, nhl_day):
    """Version compacte des analyses pour le robot d'alertes (quelques dizaines de Ko au lieu de data.json)."""
    def compact(m, day):
        row = {k: m[k] for k in LIVE_KEYS if m.get(k) is not None}
        row["edate"] = day.strftime("%Y%m%d")         # date utilisée par le tableau des scores ESPN
        row["picks"] = {k: v for k, v in (m.get("picks") or {}).items()
                        if k in ("main", "winner", "player", "decisive", "scorer", "fun", "score") and isinstance(v, dict) and v.get("market")}
        return row

    days = {today.isoformat(), (today - dt.timedelta(days=1)).isoformat()}
    done = [p for p in store.values() if p.get("result") is not None and p.get("date") in days]
    top = selection(foot_rows + nhl_rows, n=3)
    return dict(
        generated=now.isoformat(timespec="minutes"), date=today.isoformat(),
        matches=[compact(m, today) for m in foot_rows] + [compact(m, nhl_day) for m in nhl_rows],
        summary=dict(date=today.isoformat(), yesterday=dict(n=len(done), ok=sum(bool(p["result"]) for p in done)),
                     foot=len(foot_rows), nhl=len(nhl_rows),
                     top=[dict(label=c["label"], prob=c["prob"], time=c["time"], match=f"{c['home']} – {c['away']}")
                          for c in top]))


def main(now=None):
    now = now or dt.datetime.now(PARIS)
    CLOCK[0] = now
    today = now.date()
    # la soirée NHL (matchs entre 0 h et 5 h à Paris) appartient au jour américain de la veille
    nhl_day = (now - dt.timedelta(hours=7)).date()
    history = load_json(HIST_FILE, {})
    SECOND.clear()
    FETCH_LOG.clear()
    SECOND_STATS.clear()
    SECOND_STATS.update(foot=[0, []], nhl=[0, []])
    prev = load_json(DATA_FILE, {})          # passage précédent : repli si une étape échoue
    load_squad_cache(today)
    load_form()
    load_elo()
    same = lambda k, day: prev.get(k) or [] if prev.get("date" if k == "foot" else "nhl_date") == day.isoformat() else []
    foot_rows = step("Foot", lambda: foot(today, history), same("foot", today))
    step("Classement maison (sauvegarde)", lambda: save_elo(today))
    step("Effectifs (sauvegarde)", lambda: save_squad_cache(today))
    nhl_rows = step("NHL", lambda: nhl(nhl_day, history), same("nhl", nhl_day))
    step("Forme (sauvegarde)", lambda: save_form(today))
    cutoff = (today - dt.timedelta(days=400)).isoformat()
    history = {k: v for k, v in history.items() if v.get("date", "") >= cutoff}
    # pronostics : archivage avant match, puis vérification une fois le résultat connu
    store = load_json(PICKS_FILE, {})
    step("Archivage des pronostics", lambda: record_picks(foot_rows + nhl_rows, store))
    step("Vérification des pronostics", lambda: resolve_picks(store, history))
    store = {k: v for k, v in store.items() if v.get("date", "") >= cutoff}
    stats = step("Bilan", lambda: bilan(history), None)
    if stats is None:
        stats = {k: v for k, v in (prev.get("bilan") or {}).items() if k != "picks"}
    stats["picks"] = step("Bilan des pronostics", lambda: picks_bilan(store, history), (prev.get("bilan") or {}).get("picks"))
    data = dict(generated=now.isoformat(timespec="minutes"), date=today.isoformat(), nhl_date=nhl_day.isoformat(),
                foot=foot_rows, nhl=nhl_rows, bilan=stats,
                notes=NOTES[-12:], form=dict(FORM_STATS), second=dict(
                    foot=dict(ok=SECOND_STATS["foot"][0], elo=dict(ELO_STATS), elo_fetch=dict(ELO_DIAG),
                              status="ok" if any(v >= ELO_MIN for v in ELO_STATS.values()) or not ELO_STATS
                              else "Classement maison indisponible (résultats ESPN illisibles)"),
                    nhl=dict(ok=SECOND_STATS["nhl"][0], missing=sorted(set(SECOND_STATS["nhl"][1])),
                             fetch={u: v for u, v in FETCH_LOG.items() if "moneypuck" in u})))
    save_json(HIST_FILE, history)
    save_json(PICKS_FILE, store)
    save_json(DATA_FILE, data)
    feed = step("Alertes (live.json)", lambda: live_feed(now, foot_rows, nhl_rows, store, today, nhl_day))
    if feed is not None:
        save_json(LIVE_FILE, feed)
    print(f"OK : {len(foot_rows)} matchs de foot, {len(nhl_rows)} matchs NHL, "
          f"{sum(1 for h in history.values() if h.get('result') is not None)} résultats archivés.")
    return data


if __name__ == "__main__":
    main()
