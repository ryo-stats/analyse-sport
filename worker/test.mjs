// Tests du robot d'alertes et du calcul en direct : node worker/test.mjs
import assert from "node:assert/strict";
import * as L from "../docs/live.js";
import { tick, parisNow } from "./robot.js";

const MIN = 60e3;
const T = Date.parse("2026-10-18T13:00:00Z");          // coup d'envoi : 15 h à Paris
const pick = (market, label, p, extra = {}) => ({market, label, p, prob: Math.round(p * 100), stars: 1 + [.5, .6, .7, .8].filter(s => p >= s).length, ...extra});
const FOOT = {id: "foot-1", sport: "foot", league: "Premier League", code: "eng.1", edate: "20261018", kickoff: new Date(T).toISOString(),
              time: "15:00", home: "Arsenal", away: "Chelsea", state: "pre", xgH: 1.8, xgA: 0.9, pH: 55, pD: 25, pA: 20,
              picks: {main: pick("1N", "Arsenal ou nul", 0.8), winner: pick("1", "Victoire Arsenal", 0.55),
                      player: pick("scorer", "Bukayo Saka marque", 0.35, {player: "Bukayo Saka", team: "Arsenal"})}};
const NHL = {id: "nhl-2", sport: "nhl", league: "NHL", edate: "20261017", kickoff: "2026-10-17T23:00:00Z", time: "01:00",
             home: "Boston Bruins", away: "Philadelphia Flyers", state: "FUT", xgH: 3.3, xgA: 2.7, pH: 47, pD: 22, pA: 31,
             winH: 58, espn_id: "999", picks: {main: pick("win_h", "Victoire Boston Bruins", 0.58), winner: pick("win_h", "Victoire Boston Bruins", 0.58)}};
const LIVE = {generated: "2026-10-18T07:20+02:00", date: "2026-10-18", matches: [FOOT, NHL],
              summary: {date: "2026-10-18", yesterday: {n: 10, ok: 7}, foot: 12, nhl: 8,
                        top: [{label: "Arsenal ou nul", prob: 80, time: "15:00", match: "Arsenal – Chelsea"}]}};

function footEvent({state = "in", name = "STATUS_FIRST_HALF", clock = "10'", period = 1, sh = 0, sa = 0, details = []}){
  return {id: "1", status: {displayClock: clock, period, type: {state, name, shortDetail: clock}},
          competitions: [{competitors: [{homeAway: "home", id: "359", score: String(sh), team: {id: "359", displayName: "Arsenal"}},
                                        {homeAway: "away", id: "363", score: String(sa), team: {id: "363", displayName: "Chelsea"}}],
                          details}]};
}
const goal = (team, name, clock) => ({scoringPlay: true, team: {id: team}, clock: {displayValue: clock}, athletesInvolved: [{displayName: name}]});
const red = (team, clock) => ({redCard: true, team: {id: team}, clock: {displayValue: clock}});

let ok = 0;
const test = (name, fn) => { fn(); ok++; console.log("ok -", name); };

test("probabilités d'avant-match retrouvées au coup d'envoi", () => {
  const s = L.parseEspn(footEvent({state: "pre", clock: "0'"}), "foot");
  const lp = L.liveProbs(FOOT, s);
  assert.ok(Math.abs(lp.p1 + lp.pN + lp.p2 - 1) < 1e-9);
  assert.ok(lp.p1 > 0.5 && lp.p1 < 0.65, `p1=${lp.p1}`);
});

test("but et minute changent les probabilités", () => {
  const early = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "10'"}), "foot"));
  const lead = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "80'", period: 2, sh: 1}), "foot"));
  assert.ok(lead.p1 > 0.85 && lead.p1 > early.p1, `p1=${lead.p1}`);
  const late00 = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "85'", period: 2}), "foot"));
  assert.ok(late00.o25 < 0.05, "0-0 à la 85e : plus de 2,5 buts presque impossible");
  assert.equal(L.parseEspn(footEvent({name: "STATUS_SECOND_HALF", clock: "80'", period: 2}), "foot").label, "80'");
  const half = L.parseEspn(footEvent({name: "STATUS_HALFTIME", clock: "HT"}), "foot");
  assert.equal(half.elapsed, 47);
  assert.equal(half.label, "Mi-temps");
});

test("carton rouge : l'équipe réduite perd des chances", () => {
  const base = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "30'"}), "foot"));
  const r = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "30'", details: [red("359", "29'")]}), "foot"));
  assert.ok(r.p1 < base.p1 - 0.05);
});

test("buteur : 100 % dès qu'il a marqué, 0 % s'il n'a pas marqué à la fin", () => {
  const s = L.parseEspn(footEvent({clock: "30'", sh: 1, details: [goal("359", "Bukayo Saka", "28'")]}), "foot");
  const lp = L.liveProbs(FOOT, s);
  assert.equal(L.pickProb(FOOT.picks.player, FOOT, s, lp), 1);
  const end = L.parseEspn(footEvent({state: "post", name: "STATUS_FULL_TIME", clock: "FT", sh: 1, details: [goal("359", "Martin Ødegaard", "28'")]}), "foot");
  assert.equal(L.pickProb(FOOT.picks.player, FOOT, end, L.liveProbs(FOOT, end)), 0);
});

test("NHL : 3-1 après deux périodes, Boston très favori ; prolongation proche de 50/50", () => {
  const ev = (period, clock, sh, sa, name = "STATUS_IN_PROGRESS", state = "in") => ({id: "999",
    status: {period, displayClock: clock, type: {state, name, shortDetail: ""}},
    competitions: [{competitors: [{homeAway: "home", score: String(sh), team: {displayName: "Boston Bruins"}},
                                  {homeAway: "away", score: String(sa), team: {displayName: "Philadelphia Flyers"}}]}]});
  const s = L.parseEspn(ev(2, "0:00", 3, 1, "STATUS_END_PERIOD"), "nhl");
  assert.equal(s.elapsed, 40);
  assert.ok(L.liveProbs(NHL, s).winH > 0.85);
  const ot = L.liveProbs(NHL, L.parseEspn(ev(4, "3:12", 2, 2), "nhl"));
  assert.ok(ot.winH > 0.4 && ot.winH < 0.75, `prolongation ${ot.winH}`);
  const tied = L.liveProbs(NHL, L.parseEspn(ev(3, "10:00", 2, 2), "nhl"));
  assert.equal(L.pct(tied.markets.o55 + tied.markets.u55), 100);
});

// déroulé complet d'un match en favori
const io = feed => ({site: "https://site/", getJSON: async url => url.includes("live.json") ? LIVE : {events: feed()}});
let current = footEvent({state: "pre", clock: "0'"});
const st = {}, favs = {"foot-1": T - 3600e3};
const run = async at => (await tick(st, favs, at, io(() => [current]))).msgs;

const flow = async () => {
  let msgs = await run(T - 40 * MIN);
  assert.equal(msgs.length, 0, "trop tôt pour le rappel");
  msgs = await run(T - 15 * MIN);
  assert.equal(msgs.length, 1);
  assert.match(msgs[0].title, /⏰ 15:00 : Arsenal – Chelsea/);
  assert.match(msgs[0].body, /Arsenal ou nul : 80 %/);
  current = footEvent({clock: "5'"});
  assert.equal((await run(T + 5 * MIN)).length, 0, "pas d'alerte sans action");
  current = footEvent({clock: "28'", sh: 1, details: [goal("359", "Bukayo Saka", "28'")]});
  msgs = await run(T + 29 * MIN);
  assert.equal(msgs.length, 1);
  assert.match(msgs[0].title, /⚽ But Arsenal ! Arsenal 1-0 Chelsea/);
  assert.match(msgs[0].body, /28' Bukayo Saka/);
  assert.match(msgs[0].body, /Bukayo Saka marque : 35 % → 100 % ✅/);
  current = footEvent({clock: "60'", period: 2, sh: 1, details: [goal("359", "Bukayo Saka", "28'"), red("363", "59'")]});
  msgs = await run(T + 76 * MIN);
  assert.match(msgs[0].title, /🟥 Carton rouge pour Chelsea/);
  assert.equal((await run(T + 77 * MIN)).length, 0, "pas de doublon");
  current = footEvent({state: "post", name: "STATUS_FULL_TIME", clock: "FT", period: 2, sh: 2, sa: 0,
                       details: [goal("359", "Bukayo Saka", "28'"), goal("359", "Kai Havertz", "88'")]});
  msgs = await run(T + 115 * MIN);
  assert.match(msgs[0].title, /🏁 Terminé : Arsenal 2-0 Chelsea/);
  assert.match(msgs[0].body, /✅ Arsenal ou nul/);
  assert.match(msgs[0].body, /✅ Victoire Arsenal/);
  assert.equal((await run(T + 116 * MIN)).length, 0, "match terminé : plus d'alerte");
  console.log("ok - déroulé complet d'un match en favori");

  // un favori ajouté en plein match ne déclenche pas d'alerte rétroactive ; retiré, il ne sonne plus
  const st2 = {}, favs2 = {};
  current = footEvent({clock: "50'", period: 2, sh: 2, sa: 1});
  assert.equal((await tick(st2, favs2, T + 66 * MIN, io(() => [current]))).msgs.length, 0);
  favs2["foot-1"] = T + 66 * MIN;
  assert.equal((await tick(st2, favs2, T + 67 * MIN, io(() => [current]))).msgs.length, 0, "pas d'alerte rétroactive");
  current = footEvent({clock: "70'", period: 2, sh: 2, sa: 2});
  const m2 = (await tick(st2, favs2, T + 88 * MIN, io(() => [current]))).msgs;
  assert.match(m2[0].title, /But Chelsea/);
  delete favs2["foot-1"];
  current = footEvent({clock: "80'", period: 2, sh: 3, sa: 2});
  assert.equal((await tick(st2, favs2, T + 98 * MIN, io(() => [current]))).msgs.length, 0, "favori retiré");
  console.log("ok - favori ajouté en cours de match, puis retiré");

  // résumé du matin, une seule fois
  const st3 = {};
  const morning = Date.parse("2026-10-18T06:10:00Z");     // 8 h 10 à Paris
  assert.equal(parisNow(morning).hour, 8);
  const r1 = (await tick(st3, {}, morning, io(() => []))).msgs;
  assert.match(r1[0].title, /Résumé du matin/);
  assert.match(r1[0].body, /Hier : 7 pronostics réussis sur 10/);
  assert.equal((await tick(st3, {}, morning + 5 * MIN, io(() => []))).msgs.length, 0);
  console.log("ok - résumé du matin une fois par jour");

  // ESPN en panne : pas de plantage, erreur notée
  const st4 = {};
  const broken = {site: "https://site/", getJSON: async url => { if (url.includes("live.json")) return LIVE; throw new Error("site.api.espn.com a répondu 403"); }};
  await tick(st4, {"foot-1": 0}, T + 10 * MIN, broken);
  assert.match(st4.errors[0].text, /403/);
  console.log("ok - panne ESPN notée sans plantage");
  console.log(`\n${ok + 4} tests OK`);
};
flow().catch(e => { console.error(e); process.exit(1); });
