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
        run = lambda h, m, ev="schedule": horaires.should_run(dt.datetime(2026, 10, 18, h, m, tzinfo=P), ev)
        self.assertTrue(run(20, 37))          # créneau 20 h 20 lancé avec 17 min de retard
        self.assertTrue(run(7, 20))
        self.assertFalse(run(20, 52))         # créneau 20 h 50 : pas un passage utile
        self.assertFalse(run(3, 20))
        self.assertTrue(run(3, 20, "workflow_dispatch"))   # lancement à la main : toujours
        winter = dt.datetime(2026, 12, 5, 19, 20, tzinfo=dt.timezone.utc)   # 20 h 20 à Paris en hiver
        self.assertTrue(horaires.should_run(winter, "schedule"))

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
