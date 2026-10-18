#!/usr/bin/env python3
"""Analyse sportive automatique : foot (5 grands championnats et Ligue des champions) et NHL.

Sources gratuites, sans clé :
  - API publique d'ESPN : matchs, résultats, classements, effectifs et stats des joueurs, cotes (foot et NHL) ;
  - API officielle de la NHL : calendrier, classement, stats des joueurs, composition du dernier match.

Chaque exécution :
  1. complète les résultats des jours passés dans docs/history.json ;
  2. récupère les matchs du jour et leurs cotes, puis convertit les cotes en probabilités (marge retirée) ;
  3. calcule buts attendus, scores probables, buteurs et joueurs décisifs ;
  4. calcule le bilan : les probabilités annoncées se vérifient-elles ?
  5. écrit docs/data.json, que le site docs/index.html affiche.

Usage : python app.py
"""
import datetime as dt
import difflib
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
from scipy.optimize import brentq, fsolve
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
]
# bandeau de chaque championnat sur le site : pays et logo ESPN
LEAGUE_INFO = {
    "fra.1": ("France", 9), "eng.1": ("Angleterre", 23), "esp.1": ("Espagne", 15),
    "ita.1": ("Italie", 12), "ger.1": ("Allemagne", 10), "uefa.champions": ("Europe", 2),
}
LEAGUE_LOGO = "https://a.espncdn.com/i/leaguelogos/soccer/500/{}.png"
NHL_LOGO = "https://assets.nhle.com/logos/nhl/svg/{}_{}.svg"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer/{league}/standings"
ESPN_LEADERS = "https://site.web.api.espn.com/apis/site/v2/sports/soccer/{league}/statistics"
ESPN_ROSTER = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{team}/roster"
ESPN_PLAYER_STATS = ("https://sports.core.api.espn.com/v2/sports/soccer/leagues/{league}/seasons/{season}"
                     "/types/1/athletes/{athlete}/statistics")
SQUAD_SIZE = 6        # joueurs affichés par équipe
# part a priori des buts (et des buts + passes) de l'équipe selon le poste, pour un joueur qui joue tout
GOAL_PRIOR = {"F": 0.22, "M": 0.08, "D": 0.03}
DEC_PRIOR = {"F": 0.35, "M": 0.25, "D": 0.08}
POSTES = {"F": "Attaquant", "M": "Milieu", "D": "Défenseur"}
NHL_API = "https://api-web.nhle.com/v1"

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


def foot_lambdas(p_home, p_away):
    """Buts attendus de chaque équipe qui reproduisent les probabilités des cotes."""
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
                    total=o.get("overUnder"))
    return None


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
    if league == "Ligue des champions":
        # phase de ligue à 36 : 1-8 qualifiés pour les 8es, 9-24 barrages, 25-36 éliminés
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

    Pour la Ligue des champions, les stats viennent du championnat national du club (plus de matchs,
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
    if league == "uefa.champions":
        refs = [((a.get("defaultLeague") or {}).get("$ref") or "").split("/leagues/")[-1].split("?")[0].split("/")[0]
                for a in athletes]
        counts = Counter(r for r in refs if r and r != "uefa.champions")
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
                            assists=int(s.get("goalAssists") or 0)))
    squad = dict(players=players, league=domestic) if players else None
    SQUADS[(league, team_id)] = squad
    return squad


def squad_rows(squad, lam, side, team):
    """Probabilité de marquer et d'être décisif (but ou passe) pour chaque joueur de champ."""
    players = squad["players"]
    team_games = max(p["apps"] for p in players)
    team_goals = max(sum(p["goals"] for p in players), 1)
    k = 8          # lissage : l'équivalent d'environ 6 matchs de buts « moyens » réparti selon le poste
    rows = []
    for p in players:
        mfrac = min(1.0, p["minutes"] / (90 * team_games)) if p["minutes"] else min(1.0, p["apps"] / team_games) * 0.6
        share_goal = (p["goals"] + k * GOAL_PRIOR[p["pos"]] * mfrac) / (team_goals + k)
        share_dec = min(1.2, (p["goals"] + p["assists"] + k * DEC_PRIOR[p["pos"]] * mfrac) / (team_goals + k))
        rows.append(dict(name=p["name"], pos=POSTES[p["pos"]], side=side, team=team,
                         goals=p["goals"], assists=p["assists"], apps=p["apps"],
                         p_goal=pct(1 - math.exp(-lam * share_goal)),
                         p_dec=pct(1 - math.exp(-lam * share_dec))))
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
        if not all(squads.values()) and not leaders:
            leaders = espn_leaders(code)          # repli : top 50 des buteurs et passeurs du championnat
            if not leaders:
                note(f"{league} : joueurs indisponibles aujourd'hui.")
        rows.append(foot_match(e, league, table, league_gpg or 1.4, leaders, history, squads))
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


def foot_match(e, league, table, league_gpg, leaders, history, squads=None):
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
    if state != "pre" and saved and len(saved) == 3:
        # après le coup d'envoi, ESPN affiche des cotes en direct : on garde celles d'avant-match
        p_h, p_d, p_a = saved
        lh, la = foot_lambdas(p_h, p_a)
        source = history[key].get("source", "Cotes d'avant-match")
    elif odds and state == "pre":
        p_h, p_d, p_a = odds["p"]
        lh, la = foot_lambdas(p_h, p_a)
        source = f"Cotes {odds['provider']}"
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

    squads = squads or {}
    team_rows, stats_from, thin = {}, set(), []
    for side, tid, st, lam, name in (("h", hid, sh, lh, home), ("a", aid, sa, la, away)):
        if squads.get(side):
            team_rows[side] = squad_rows(squads[side], lam, side, name)
            stats_from.add(squads[side]["league"])
            if max(p["apps"] for p in squads[side]["players"]) < 3:
                thin.append(name)
        else:
            rows = foot_players(leaders.get(tid, []), max((st or {}).get("gf", 0), 1), lam)
            for r in rows:
                r.update(side=side, team=name)
            team_rows[side] = sorted(rows, key=lambda r: -r["p_goal"])[:SQUAD_SIZE]

    # cotes du bookmaker : lues avant le match, archivées pour rester affichées après le coup d'envoi
    book = (dict(h=odds["dec"][0], d=odds["dec"][1], a=odds["dec"][2], by=odds["provider"])
            if odds and state == "pre" else history.get(key, {}).get("odds"))
    if state == "pre":
        old = history.get(key, {})
        history[key] = dict(sport="foot", league=league, date=paris(kickoff).date().isoformat(),
                            home=home, away=away, p=[round(p_h, 4), round(p_d, 4), round(p_a, 4)],
                            source=source, result=old.get("result"), score=old.get("score"))
        if book:
            history[key]["odds"] = book

    ctx = []
    for name, st in ((home, sh), (away, sa)):
        if st:
            ctx.append(f"{name} : {st['rank']}e, {plural(st['pts'], 'point')}, "
                       f"{plural(st['gf'], 'but')} marqué{'s' if st['gf'] > 1 else ''} et "
                       f"{st['ga']} encaissé{'s' if st['ga'] > 1 else ''} en {plural(st['gp'], 'match', 'matchs')}.")
    if not team_rows["h"] and not team_rows["a"]:
        ctx.append("Statistiques des joueurs indisponibles pour ce match.")
    for name in thin:
        ctx.append(f"{name} : ESPN ne connaît que 1 ou 2 matchs de ses joueurs, chiffres des joueurs très incertains.")
    if league == "Ligue des champions" and stats_from - {"uefa.champions"}:
        ctx.append("Joueurs : statistiques de la saison en championnat national (plus de matchs que la Ligue des champions).")

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
        pH=pct(p_h), pD=pct(p_d), pA=pct(p_a), source=source, odds=book,
        xgH=round(lh, 2), xgA=round(la, 2), scores=scores, scorep=scorep,
        over25=pct(over25), btts=pct(btts), squad_h=team_rows["h"], squad_a=team_rows["a"],
        ctx=ctx, reading=reading, stakes=stakes, picks=foot_picks(home, away, p_h, p_d, p_a, over25, btts, team_rows, scores, scorep, lh, la),
        **foot_visuals(league, H, A))


# ---------------------------------------------------------------- pronostics

STAR_STEPS = (0.5, 0.6, 0.7, 0.8)      # 1 étoile sous 50 %, 5 étoiles à partir de 80 %


def make_pick(market, label, p, **extra):
    p = float(p)          # les calculs scipy renvoient des types numpy, refusés par json
    return dict(market=market, label=label, p=round(p, 4), prob=int(pct(p)),
                fair=round(1 / max(p, 0.01), 2),          # « cote juste » : 1 / probabilité, sans marge de bookmaker
                stars=1 + sum(int(p >= s) for s in STAR_STEPS), **extra)


FUN_LO, FUN_HI = 0.35, 0.66          # pari « fun » : plus risqué que le plus sûr, mais plausible


def fun_pick(sport, home, away, lh, la, q, exclude=()):
    """Pari « fun » d'avant-match : un pari entre 35 et 66 %, un par famille, le plus probable d'abord,
    jamais le même que le pronostic le plus sûr ni que le vainqueur annoncé. Même logique que funPicks dans docs/live.js."""
    n = 11
    ph = [poisson.pmf(i, lh) for i in range(n)]
    pa = [poisson.pmf(j, la) for j in range(n)]
    z = sum(ph) * sum(pa)

    def P(f):
        return sum(ph[i] * pa[j] * f(i, j) for i in range(n) for j in range(n)) / z
    c = []
    if sport == "foot":
        c += [("res", "1", f"Victoire {home}", P(lambda h, a: h > a)), ("res", "2", f"Victoire {away}", P(lambda h, a: a > h)),
              ("res", "X", "Match nul", P(lambda h, a: h == a)),
              ("dc", "1N", f"{home} ou nul", P(lambda h, a: h >= a)), ("dc", "N2", f"Nul ou {away}", P(lambda h, a: a >= h))]
        c += [("ou", f"o{k}5", f"Plus de {k},5 buts", P(lambda h, a, k=k: h + a > k)) for k in (0, 1, 2)]
        c += [("under", f"u{k}5", f"Moins de {k},5 buts", P(lambda h, a, k=k: h + a <= k)) for k in (1, 2, 3)]
        c += [("btts", "btts_y", "Les deux équipes marquent", P(lambda h, a: h > 0 and a > 0)),
              ("margin", "h2", f"{home} gagne avec 2 buts d'écart ou plus", P(lambda h, a: h - a >= 2)),
              ("margin", "a2", f"{away} gagne avec 2 buts d'écart ou plus", P(lambda h, a: a - h >= 2)),
              ("cs", "cs_h", f"{away} ne marque pas", P(lambda h, a: a == 0)),
              ("cs", "cs_a", f"{home} ne marque pas", P(lambda h, a: h == 0))]
        more = 1 - ph[0] * pa[0]
        if lh + la > 0:
            c += [("next", "fg_h", f"Premier but : {home}", more * lh / (lh + la)),
                  ("next", "fg_a", f"Premier but : {away}", more * la / (lh + la))]
    else:
        win_h = P(lambda h, a: 1 if h > a else (q if h == a else 0))
        c += [("res", "win_h", f"Victoire {home}", win_h),
              ("res", "win_a", f"Victoire {away}", 1 - win_h),
              ("reg", "reg_h", f"{home} gagne dans le temps réglementaire", P(lambda h, a: h > a)),
              ("reg", "reg_a", f"{away} gagne dans le temps réglementaire", P(lambda h, a: a > h)),
              ("ot", "ot", "Il y aura prolongation", P(lambda h, a: h == a))]
        # une égalité est suivie d'un but de prolongation (ou tirs au but) : il compte dans le total
        c += [("ou", f"o{k}5", f"Plus de {k},5 buts", P(lambda h, a, k=k: h + a + (h == a) > k)) for k in (4, 5, 6)]
        c += [("under", f"u{k}5", f"Moins de {k},5 buts", P(lambda h, a, k=k: h + a + (h == a) <= k)) for k in (5, 6, 7)]
    best = {}
    for fam, market, label, prob in sorted(c, key=lambda x: -x[3]):
        if FUN_LO <= prob <= FUN_HI and fam not in best and label not in exclude:
            best[fam] = (market, label, prob)
    if not best:
        return None
    market, label, prob = max(best.values(), key=lambda x: x[2])
    return make_pick(market, label, prob)


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
    if lh is not None:
        fun = fun_pick("foot", home, away, lh, la, 0.5, {picks["main"]["label"], picks["winner"]["label"]})
        if fun:
            picks["fun"] = fun
    players = (team_rows.get("h") or []) + (team_rows.get("a") or [])
    if players:
        best = max(players, key=lambda r: r["p_goal"])
        picks["player"] = make_pick("scorer", f"{best['name']} marque", best["p_goal"] / 100,
                                    player=best["name"], team=best["team"])
    return picks


def nhl_picks(hname, aname, win_h, lh, la, team_rows, q=0.5):
    total = lh + la
    over55 = 1 - sum(poisson.pmf(k, total) for k in range(6))     # les égalités sont paires : la prolongation ne change rien
    team = [make_pick("win_h", f"Victoire {hname}", win_h), make_pick("win_a", f"Victoire {aname}", 1 - win_h),
            make_pick("o55", "Plus de 5,5 buts", over55), make_pick("u55", "Moins de 5,5 buts", 1 - over55)]
    picks = dict(main=max(team, key=lambda c: c["p"]), winner=team[0] if win_h >= 0.5 else team[1])
    fun = fun_pick("nhl", hname, aname, lh, la, q, {picks["main"]["label"], picks["winner"]["label"]})
    if fun:
        picks["fun"] = fun
    players = (team_rows.get("best_h") or []) + (team_rows.get("best_a") or [])
    if players:
        best = max(players, key=lambda r: r["p_point"])
        picks["player"] = make_pick("point", f"{best['name']} : au moins 1 point", best["p_point"] / 100,
                                    player=best["name"], pid=best.get("pid"), team=best["team"])
        scorer = max(players, key=lambda r: r["p_goal"])
        picks["scorer"] = make_pick("goal", f"{scorer['name']} marque", scorer["p_goal"] / 100,
                                    player=scorer["name"], pid=scorer.get("pid"), team=scorer["team"])
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
        for kind in ("main", "winner", "player", "scorer", "fun", "score"):
            pk = (m.get("picks") or {}).get(kind)
            if not pk or not pk.get("market"):
                continue
            pid = f"{m['id']}|{pk['market']}|{pk.get('player', '')}"
            old = store.get(pid, {})
            kinds = sorted(set(old.get("kinds", [])) | {kind})
            store[pid] = dict(match=m["id"], sport=m["sport"], league=m["league"], date=m["kickoff"][:10],
                              home=m["home"], away=m["away"], market=pk["market"], label=pk["label"], p=pk["p"],
                              stars=pk["stars"], player=pk.get("player"), pid_player=pk.get("pid"), value=pk.get("value"),
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
                  "h2": gh - ga >= 2, "a2": ga - gh >= 2, "cs_h": ga == 0, "cs_a": gh == 0}.get(mk)
            if mk in ("fg_h", "fg_a"):                   # 0-0 : personne n'a marqué, pari perdu
                ok = False if gh + ga == 0 else (h["first"] == mk[-1] if h.get("first") else None)
            if mk == "scorer" and h.get("scorers") is not None:
                ok = norm(pk["player"]) in {norm(s) for s in h["scorers"]}
        else:
            ot = "prol" in h["score"] or "tirs" in h["score"]
            ok = {"win_h": h["result"] == 0, "win_a": h["result"] == 1, "ot": ot,
                  "reg_h": h["result"] == 0 and not ot, "reg_a": h["result"] == 1 and not ot}.get(mk)
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


def picks_bilan(store):
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
    recent = [p for p in details if pick_group(p) == "safe"][:40]
    return dict(n=len(done), rate=pct(hits / len(done)), expected=pct(sum(p["p"] for p in done) / len(done)),
                since=dates[-1], total=by_sport(done), fun=by_sport(fun_done), days=days, leagues=leagues,
                by_stars=by_stars,
                details=[dict(date=p["date"], sport=p["sport"], league=league_of(p), mid=p["match"],
                              match=f"{p['home']} – {p['away']}", label=p["label"], prob=pct(p["p"]),
                              stars=p["stars"], ok=bool(p["result"]), fun=pick_group(p) == "fun") for p in details],
                recent=[dict(date=p["date"], sport=p["sport"], match=f"{p['home']} – {p['away']}", label=p["label"],
                             prob=pct(p["p"]), stars=p["stars"], ok=p["result"]) for p in recent])


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
    skaters = []
    for p in cur.get("skaters", []):
        if dressed is not None and p["playerId"] not in dressed:
            continue
        q = prev_by_id.get(p["playerId"], {})
        gp = p.get("gamesPlayed", 0) + q.get("gamesPlayed", 0)
        g = p.get("goals", 0) + q.get("goals", 0)
        a = p.get("assists", 0) + q.get("assists", 0)
        cg, ca, cgp = p.get("goals", 0), p.get("assists", 0), p.get("gamesPlayed", 0)
        skaters.append(dict(
            name=f"{p['firstName']['default']} {p['lastName']['default']}",
            pos=POSITIONS.get(p.get("positionCode"), ""),
            cur=f"{plural(cg, 'but')}, {plural(ca, 'passe')} en {cgp}", goals=cg, assists=ca, gp=cgp, pid=p["playerId"],
            gpg=(g + 0.10 * 20) / (gp + 20), ppg=(g + a + 0.35 * 20) / (gp + 20),
            apg=(a + 0.20 * 20) / (gp + 20), new=not q))
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
            if back == 1:
                yesterday_teams |= {g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"]}
            resolve_nhl(g, history)
    # 2. matchs du jour
    sched = nhl_get(f"/schedule/{today.isoformat()}")
    if sched is None:
        note("NHL : l'API officielle ne répond pas. Aucune donnée NHL aujourd'hui.")
        return []
    day = next((d for d in sched.get("gameWeek", []) if d.get("date") == today.isoformat()), None)
    games = (day or {}).get("games", [])
    if not games:
        return []
    standings = {s["teamAbbrev"]["default"]: s
                 for s in (nhl_get("/standings/now") or {}).get("standings", [])}
    espn = espn_events("hockey", "nhl", today) or []
    if not espn:
        note("NHL : cotes ESPN indisponibles, probabilités tirées du classement (moins fiables).")
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
    if started and saved.get("p"):
        # match commencé : on garde les probabilités d'avant-match, pas les cotes en direct
        odds = None
        win_h = saved["p"][0]
        source = saved.get("source", "Cotes d'avant-match")
    elif started:
        odds = None
    if started and saved.get("p"):
        pass
    elif odds:
        win_h = odds["p"][0]
        source = f"Cotes {odds['provider']} (vainqueur final)"
        try:
            if odds.get("total"):
                total = max(4.5, float(odds["total"]) - 0.2)
        except (TypeError, ValueError):
            pass
    else:
        ph_, pa_ = sh.get("pointPctg") or 0.5, sa.get("pointPctg") or 0.5
        win_h = min(0.75, max(0.25, 0.5 + (ph_ - pa_) * 0.9 + 0.04))
        source = "Modèle maison fondé sur le classement (pas de cotes disponibles)"
    win_a = 1 - win_h
    p_h, p_d, p_a, lh, la = nhl_split(win_h, total)
    scores, scorep = top_scores(lh, la, n=10, exclude_draws=True)

    b2b_h, b2b_a = H in yesterday_teams, A in yesterday_teams
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
                            source=source, result=old.get("result"), score=old.get("score"))
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

    live = state in ("LIVE", "CRIT", "OFF", "FINAL")
    return dict(
        id=key, sport="nhl", league="NHL", kickoff=start.isoformat(), time=paris(start).strftime("%H:%M"),
        state=state, score=(f"{g['homeTeam'].get('score', 0)}-{g['awayTeam'].get('score', 0)}" if live else None),
        home=hname, away=aname, rec_h=record(sh), rec_a=record(sa),
        winH=pct(win_h), winA=pct(win_a), pH=pct(p_h), pD=pct(p_d), pA=pct(p_a), source=source, odds=book,
        xgH=round(lh, 2), xgA=round(la, 2), total=round(total, 1), scores=scores, scorep=scorep,
        goalie_h=projected_goalie(prof_h, b2b_h), goalie_a=projected_goalie(prof_a, b2b_a),
        squad_h=team_rows["h"], squad_a=team_rows["a"], best_h=team_rows["best_h"], best_a=team_rows["best_a"],
        ctx=ctx, reading=reading, stakes=nhl_stakes(hname, aname, sh, sa),
        espn_id=espn_id, picks=nhl_picks(hname, aname, win_h, lh, la, team_rows,
                                                       q=min(0.75, max(0.25, (win_h - p_h) / p_d)) if p_d > 0 else 0.5),
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
        out[sport] = dict(
            n=n, fav_rate=pct(hits / n), fav_expected=pct(sum(max(h["p"]) for h in rows) / n),
            brier=round(brier / n, 3), brier_naive=0.667 if sport == "foot" else 0.5,
            bins=[dict(range=f"{lo}-{lo + 10} %", n=v[0], announced=pct(v[2] / v[0]), actual=pct(v[1] / v[0]))
                  for lo, v in sorted(bins.items())])
    recent = sorted(done, key=lambda h: h["date"], reverse=True)[:40]
    out["recent"] = []
    for h in recent:
        p = h["p"]
        fav = max(range(len(p)), key=lambda i: p[i])
        labels = [h["home"], "Nul", h["away"]] if len(p) == 3 else [h["home"], h["away"]]
        out["recent"].append(dict(date=h["date"], sport=h["sport"], league=h["league"], home=h["home"],
                                  away=h["away"], score=h.get("score"), fav=labels[fav], favp=pct(p[fav]),
                                  ok=fav == h["result"]))
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
                        if k in ("main", "winner", "player", "scorer", "fun", "score") and isinstance(v, dict) and v.get("market")}
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
    today = now.date()
    # la soirée NHL (matchs entre 0 h et 5 h à Paris) appartient au jour américain de la veille
    nhl_day = (now - dt.timedelta(hours=7)).date()
    history = load_json(HIST_FILE, {})
    load_squad_cache(today)
    foot_rows = foot(today, history)
    save_squad_cache(today)
    nhl_rows = nhl(nhl_day, history)
    cutoff = (today - dt.timedelta(days=400)).isoformat()
    history = {k: v for k, v in history.items() if v.get("date", "") >= cutoff}
    # pronostics : archivage avant match, puis vérification une fois le résultat connu
    store = load_json(PICKS_FILE, {})
    record_picks(foot_rows + nhl_rows, store)
    resolve_picks(store, history)
    store = {k: v for k, v in store.items() if v.get("date", "") >= cutoff}
    stats = bilan(history)
    stats["picks"] = picks_bilan(store)
    data = dict(generated=now.isoformat(timespec="minutes"), date=today.isoformat(), nhl_date=nhl_day.isoformat(),
                foot=foot_rows, nhl=nhl_rows, selection=selection(foot_rows + nhl_rows), bilan=stats,
                notes=NOTES[-12:])
    save_json(HIST_FILE, history)
    save_json(PICKS_FILE, store)
    save_json(DATA_FILE, data)
    save_json(LIVE_FILE, live_feed(now, foot_rows, nhl_rows, store, today, nhl_day))
    print(f"OK : {len(foot_rows)} matchs de foot, {len(nhl_rows)} matchs NHL, "
          f"{sum(1 for h in history.values() if h.get('result') is not None)} résultats archivés.")
    return data


if __name__ == "__main__":
    main()
