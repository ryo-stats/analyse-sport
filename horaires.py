#!/usr/bin/env python3
"""Décide si le passage automatique en cours doit recalculer les analyses.

GitHub lance le workflow toutes les 10 minutes (et le robot Cloudflare en secours), mais les analyses
ne sont recalculées qu'aux heures de Paris listées dans PASSAGES : environ 30 à 40 minutes avant
chaque vague de coups d'envoi. Raisonner en heure de Paris évite d'avoir à modifier les horaires au
changement d'heure.

Avant chaque coup d'envoi, recalcul supplémentaire pour suivre les compositions officielles (voir should_run).

Usage : python horaires.py  →  écrit « run=true » ou « run=false » (format GitHub Actions).
"""
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
PASSAGES = {
    "07:20",   # résultats de la nuit (NHL) et analyses du jour
    "12:20",   # avant les matchs de 13 h - 13 h 30
    "14:20",   # avant les matchs de 15 h - 16 h
    "16:20",   # avant les matchs de 16 h 30 - 17 h 30
    "17:50",   # avant 18 h 30, 18 h 45 (Ligue des champions) et 19 h
    "20:20",   # avant 20 h 45 - 21 h
    "23:50",   # résultats du soir, cotes et gardiens NHL avant les matchs de la nuit
}


def last_passage(now):
    """Dernier horaire prévu déjà atteint (aujourd'hui ou hier soir), en heure de Paris."""
    now = now.astimezone(PARIS)
    times = []
    for back in (0, 1):
        day = (now - dt.timedelta(days=back)).date()
        for p in PASSAGES:
            h, m = map(int, p.split(":"))
            t = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=PARIS)
            if t <= now:
                times.append(t)
    return max(times)


BEFORE_KICKOFF = (dt.timedelta(minutes=5), dt.timedelta(minutes=75))   # compos officielles : ~1 h avant
REFRESH = dt.timedelta(minutes=20)


def should_run(now, event, last=None, kickoffs=()):
    """GitHub lance les passages avec du retard, et en saute parfois. On ne se fie donc pas au créneau
    d'origine : on recalcule dès qu'un horaire prévu est passé depuis la dernière publication (last = champ
    « generated » de docs/data.json). Un passage raté est rattrapé au lancement suivant.
    En plus, avant chaque coup d'envoi (de 75 à 5 min avant), on recalcule toutes les 20 min pour suivre les
    compositions officielles et les blessés de dernière minute.
    event : « schedule » (lancement programmé ou relance du robot) ; autre chose = lancement à la main."""
    if event != "schedule":           # lancement à la main : toujours
        return True
    if last is None or last < last_passage(now):
        return True
    soon = any(BEFORE_KICKOFF[0] <= k - now <= BEFORE_KICKOFF[1] for k in kickoffs)
    return soon and now - last >= REFRESH


def _data(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def published(path="docs/data.json"):
    try:
        return dt.datetime.fromisoformat(_data(path)["generated"])
    except (ValueError, KeyError, TypeError):
        return None


def kickoffs(path="docs/data.json"):
    d, out = _data(path), []
    for m in (d.get("foot") or []) + (d.get("nhl") or []):
        try:
            out.append(dt.datetime.fromisoformat(m["kickoff"]))
        except (ValueError, KeyError, TypeError):
            pass
    return out


if __name__ == "__main__":
    event = os.environ.get("PASSAGE") or os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    run = should_run(dt.datetime.now(PARIS), event, published(), kickoffs())
    line = f"run={'true' if run else 'false'}"
    print(line)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    sys.exit(0)
