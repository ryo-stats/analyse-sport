"""Tests de bout en bout sans réseau : les réponses des API sont simulées.

Lancer : python -m unittest discover -s tests -v
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402

NOW = dt.datetime(2026, 10, 11, 14, 0, tzinfo=app.PARIS)


def skater(pid, first, last, pos, gp, g, a):
    return dict(playerId=pid, firstName={"default": first}, lastName={"default": last},
                positionCode=pos, gamesPlayed=gp, goals=g, assists=a)


def goalie(pid, first, last, gs, sv):
    return dict(playerId=pid, firstName={"default": first}, lastName={"default": last},
                gamesPlayed=gs, gamesStarted=gs, savePercentage=sv)


CLUB = {
    ("WSH", 20262027): dict(skaters=[skater(1, "Tom", "Wilson", "R", 4, 2, 3), skater(2, "Jakob", "Chychrun", "D", 4, 1, 2),
                                     skater(3, "Blessé", "Absent", "C", 2, 1, 0)],
                            goalies=[goalie(10, "Logan", "Thompson", 3, .929), goalie(11, "Charlie", "Lindgren", 1, .931),
                                     goalie(12, "Gardien", "Blessé", 5, .935)]),
    ("WSH", 20252026): dict(skaters=[skater(1, "Tom", "Wilson", "R", 72, 30, 32), skater(2, "Jakob", "Chychrun", "D", 80, 26, 34)]),
    ("SEA", 20262027): dict(skaters=[skater(4, "Jared", "McCann", "L", 5, 1, 5), skater(5, "Mackie", "Samoskevich", "R", 4, 3, 0)],
                            goalies=[goalie(20, "Philipp", "Grubauer", 2, .917), goalie(21, "Joey", "Daccord", 3, .911)]),
    ("SEA", 20252026): dict(skaters=[skater(4, "Jared", "McCann", "L", 52, 20, 20)]),
}


# effectifs ESPN simulés : (id, nom, poste, matchs, minutes, buts, passes) ; Liverpool n'a pas d'effectif
# (ESPN ne répond pas) pour vérifier le repli sur le top des buteurs du championnat
SQUAD = {
    "382": [("m1", "Erling Haaland", "F", 5, 430, 5, 0), ("m2", "Phil Foden", "M", 5, 380, 1, 3),
            ("m3", "Ruben Dias", "D", 5, 450, 0, 0), ("m4", "Rayan Cherki", "M", 3, 120, 0, 1),
            ("m0", "Ederson", "G", 5, 450, 0, 0)],
}


def espn_soccer_event(eid, date, state, completed, home, away, odds=None):
    def comp(side, t):
        return dict(homeAway=side, team=dict(id=t[0], displayName=t[1], shortDisplayName=t[1]),
                    score=t[2], form="WDWWL")
    return dict(id=eid, date=date, status=dict(type=dict(state=state, completed=completed)),
                competitions=[dict(competitors=[comp("home", home), comp("away", away)], odds=odds or [])])


def fake_get_json(url, params=None):
    p = params or {}
    if "soccer/eng.1/scoreboard" in url:
        if p.get("dates") == "20261011":
            odds = [None, dict(provider=dict(name="ESPN BET"), overUnder=2.5,
                               moneyline=dict(home=dict(close=dict(odds="+230")), away=dict(close=dict(odds="+105"))),
                               drawOdds=dict(moneyLine=275))]
            return dict(events=[espn_soccer_event("401", "2026-10-11T15:30Z", "pre", False,
                                                  ("364", "Liverpool", "0"), ("382", "Man City", "0"), odds)])
        if p.get("dates") == "20261010":
            return dict(events=[espn_soccer_event("900", "2026-10-10T14:00Z", "post", True,
                                                  ("359", "Arsenal", "2"), ("363", "Chelsea", "1"))])
        return dict(events=[])
    if "/scoreboard" in url and "soccer" in url:
        return dict(events=[])
    if "soccer/eng.1/standings" in url:
        def entry(tid, rank, gp, gf, ga, pts):
            return dict(team=dict(id=tid), stats=[dict(name="rank", value=rank), dict(name="gamesPlayed", value=gp),
                                                  dict(name="pointsFor", value=gf), dict(name="pointsAgainst", value=ga),
                                                  dict(name="points", value=pts)])
        return dict(children=[dict(standings=dict(entries=[entry("382", 1, 5, 13, 5, 15), entry("364", 7, 5, 7, 4, 9)]))])
    if "soccer/eng.1/statistics" in url:
        def lead(pid, name, tid, val):
            return dict(value=val, athlete=dict(id=pid, displayName=name, team=dict(id=tid)))
        return dict(stats=[dict(name="goalsLeaders", leaders=[lead("a1", "Erling Haaland", "382", 5), lead("a2", "Alexander Isak", "364", 4)]),
                           dict(name="assistsLeaders", leaders=[lead("a3", "Antoine Semenyo", "382", 3)])])
    if "/teams/" in url and url.endswith("/roster"):
        tid = url.split("/teams/")[1].split("/")[0]
        if tid not in SQUAD:
            return None
        return dict(season=dict(year=2026), athletes=[
            dict(id=pid, displayName=name, position=dict(abbreviation=pos),
                 defaultLeague={"$ref": "http://x/v2/sports/soccer/leagues/eng.1?lang=en"})
            for pid, name, pos, *_ in SQUAD[tid]])
    if "sports.core.api.espn.com" in url and "/leagues/eng.1/" in url and url.endswith("/statistics"):
        pid = url.split("/athletes/")[1].split("/")[0]
        for squad in SQUAD.values():
            for p_id, _, _, apps, minutes, g, a in squad:
                if p_id == pid:
                    stats = [dict(name="appearances", value=apps), dict(name="minutes", value=minutes),
                             dict(name="totalGoals", value=g), dict(name="goalAssists", value=a)]
                    return dict(splits=dict(categories=[dict(name="general", stats=stats)]))
        return None
    if "hockey/nhl/scoreboard" in url:
        comp = dict(competitors=[dict(homeAway="home", team=dict(displayName="Washington Capitals")),
                                 dict(homeAway="away", team=dict(displayName="Seattle Kraken"))],
                    odds=[dict(provider=dict(name="ESPN BET"), overUnder=6.5,
                               moneyline=dict(home=dict(close=dict(odds="-218")), away=dict(close=dict(odds="+180"))))])
        return dict(events=[dict(id="e1", date="2026-10-11T21:00Z", competitions=[comp])])
    if url.endswith("/score/2026-10-10"):
        return dict(games=[dict(id=777, gameState="OFF", homeTeam=dict(abbrev="BOS", score=5),
                                awayTeam=dict(abbrev="PHI", score=1), gameOutcome=dict(lastPeriodType="REG")),
                           dict(id=778, gameState="OFF", homeTeam=dict(abbrev="CHI", score=2),
                                awayTeam=dict(abbrev="SEA", score=5), gameOutcome=dict(lastPeriodType="REG"))])
    if "/score/" in url:
        return dict(games=[])
    if url.endswith("/schedule/2026-10-11"):
        return dict(gameWeek=[dict(date="2026-10-11", games=[dict(
            id=2026020084, season=20262027, startTimeUTC="2026-10-11T21:00:00Z", gameState="FUT",
            homeTeam=dict(abbrev="WSH"), awayTeam=dict(abbrev="SEA"))])])
    if url.endswith("/standings/now"):
        def st(abbr, name, gp, w, l, otl, gf, ga, seq, div, code, n):
            return dict(teamAbbrev={"default": abbr}, teamName={"default": name}, gamesPlayed=gp, wins=w, losses=l,
                        otLosses=otl, goalFor=gf, goalAgainst=ga, divisionSequence=seq, divisionName=div,
                        streakCode=code, streakCount=n, pointPctg=(2 * w + otl) / (2 * gp))
        return dict(standings=[st("WSH", "Washington Capitals", 4, 3, 1, 0, 14, 9, 4, "Metropolitan", "W", 2),
                               st("SEA", "Seattle Kraken", 5, 3, 2, 0, 21, 14, 4, "Pacific", "W", 1)])
    if "/club-stats/" in url:
        parts = url.split("/club-stats/")[1].split("/")
        return CLUB.get((parts[0], int(parts[1])), dict(skaters=[], goalies=[]))
    if "/club-schedule-season/" in url:
        abbr = url.split("/club-schedule-season/")[1].split("/")[0]
        gid = 55 if abbr == "WSH" else 56
        return dict(games=[dict(id=gid, gameState="OFF", startTimeUTC="2026-10-10T23:00:00Z", gameDate="2026-10-10")])
    if "/gamecenter/777/boxscore" in url:
        return dict(playerByGameStats=dict(homeTeam=dict(forwards=[dict(playerId=88, goals=0, assists=2)], defense=[]),
                                           awayTeam=dict(forwards=[], defense=[])))
    if "/gamecenter/55/boxscore" in url:
        return dict(homeTeam=dict(abbrev="WSH"), playerByGameStats=dict(homeTeam=dict(
            forwards=[dict(playerId=1)], defense=[dict(playerId=2)],
            goalies=[dict(playerId=10, toi="60:00"), dict(playerId=11, toi="00:00")])))
    if "/gamecenter/56/boxscore" in url:
        return dict(homeTeam=dict(abbrev="CHI"), playerByGameStats=dict(awayTeam=dict(
            forwards=[dict(playerId=4), dict(playerId=5)], defense=[],
            goalies=[dict(playerId=21, toi="59:10"), dict(playerId=20, toi="00:00")])))
    return None


# deuxième avis simulé : ClubElo (probabilités du match Liverpool – Man City, classement Elo) et MoneyPuck
CLUBELO_DAY = "Rank,Club,Country,Level,Elo,From,To\n1,Man City,ENG,1,2010,2026-10-05,2026-10-12\n" \
              "3,Liverpool,ENG,1,1960,2026-10-05,2026-10-12\n9,Paris SG,FRA,1,1900,2026-10-05,2026-10-12\n" \
              "40,Paris FC,FRA,1,1610,2026-10-05,2026-10-12\n25,Forest,ENG,1,1750,2026-10-05,2026-10-12\n"
MP_HEAD = "team,season,name,team,position,situation,games_played,xGoalsFor,xGoalsAgainst,iceTime\n"
MONEYPUCK = {2026: MP_HEAD + "WSH,2026,WSH,WSH,Team Level,all,4,14.0,9.0,14400\nSEA,2026,SEA,SEA,Team Level,all,5,13.0,14.0,18000\n"
                            "WSH,2026,WSH,WSH,Team Level,5on5,4,10.0,7.0,11000\n",
             2025: MP_HEAD + "WSH,2025,WSH,WSH,Team Level,all,82,260,230,295200\nSEA,2025,SEA,SEA,Team Level,all,82,235,250,295200\n"}


def fake_get_text(url, headers=None, tries=3, timeout=30):
    if url.startswith("http://api.clubelo.com/"):
        return CLUBELO_DAY
    if "moneypuck.com" in url:
        return MONEYPUCK.get(int(url.split("/seasonSummary/")[1].split("/")[0]))
    return None


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app.DATA_FILE = os.path.join(self.tmp, "data.json")
        app.HIST_FILE = os.path.join(self.tmp, "history.json")
        app.NOTES.clear()
        app.SQUADS.clear()
        app.CACHE_FILE = os.path.join(self.tmp, "squads.json")
        app.PICKS_FILE = os.path.join(self.tmp, "picks.json")
        app.LIVE_FILE = os.path.join(self.tmp, "live.json")
        app.get_json = fake_get_json
        app.get_text = fake_get_text
        app.time.sleep = lambda s: None
        history = {"foot-900": dict(sport="foot", league="Premier League", date="2026-10-10", home="Arsenal",
                                    away="Chelsea", p=[0.5, 0.27, 0.23], result=None, score=None),
                   "nhl-777": dict(sport="nhl", league="NHL", date="2026-10-10", home="Boston Bruins",
                                   away="Philadelphia Flyers", p=[0.62, 0.38], result=None, score=None)}
        app.save_json(app.HIST_FILE, history)
        self.data = app.main(NOW)

    def test_foot_probabilities_follow_odds(self):
        m = self.data["foot"][0]
        raw = [app.implied("+230"), app.implied(275), app.implied("+105")]
        fair = [x / sum(raw) for x in raw]
        self.assertEqual([m["pH"], m["pD"], m["pA"]], [round(x * 100) for x in fair])
        h, d, a = app.outcome_probs(m["xgH"], m["xgA"])
        self.assertAlmostEqual(h, fair[0], delta=0.01)
        self.assertAlmostEqual(a, fair[2], delta=0.01)
        # cotes du bookmaker en décimal (+230 → 3,30 ; +275 → 3,75 ; +105 → 2,05), archivées pour après le coup d'envoi
        self.assertEqual(m["odds"], dict(h=3.3, d=3.75, a=2.05, by="ESPN BET"))
        self.assertEqual(app.load_json(app.HIST_FILE, {})["foot-401"]["odds"]["a"], 2.05)
        # cote juste d'un pronostic = 1 / probabilité
        pk = m["picks"]["main"]
        self.assertAlmostEqual(pk["fair"], 1 / pk["p"], delta=0.01)
        nhl = self.data["nhl"][0]
        self.assertEqual((nhl["odds"]["h"], nhl["odds"]["a"]), (1.46, 2.8))
        # Man City : effectif complet, gardien exclu, joueurs triés par chance de marquer
        city = m["squad_a"]
        self.assertEqual([p["name"] for p in city][:2], ["Erling Haaland", "Phil Foden"])
        self.assertNotIn("Ederson", [p["name"] for p in city])
        self.assertIn("Ruben Dias", [p["name"] for p in city])
        self.assertTrue(all(0 < p["p_goal"] < 100 and p["p_dec"] >= p["p_goal"] for p in city))
        self.assertEqual(city[0]["pos"], "Attaquant")
        # Liverpool : pas d'effectif ESPN, repli sur le top des buteurs du championnat
        self.assertEqual([p["name"] for p in m["squad_h"]], ["Alexander Isak"])

    def test_heavy_favourite_keeps_realistic_goals(self):
        lh, la = app.foot_lambdas(0.07, 0.83)          # Viking - Bayern : deux solutions possibles
        h, _, a = app.outcome_probs(lh, la)
        self.assertAlmostEqual(h, 0.07, delta=0.002)
        self.assertAlmostEqual(a, 0.83, delta=0.002)
        self.assertLess(lh + la, 5.5)                  # la solution à 8 buts par match est écartée

    def test_champions_league_uses_domestic_stats(self):
        app.SQUADS.clear()
        squad = app.espn_squad("uefa.champions", "382")
        self.assertEqual(squad["league"], "eng.1")
        self.assertEqual(len(squad["players"]), 4)          # 4 joueurs de champ, le gardien est écarté

    def test_nhl_game(self):
        g = self.data["nhl"][0]
        raw = [app.implied("-218"), app.implied("+180")]
        self.assertEqual(g["winH"], round(raw[0] / sum(raw) * 100))
        self.assertAlmostEqual(g["xgH"] + g["xgA"], 6.3, delta=0.01)
        names = [p["name"] for p in g["squad_h"]]
        self.assertEqual(set(names), {"Tom Wilson", "Jakob Chychrun"})   # « Blessé Absent » n'était pas aligné
        self.assertEqual({p["team"] for p in g["squad_a"]}, {"Seattle Kraken"})
        self.assertEqual(g["goalie_a"]["name"], "Philipp Grubauer")   # Seattle a joué hier, Daccord souffle
        self.assertEqual(g["goalie_h"]["name"], "Logan Thompson")     # le gardien blessé (absent de la feuille) est écarté
        self.assertTrue(any("joué hier" in c for c in g["ctx"]))
        self.assertTrue(g["squad_h"][0]["p_goal"] >= g["squad_h"][-1]["p_goal"])
        self.assertTrue(all(p["p_point"] >= p["p_goal"] for p in g["squad_h"] + g["squad_a"]))
        # récapitulatif : au plus 3 joueurs par équipe, triés par chance de faire au moins 1 point
        self.assertTrue(1 <= len(g["best_h"]) <= 3 and 1 <= len(g["best_a"]) <= 3)
        wilson = next(p for p in g["best_h"] if p["name"] == "Tom Wilson")
        self.assertEqual((wilson["goals"], wilson["assists"], wilson["gp"]), (2, 3, 4))   # stats de la saison en cours
        self.assertEqual([p["p_point"] for p in g["best_a"]], sorted((p["p_point"] for p in g["best_a"]), reverse=True))

    def test_history_and_bilan(self):
        hist = app.load_json(app.HIST_FILE, {})
        self.assertEqual(hist["foot-900"]["result"], 0)
        self.assertEqual(hist["foot-900"]["score"], "2-1")
        self.assertEqual(hist["nhl-777"]["result"], 0)
        self.assertIn("foot-401", hist)
        self.assertIn("nhl-2026020084", hist)
        self.assertEqual(self.data["bilan"]["foot"]["n"], 1)
        self.assertEqual(self.data["bilan"]["foot"]["fav_rate"], 100)
        self.assertEqual(self.data["bilan"]["nhl"]["n"], 1)

    def test_finished_match_keeps_pre_match_odds(self):
        live_odds = [dict(provider=dict(name="X"), drawOdds=dict(moneyLine=1500),
                          moneyline=dict(home=dict(close=dict(odds="-5000")), away=dict(close=dict(odds="+3000"))))]
        e = espn_soccer_event("402", "2026-10-11T12:00Z", "post", True, ("1", "A", "3"), ("2", "B", "1"), live_odds)
        hist = {"foot-402": dict(sport="foot", league="L", date="2026-10-11", home="A", away="B",
                                 p=[0.4, 0.3, 0.3], source="Cotes X", result=None, score=None)}
        m = app.foot_match(e, "L", {}, 1.4, {}, hist)
        self.assertEqual([m["pH"], m["pD"], m["pA"]], [40, 30, 30])

    def test_relieved_goalie_is_not_projected(self):
        profile = dict(goalies=[goalie(30, "Igor", "Shesterkin", 5, .927), goalie(31, "Dylan", "Garand", 1, .971)],
                       last=dict(starter=30, relieved=30, goalies_dressed={30, 31}))
        g = app.projected_goalie(profile, back_to_back=False)
        self.assertEqual(g["name"], "Dylan Garand")
        self.assertIn("remplacé", g["note"])

    def test_squad_cache_reused_same_day(self):
        cache = app.load_json(app.CACHE_FILE, {})
        self.assertEqual(cache["date"], "2026-10-11")
        self.assertIn("eng.1|382", cache["squads"])
        app.SQUADS.clear()
        app.get_json = lambda url, params=None: None          # ESPN injoignable : le cache doit suffire
        app.load_squad_cache(dt.date(2026, 10, 11))
        self.assertEqual(app.espn_squad("eng.1", "382")["players"][0]["name"], "Erling Haaland")
        app.SQUADS.clear()
        app.load_squad_cache(dt.date(2026, 10, 12))          # le lendemain : cache ignoré
        self.assertNotIn(("eng.1", "382"), app.SQUADS)

    def test_nhl_night_belongs_to_previous_us_day(self):
        self.assertEqual(self.data["nhl_date"], "2026-10-11")
        late = dt.datetime(2026, 10, 12, 2, 30, tzinfo=app.PARIS)   # 2 h 30 à Paris : soirée NHL du 11
        self.assertEqual((late - dt.timedelta(hours=7)).date().isoformat(), "2026-10-11")

    def test_update_schedule(self):
        import horaires
        P = horaires.PARIS
        at = lambda d, h, m: dt.datetime(2026, 10, d, h, m, tzinfo=P)
        run = lambda h, m, last, ev="schedule": horaires.should_run(at(18, h, m), ev, last)
        self.assertTrue(run(20, 37, at(18, 18, 8)))     # 20 h 20 lancé avec 17 min de retard
        self.assertTrue(run(20, 56, at(18, 18, 8)))     # 20 h 20 lancé avec 36 min de retard (cas du 18/10) : rattrapé
        self.assertFalse(run(20, 52, at(18, 20, 25)))   # 20 h 20 déjà publié : rien à refaire
        self.assertFalse(run(19, 50, at(18, 18, 8)))    # aucun horaire prévu depuis 18 h 08
        self.assertFalse(run(7, 10, at(17, 23, 55)))    # nuit : publication de 23 h 50 encore valable
        self.assertTrue(run(7, 20, at(17, 23, 55)))
        self.assertTrue(run(8, 20, at(17, 21, 30)))     # 23 h 50 et 7 h 20 ratés : rattrapés au passage suivant
        self.assertTrue(run(3, 20, at(18, 3, 0), "workflow_dispatch"))   # lancement à la main : toujours
        self.assertTrue(run(12, 50, None))             # pas encore de données publiées
        winter = dt.datetime(2026, 12, 5, 19, 35, tzinfo=dt.timezone.utc)   # 20 h 35 à Paris en hiver
        self.assertTrue(horaires.should_run(winter, "schedule", dt.datetime(2026, 12, 5, 18, 10, tzinfo=P)))
        self.assertIsNotNone(horaires.published())     # le champ « generated » se relit bien
        # avant un coup d'envoi à 20 h 45 : recalcul toutes les 30 min pour suivre les compos officielles
        ko = [at(18, 20, 45)]
        self.assertTrue(horaires.should_run(at(18, 19, 52), "schedule", at(18, 19, 20), ko))   # 53 min avant
        self.assertTrue(horaires.should_run(at(18, 20, 24), "schedule", at(18, 19, 53), ko))   # 21 min avant
        self.assertFalse(horaires.should_run(at(18, 20, 24), "schedule", at(18, 20, 21), ko))  # publié il y a 3 min
        self.assertFalse(horaires.should_run(at(18, 20, 43), "schedule", at(18, 20, 24), ko))  # coup d'envoi imminent
        self.assertFalse(horaires.should_run(at(18, 19, 10), "schedule", at(18, 18, 8), ko))   # 1 h 35 avant : trop tôt
        self.assertTrue(all(k.tzinfo for k in horaires.kickoffs()))

    def test_picks_and_selection(self):
        m = self.data["foot"][0]
        pk = m["picks"]
        team_markets = ["1", "2", "1N", "N2", "o25", "u25", "btts_y", "btts_n"]
        self.assertIn(pk["main"]["market"], team_markets)
        self.assertGreaterEqual(pk["main"]["p"], pk["winner"]["p"])          # le plus sûr est au moins aussi probable
        self.assertEqual(pk["winner"]["market"], "2")                          # Man City favori
        self.assertEqual(pk["player"]["player"], "Erling Haaland")
        self.assertTrue(1 <= pk["main"]["stars"] <= 5)
        g = self.data["nhl"][0]["picks"]
        self.assertIn(g["main"]["market"], ["win_h", "win_a", "o55", "u55"])
        self.assertEqual(g["winner"]["market"], "win_h")
        self.assertIn("au moins 1 point", g["player"]["label"])
        sel = self.data["selection"]
        self.assertEqual([c["p"] for c in sel], sorted((c["p"] for c in sel), reverse=True))
        self.assertTrue(all(sum(c["id"] == x["id"] for x in sel) <= 2 for c in sel))
        store = app.load_json(app.PICKS_FILE, {})
        self.assertTrue(any(v["match"] == "foot-401" and v["result"] is None for v in store.values()))

    def test_match_stakes(self):
        T = {str(i): dict(rank=i, pts=40 - 2 * i, gp=10) for i in range(1, 19)}
        self.assertEqual(app.foot_stakes("Ligue 1", "PSG", "Lens", T["1"], T["3"], T), "Choc au sommet : 1er contre 3e.")
        s = app.foot_stakes("Ligue 1", "Nice", "Metz", T["6"], T["17"], T)
        self.assertIn("course à l'Europe", s)
        self.assertIn("zone de relégation", s)
        self.assertIn("lutte pour le maintien, 4 points d'avance", app.foot_stakes("Ligue 1", "A", "B", T["14"], T["9"], T))
        early = dict(rank=9, pts=6, gp=3)
        self.assertIn("Début de saison", app.foot_stakes("Ligue 1", "A", "B", early, dict(early, rank=10), T))
        ucl = app.foot_stakes("Ligue des champions", "Real", "Slavia", dict(rank=3, pts=6, gp=2), dict(rank=30, pts=0, gp=2), {})
        self.assertIn("top 8", ucl)
        self.assertIn("zone d'élimination", ucl)
        self.assertIn("Match de division", app.nhl_stakes("Boston", "Toronto", dict(divisionName="Atlantic", gamesPlayed=30),
                                                          dict(divisionName="Atlantic", gamesPlayed=30, wildcardSequence=5)))
        self.assertIn("stakes", self.data["foot"][0])

    def test_live_feed(self):
        live = app.load_json(app.LIVE_FILE, {})
        ids = {m["id"] for m in live["matches"]}
        self.assertIn("foot-401", ids)
        foot = next(m for m in live["matches"] if m["id"] == "foot-401")
        self.assertEqual(foot["code"], "eng.1")
        self.assertEqual(foot["edate"], "20261011")
        self.assertIn("main", foot["picks"])
        self.assertNotIn("squad_h", foot)                       # version compacte
        nhl = next(m for m in live["matches"] if m["sport"] == "nhl")
        self.assertIn("winH", nhl)
        self.assertEqual(live["summary"]["date"], "2026-10-11")
        self.assertLessEqual(len(live["summary"]["top"]), 3)

    def test_nhl_total_pick_is_json_safe(self):
        # gros total de buts attendus : « Plus de 5,5 buts » (calcul scipy) devient le pronostic le plus sûr
        pk = app.nhl_picks("A", "B", 0.5, 4.5, 4.5, {})
        self.assertEqual(pk["main"]["market"], "o55")
        json.dumps(pk)                                      # ne doit pas lever d'erreur

    def test_resolve_picks(self):
        history = {"foot-900": dict(sport="foot", score="2-1", result=0, scorers=["Bukayo Saka", "Kai Havertz"]),
                   "nhl-777": dict(sport="nhl", score="5-1", result=0)}
        def pk(match, sport, market, player=None, pid=None):
            return dict(match=match, sport=sport, market=market, player=player, pid_player=pid, result=None)
        store = {"a": pk("foot-900", "foot", "1"), "b": pk("foot-900", "foot", "o25"),
                 "c": pk("foot-900", "foot", "u25"), "d": pk("foot-900", "foot", "btts_y"),
                 "e": pk("foot-900", "foot", "scorer", "Bukayo Saka"), "f": pk("foot-900", "foot", "scorer", "Cole Palmer"),
                 "g": pk("nhl-777", "nhl", "win_a"), "h": pk("nhl-777", "nhl", "o55"),
                 "i": pk("nhl-777", "nhl", "point", "David Pastrnak", 88), "j": pk("nhl-777", "nhl", "goal", "David Pastrnak", 88)}
        app.resolve_picks(store, history)
        got = {k: v["result"] for k, v in store.items()}
        self.assertEqual(got, dict(a=True, b=True, c=False, d=True, e=True, f=False, g=False, h=True, i=True, j=False))

    def test_picks_bilan_by_day_and_league(self):
        def pk(day, sport, league, ok, p=0.7):
            return dict(match="x", sport=sport, league=league, date=day, home="A", away="B", label="L", p=p, stars=3, result=ok)
        store = {"1": pk("2026-10-17", "foot", "Ligue 1", True), "2": pk("2026-10-17", "foot", "Ligue 1", False),
                 "3": pk("2026-10-18", "foot", "LaLiga", True), "4": pk("2026-10-18", "nhl", "NHL", True),
                 "5": pk("2026-10-18", "nhl", "NHL", None)}                     # pas encore vérifié : ignoré
        b = app.picks_bilan(store)
        self.assertEqual((b["n"], b["since"]), (4, "2026-10-17"))
        self.assertEqual((b["total"]["all"]["ok"], b["total"]["foot"]["n"], b["total"]["nhl"]["ok"]), (3, 3, 1))
        self.assertEqual([d["date"] for d in b["days"]], ["2026-10-18", "2026-10-17"])       # le plus récent d'abord
        self.assertEqual((b["days"][0]["all"]["n"], b["days"][0]["nhl"]["n"]), (2, 1))
        self.assertEqual([(x["sport"], x["league"], x["n"]) for x in b["leagues"]],
                         [("foot", "Ligue 1", 2), ("foot", "LaLiga", 1), ("nhl", "NHL", 1)])
        self.assertEqual(len(b["details"]), 4)
        self.assertEqual(app.picks_bilan({}), dict(n=0))
        # les paris Fun et le score exact sont comptés à part, sans toucher au bilan des pronostics sûrs
        store["6"] = dict(pk("2026-10-18", "foot", "LaLiga", False, 0.4), kinds=["fun"])
        store["7"] = dict(pk("2026-10-18", "foot", "LaLiga", True, 0.12), kinds=["score"])
        b = app.picks_bilan(store)
        self.assertEqual(b["n"], 4)
        self.assertEqual((b["fun"]["all"]["n"], b["fun"]["all"]["ok"]), (2, 1))
        self.assertEqual(b["days"][0]["fun"]["all"]["n"], 2)
        self.assertEqual(sum(d["fun"] for d in b["details"]), 2)

    def test_fun_and_exact_score_picks(self):
        m = self.data["foot"][0]["picks"]
        self.assertEqual(m["score"]["market"], "exact")
        self.assertRegex(m["score"]["value"], r"^\d+-\d+$")
        fun = m.get("fun")
        self.assertIsNotNone(fun)
        self.assertTrue(0.35 <= fun["p"] <= 0.66)
        self.assertNotIn(fun["label"], (m["main"]["label"], m["winner"]["label"]))
        g = app.nhl_picks("A", "B", 0.55, 3.2, 2.9, {}, q=0.5)
        self.assertTrue(0.35 <= g["fun"]["p"] <= 0.66)
        store = app.load_json(app.PICKS_FILE, {})
        groups = {v["market"]: app.pick_group(v) for v in store.values() if v["match"] == "foot-401"}
        self.assertEqual(groups["exact"], "fun")
        self.assertEqual(groups[m["main"]["market"]], "safe")

    def test_picks_tell_one_story(self):
        # Leeds–Man United du 18/10 : Leeds légèrement favori. Avant : Fun « Nul ou Man United », score exact 1-1
        # et buteur de Man United. Tous les pronostics doivent maintenant aller dans le sens de l'analyse.
        lh, la = app.foot_lambdas(0.38, 0.33)
        p_h, p_d, p_a = app.outcome_probs(lh, la)
        scores, scorep = app.top_scores(lh, la)
        rows = {"h": [dict(name="Joël Piroe", p_goal=22, team="Leeds", side="h")],
                "a": [dict(name="Bryan Mbeumo", p_goal=28, team="Man United", side="a")]}
        pk = app.foot_picks("Leeds", "Man United", p_h, p_d, p_a, 0.45, 0.5, rows, scores, scorep, lh, la)
        self.assertEqual(pk["winner"]["label"], "Victoire Leeds")
        h, a = map(int, pk["score"]["value"].split("-"))
        self.assertGreater(h, a)                                   # score exact cohérent avec le vainqueur
        self.assertEqual(pk["player"]["team"], "Leeds")            # buteur de l'équipe annoncée gagnante
        fun = pk["fun"]
        self.assertNotIn(fun["market"], ("N2", "2", "X", "fg_a", "cs_a", "a2"))
        # règle générale : quand le Fun passe, le Safe et le Vainqueur ont au moins autant de chances de passer
        g = app.score_grid(lh, la)
        P = lambda f: sum(g[i][j] * f(i, j) for i in range(11) for j in range(11))
        f = app.market_fn("foot", fun["market"])
        for mk in (pk["main"]["market"], pk["winner"]["market"]):
            a_ = app.market_fn("foot", mk)
            self.assertGreaterEqual(P(lambda i, j: a_(i, j) * f(i, j)), P(a_) * P(f) - 1e-9, mk)
        # NHL : le Fun n'est jamais le même pari que le Safe sous un autre nom
        n = app.nhl_picks("Minnesota Wild", "Columbus", 0.63, 3.56, 2.74, {}, q=0.6)
        self.assertNotIn(n["fun"]["market"], (n["main"]["market"], "win_a", "reg_a"))

    def test_second_opinion(self):
        # ClubElo et MoneyPuck trouvés, d'accord avec les cotes : le Safe reste
        self.assertEqual((self.data["second"]["foot"]["ok"], self.data["second"]["foot"]["missing"]), (1, []))
        self.assertEqual((self.data["second"]["nhl"]["ok"], self.data["second"]["nhl"]["missing"]), (1, []))
        # MoneyPuck : San Jose s'écrit « SJS » dans certains fichiers, « S.J » dans d'autres
        old = app.get_text
        try:
            for code in ("SJS", "S.J"):
                csv_ = MP_HEAD + f"{code},2026,{code},{code},Team Level,all,6,15.0,20.0,21600\nTOR,2026,TOR,TOR,Team Level,all,6,19.0,14.0,21600\n"
                app.get_text = lambda url, headers=None, tries=3, timeout=30, c=csv_: c if "/2026/" in url else MP_HEAD
                app.SECOND.clear()
                self.assertIsNotNone(app.nhl_second("TOR", "SJS", 20262027), code)
            # ClubElo en panne (502, comme le 19/10) : une seule tentative courte, pas d'équipe « introuvable »
            calls = []
            def fake(url, headers=None, tries=3, timeout=30):
                calls.append((url, tries, timeout))
                return None
            app.get_text = fake
            app.SECOND.clear(); app.SECOND_STATS.update(foot=[0, []])
            self.assertIsNone(app.elo_second("eng.1", dt.date(2026, 10, 19), ["Spurs"], ["Coventry"]))
            self.assertEqual(calls, [("http://api.clubelo.com/2026-10-19", 1, 10)])
            self.assertEqual(app.SECOND_STATS["foot"][1], [])
            self.assertTrue(app.SECOND_STATS.get("foot_down"))
        finally:
            app.get_text = old
            app.SECOND.clear()
        foot, nhl = self.data["foot"][0], self.data["nhl"][0]
        self.assertTrue(foot["second"]["src"].startswith("Classement Elo ClubElo (Liverpool 1960, Man City 2010)"))
        self.assertTrue(foot["second"]["agree"])
        self.assertEqual(self.data["second"]["foot"]["status"], "ok")
        self.assertIn("main", foot["picks"])
        self.assertTrue(nhl["second"]["agree"] and nhl["second"]["winH"] > 50, nhl["second"])
        # noms : alias et pas de confusion Paris FC / Paris SG
        clubs = ["Man City", "Liverpool", "Paris SG", "Paris FC", "Forest"]
        self.assertEqual(app.best_club(["Paris FC"], clubs), "Paris FC")
        self.assertEqual(app.best_club(["PSG", "Paris Saint-Germain"], clubs), "Paris SG")
        self.assertEqual(app.best_club(["Nottm Forest", "Nottingham Forest"], clubs), "Forest")
        self.assertIsNone(app.best_club(["Real Madrid"], clubs))
        # classement Elo seul (match absent de la liste ClubElo) : probabilités calculées
        sec = app.elo_second("eng.1", dt.date(2026, 10, 12), ["Forest"], ["Man City"])
        h, d, a = sec["p"]
        self.assertAlmostEqual(h + d + a, 1, places=6)
        self.assertGreater(a, h)                                  # City 260 points Elo au-dessus
        # désaccord : les cotes voient Lyon favori, le deuxième avis voit Nice → pas de Safe
        picks = dict(main=app.make_pick("1N", "Lyon ou nul", 0.76), winner=app.make_pick("1", "Victoire Lyon", 0.52))
        out = app.concord("foot", picks, (0.52, 0.25, 0.23), dict(src="ClubElo", p=(0.30, 0.27, 0.43)))
        self.assertFalse(out["agree"])
        self.assertNotIn("main", picks)
        self.assertEqual(picks["held"]["label"], "Lyon ou nul")
        # même favori mais Safe jugé bien moins sûr (76 % contre 60 %) → pas de Safe non plus
        picks = dict(main=app.make_pick("1N", "Lyon ou nul", 0.76))
        out = app.concord("foot", picks, (0.52, 0.25, 0.23), dict(src="ClubElo", p=(0.36, 0.24, 0.40 - 0.0)))
        self.assertFalse(out["agree"])
        # avis proches : Safe gardé
        picks = dict(main=app.make_pick("1N", "Lyon ou nul", 0.76))
        self.assertTrue(app.concord("foot", picks, (0.52, 0.25, 0.23), dict(src="ClubElo", p=(0.48, 0.26, 0.26)))["agree"])
        self.assertIn("main", picks)
        # source injoignable : pas de deuxième avis, rien ne change
        self.assertIsNone(app.concord("foot", picks, (0.52, 0.25, 0.23), None))

    def test_scorer_pick(self):
        # l'analyse des joueurs n'est pas modifiée ; le buteur proposé en découle
        P = lambda name, pos, apps, mins, g, a, pid="": dict(name=name, pos=pos, apps=apps, minutes=mins, goals=g, assists=a, pid=pid)
        squad = dict(league="fra.1", season=2026, players=[
            P("Ernest Nuamah", "F", 5, 378, 3, 1, "n1"), P("Loïs Openda", "F", 5, 394, 1, 1, "o1"),
            P("Zachary Athekame", "D", 6, 540, 2, 2, "z1"), P("Corentin Tolisso", "M", 6, 471, 1, 2, "t1"),
            P("Pavel Sulc", "M", 6, 500, 0, 3, "s1")])
        rows = app.squad_rows(squad, 1.64, "h", "Lyon")
        self.assertEqual(rows[0]["name"], "Ernest Nuamah")
        self.assertEqual((rows[0]["pid"], rows[0]["stats_league"], rows[0]["season"]), ("n1", "fra.1", 2026))
        # buteur proposé : équipe favorite, réussite de la saison et analyse expliquées
        lh, la = app.foot_lambdas(0.52, 0.24)
        p_h, p_d, p_a = app.outcome_probs(lh, la)
        sc, sp = app.top_scores(lh, la)
        nice = [dict(name="Elye Wahi", pos="Attaquant", side="a", team="Nice", goals=4, assists=0, apps=6, p_goal=40, p_dec=45)]
        pk = app.foot_picks("Lyon", "Nice", p_h, p_d, p_a, 0.5, 0.5, {"h": rows, "a": nice}, sc, sp, lh, la)
        self.assertEqual(pk["player"]["player"], "Ernest Nuamah")              # Lyon favori : pas Wahi
        self.assertIn("3 buts et 1 passe en 5 matchs", pk["player"]["why"])
        self.assertIn("Lyon favori (52 %)", pk["player"]["why"])
        self.assertEqual(pk["decisive"]["player"], "Ernest Nuamah")
        self.assertEqual(pk["decisive"]["base"], [5, 3, 1])
        # composition officielle : Nuamah remplaçant → un titulaire ; un défenseur seulement s'il domine nettement
        lineup = ({app.norm(n) for n in ("Zachary Athekame", "Corentin Tolisso", "Pavel Sulc", "Loïs Openda")}, {app.norm("Ernest Nuamah")})
        rows = app.squad_rows(squad, 1.64, "h", "Lyon", lineup)
        best = app.pick_scorer(rows)
        self.assertEqual(best["lineup"], "start")
        d = max((r for r in rows if r["pos"] == "Défenseur"), key=lambda r: r["p_goal"])
        o = max((r for r in rows if r["pos"] != "Défenseur" and r.get("lineup") == "start"), key=lambda r: r["p_goal"])
        self.assertEqual(best["name"], d["name"] if d["p_goal"] >= 1.3 * o["p_goal"] else o["name"])
        # vérification « buteur ou passeur » : feuille de match, sinon buts + passes avant / après
        dec = dict(player="Corentin Tolisso", pid_player="t1", stats_league="fra.1", season=2026, base=[6, 1, 2], date="2026-10-18")
        self.assertTrue(app.decisive_result(dec, dict(scorers=["Corentin Tolisso"])))
        old = app.get_json
        try:
            stats = lambda apps, g, a: (lambda url, params=None: dict(splits=dict(categories=[dict(name="general", stats=[
                dict(name="appearances", value=apps), dict(name="totalGoals", value=g), dict(name="goalAssists", value=a)])])))
            app.get_json = stats(7, 1, 3)
            self.assertTrue(app.decisive_result(dec, dict(scorers=["Ernest Nuamah"])))     # une passe décisive
            app.get_json = stats(7, 1, 2)
            self.assertFalse(app.decisive_result(dec, dict(scorers=[])))
            app.get_json = stats(6, 1, 2)                                                  # ESPN pas encore à jour
            self.assertIsNone(app.decisive_result(dict(dec, date=dt.date.today().isoformat()), dict(scorers=[])))
        finally:
            app.get_json = old

    def test_official_lineup(self):
        # Lyon–Nice : Nuamah remplaçant, Openda absent → le pronostic buteur va à un titulaire
        def roster(names_start, names_bench):
            return [dict(starter=True, athlete=dict(displayName=n)) for n in names_start] + \
                   [dict(starter=False, athlete=dict(displayName=n)) for n in names_bench]
        start = ["Rémy Descamps", "Zachary Athekame", "Ainsley Maitland", "Moussa Niakhate", "Abner", "Mathys Bidstrup",
                 "Tanner Tessmann", "Noah Nartey", "Boudache", "Pavel Sulc", "Duranville"]
        old = app.get_json
        app.get_json = lambda url, params=None: dict(rosters=[dict(homeAway="home", roster=roster(start, ["Ernest Nuamah", "Corentin Tolisso"])),
                                                             dict(homeAway="away", roster=roster(["x"] * 3, []))])
        try:
            lu = app.espn_lineup("fra.1", "401876434")
        finally:
            app.get_json = old
        self.assertEqual(set(lu), {"h"})                       # l'équipe sans 11 titulaires connus est ignorée
        rows = [dict(name="Ernest Nuamah", p_goal=32, p_dec=42), dict(name="Loïs Openda", p_goal=19, p_dec=31),
                dict(name="Pavel Šulc", p_goal=12, p_dec=25)]
        missing = app.apply_lineup(rows, lu["h"])
        got = {r["name"]: (r["lineup"], r["p_goal"]) for r in rows}
        self.assertEqual(got["Pavel Šulc"], ("start", 12))
        self.assertEqual(got["Loïs Openda"], ("out", 0))
        self.assertEqual(got["Ernest Nuamah"][0], "bench")
        self.assertLess(got["Ernest Nuamah"][1], 12)
        self.assertEqual(missing, ["Ernest Nuamah (remplaçant)", "Loïs Openda (absent)"])
        for r in rows:
            r["team"] = "Lyon"
        pk = app.foot_picks("Lyon", "Nice", 0.5, 0.26, 0.24, 0.5, 0.5, {"h": rows, "a": []}, ["1-1"], [12])
        self.assertEqual(pk["player"]["player"], "Pavel Šulc")
        self.assertEqual(pk["player"]["lineup"], "start")

    def test_resolve_fun_markets(self):
        history = {"foot-1": dict(sport="foot", score="2-0", result=0, first="h", scorers=[]),
                   "foot-2": dict(sport="foot", score="0-0", result=1, scorers=[]),
                   "nhl-1": dict(sport="nhl", score="3-2 (prol.)", result=0)}
        def pk(match, sport, market, value=None):
            return dict(match=match, sport=sport, market=market, value=value, result=None)
        store = {"a": pk("foot-1", "foot", "h2"), "b": pk("foot-1", "foot", "cs_h"), "c": pk("foot-1", "foot", "fg_h"),
                 "d": pk("foot-1", "foot", "o15"), "e": pk("foot-1", "foot", "u15"), "f": pk("foot-1", "foot", "exact", "2-0"),
                 "g": pk("foot-2", "foot", "X"), "h": pk("foot-2", "foot", "fg_a"), "i": pk("foot-2", "foot", "exact", "1-1"),
                 "j": pk("nhl-1", "nhl", "ot"), "k": pk("nhl-1", "nhl", "reg_h"), "l": pk("nhl-1", "nhl", "o55"),
                 "m": pk("nhl-1", "nhl", "u55")}
        app.resolve_picks(store, history)
        got = {k: v["result"] for k, v in store.items()}
        self.assertEqual(got, dict(a=True, b=True, c=True, d=True, e=False, f=True, g=True, h=False, i=False,
                                   j=True, k=False, l=False, m=True))

    def test_name_matching(self):
        self.assertLess(app.similarity("Paris Saint-Germain", "Paris FC"), 0.8)
        self.assertGreaterEqual(app.similarity("Olympique de Marseille", "Marseille"), 0.9)
        self.assertGreaterEqual(app.similarity("Washington Capitals", "Washington Capitals"), 1.0)

    def test_data_file_written(self):
        with open(app.DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["date"], "2026-10-11")
        self.assertEqual(len(data["foot"]), 1)
        self.assertEqual(len(data["nhl"]), 1)


if __name__ == "__main__":
    unittest.main()
