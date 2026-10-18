#!/usr/bin/env python3
"""Décide si le passage automatique en cours doit recalculer les analyses.

GitHub lance le workflow toutes les 30 minutes (à :20 et :50 UTC), mais les analyses ne sont
recalculées qu'aux heures de Paris listées dans PASSAGES : environ 30 à 40 minutes avant chaque
vague de coups d'envoi, pour archiver les cotes les plus récentes. Raisonner en heure de Paris
évite d'avoir à modifier les horaires au changement d'heure.

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


def should_run(now, event, last=None):
    """GitHub lance les passages avec 5 à 60 min de retard, et en saute parfois. On ne se fie donc pas au
    créneau d'origine : on recalcule dès qu'un horaire prévu est passé depuis la dernière publication
    (last = champ « generated » de docs/data.json). Un passage raté est rattrapé 30 minutes plus tard."""
    if event != "schedule":           # lancement à la main : toujours
        return True
    return last is None or last < last_passage(now)


def published(path="docs/data.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return dt.datetime.fromisoformat(json.load(f)["generated"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


if __name__ == "__main__":
    run = should_run(dt.datetime.now(PARIS), os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"), published())
    line = f"run={'true' if run else 'false'}"
    print(line)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    sys.exit(0)
