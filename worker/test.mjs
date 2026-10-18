// Tests du robot d'alertes et du calcul en direct : node worker/test.mjs
import assert from "node:assert/strict";
import * as L from "../docs/live.js";
import { tick, route, parisNow, PUSH_HOSTS } from "./robot.js";
import { encrypt, b64u, unb64u } from "./webpush.js";

let ok = 0;
const test = (name, fn) => { fn(); ok++; console.log("ok -", name); };

// chiffrement des notifications : exemple officiel de la RFC 8291 (section 5), octet pour octet
{
  const sub = {keys: {p256dh: "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", auth: "BTBZMqHH6r4Tts7J_aSIgg"}};
  const fixed = {salt: unb64u("DGv6ra1nlYgDCS1FRnbzlw"), asPrivateD: unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
                 asPublic: unb64u("BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")};
  const out = await encrypt("When I grow up, I want to be a watermelon", sub, fixed);
  assert.equal(b64u(out), "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN");
  ok++; console.log("ok - chiffrement Web Push identique à l'exemple de la RFC 8291");
}

const MIN = 60e3;
const T = Date.parse("2026-10-18T13:00:00Z");          // coup d'envoi : 15 h à Paris
const pick = (market, label, p, extra = {}) => ({market, label, p, prob: Math.round(p * 100), stars: 1 + [.5, .6, .7, .8].filter(s => p >= s).length, ...extra});
const FOOT = {id: "foot-1", sport: "foot", league: "Premier League", code: "eng.1", edate: "20261018", kickoff: new Date(T).toISOString(),
              time: "15:00", home: "Arsenal", away: "Chelsea", state: "pre", xgH: 1.8, xgA: 0.9, pH: 55, pD: 25, pA: 20,
              picks: {main: pick("1N", "Arsenal ou nul", 0.8), winner: pick("1", "Victoire Arsenal", 0.55),
                      player: pick("scorer", "Bukayo Saka marque", 0.35, {player: "Bukayo Saka", team: "Arsenal"}),
                      fun: pick("fg_a", "Premier but : Chelsea", 0.4), score: pick("exact", "Score exact 1-0", 0.12, {value: "1-0"})}};
const NHL = {id: "nhl-2", sport: "nhl", league: "NHL", edate: "20261017", kickoff: "2026-10-17T23:00:00Z", time: "01:00",
             home: "Boston Bruins", away: "Philadelphia Flyers", state: "FUT", xgH: 3.3, xgA: 2.7, pH: 47, pD: 22, pA: 31,
             winH: 58, espn_id: "999", picks: {main: pick("win_h", "Victoire Boston Bruins", 0.58), winner: pick("win_h", "Victoire Boston Bruins", 0.58)}};
const LIVE = {generated: "2026-10-18T07:20+02:00", date: "2026-10-18", matches: [FOOT, NHL],
              summary: {date: "2026-10-18", yesterday: {n: 10, ok: 7}, foot: 12, nhl: 8,
                        top: [{label: "Arsenal ou nul", prob: 80, time: "15:00", match: "Arsenal – Chelsea"}]}};

// stats : {h: [tirs, cadrés, corners, possession], a: [...]}, comme le tableau des scores ESPN
const espnStats = v => v ? [["totalShots", v[0]], ["shotsOnTarget", v[1]], ["wonCorners", v[2]], ["possessionPct", v[3]], ["foulsCommitted", 4]]
  .map(([name, x]) => ({name, displayValue: String(x)})) : undefined;
function footEvent({state = "in", name = "STATUS_FIRST_HALF", clock = "10'", period = 1, sh = 0, sa = 0, details = [], stats = null}){
  return {id: "1", status: {displayClock: clock, period, type: {state, name, shortDetail: clock}},
          competitions: [{competitors: [{homeAway: "home", id: "359", score: String(sh), team: {id: "359", displayName: "Arsenal"}, statistics: espnStats(stats && stats.h)},
                                        {homeAway: "away", id: "363", score: String(sa), team: {id: "363", displayName: "Chelsea"}, statistics: espnStats(stats && stats.a)}],
                          details}]};
}
const goal = (team, name, clock) => ({scoringPlay: true, team: {id: team}, clock: {displayValue: clock}, athletesInvolved: [{displayName: name}]});
const red = (team, clock) => ({redCard: true, team: {id: team}, clock: {displayValue: clock}});

test("probabilités d'avant-match retrouvées au coup d'envoi", () => {
  const lp = L.liveProbs(FOOT, L.parseEspn(footEvent({state: "pre", clock: "0'"}), "foot"));
  assert.ok(Math.abs(lp.p1 + lp.pN + lp.p2 - 1) < 1e-9);
  assert.ok(lp.p1 > 0.5 && lp.p1 < 0.65, `p1=${lp.p1}`);
});

test("but et minute changent les probabilités ; « Mi-temps » seulement à la mi-temps", () => {
  const early = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "10'"}), "foot"));
  const lead = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "80'", period: 2, sh: 1}), "foot"));
  assert.ok(lead.p1 > 0.85 && lead.p1 > early.p1, `p1=${lead.p1}`);
  assert.ok(L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "85'", period: 2}), "foot")).o25 < 0.05);
  assert.equal(L.parseEspn(footEvent({name: "STATUS_SECOND_HALF", clock: "80'", period: 2}), "foot").label, "80'");
  const half = L.parseEspn(footEvent({name: "STATUS_HALFTIME", clock: "HT"}), "foot");
  assert.equal(half.elapsed, 47);
  assert.equal(half.label, "Mi-temps");
});

test("carton rouge : l'équipe réduite perd des chances (affichage En direct)", () => {
  const base = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "30'"}), "foot"));
  const r = L.liveProbs(FOOT, L.parseEspn(footEvent({clock: "30'", details: [red("359", "29'")]}), "foot"));
  assert.ok(r.p1 < base.p1 - 0.05);
});

test("déroulement du match : l'équipe qui domine aux tirs voit ses chances monter", () => {
  const plain = L.parseEspn(footEvent({clock: "60'", period: 2, name: "STATUS_SECOND_HALF"}), "foot");
  assert.equal(plain.stats, undefined);                         // pas de stats : comme avant
  // 60e, 0-0 : Chelsea (outsider) écrase Arsenal aux tirs
  const s = L.parseEspn(footEvent({clock: "60'", period: 2, name: "STATUS_SECOND_HALF", stats: {h: [3, 0, 1, 38], a: [14, 6, 7, 62]}}), "foot");
  assert.deepEqual(s.stats.a, {shots: 14, sot: 6, corners: 7, poss: 62});
  const base = L.liveProbs(FOOT, plain), now = L.liveProbs(FOOT, s);
  assert.ok(now.p2 > base.p2 + 0.08 && now.p1 < base.p1 - 0.08, `p1 ${base.p1}→${now.p1}, p2 ${base.p2}→${now.p2}`);
  const r = L.liveRates(FOOT, s);
  assert.equal(r.dom, "a");
  // 30e : la mention apparaît sur le pari Fun qui va dans le sens de l'équipe qui domine
  const s30 = L.parseEspn(footEvent({clock: "30'", stats: {h: [2, 0, 0, 35], a: [9, 4, 4, 65]}}), "foot");
  const fun = L.funPicks(FOOT, s30, {max: 6});
  assert.ok(fun.some(x => x.why === "📈 Chelsea domine (9 tirs, 4 cadrés)" && /Chelsea/.test(x.label)), JSON.stringify(fun));
  // match équilibré : personne ne « domine »
  const even = L.parseEspn(footEvent({clock: "60'", period: 2, name: "STATUS_SECOND_HALF", stats: {h: [8, 3, 4, 50], a: [7, 3, 3, 50]}}), "foot");
  assert.equal(L.liveRates(FOOT, even).dom, null);
  // avant le coup d'envoi, les stats éventuelles ne comptent pas
  assert.equal(L.parseEspn(footEvent({state: "pre", clock: "0'", stats: {h: [1, 1, 1, 50], a: [1, 1, 1, 50]}}), "foot").stats, undefined);
});

test("buteur : 100 % dès qu'il a marqué, 0 % s'il n'a pas marqué à la fin", () => {
  const s = L.parseEspn(footEvent({clock: "30'", sh: 1, details: [goal("359", "Bukayo Saka", "28'")]}), "foot");
  assert.equal(L.pickProb(FOOT.picks.player, FOOT, s, L.liveProbs(FOOT, s)), 1);
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
});

test("paris fun : entre 35 et 66 %, un par famille, supériorité numérique mise en avant", () => {
  const pre = L.funPicks(FOOT, L.parseEspn(footEvent({state: "pre", clock: "0'"}), "foot"));
  assert.ok(pre.length >= 2 && pre.every(x => x.p >= 0.35 && x.p <= 0.66), JSON.stringify(pre));
  assert.equal(new Set(pre.map(x => x.fam)).size, pre.length);
  // Chelsea réduit à 10 à la 30e, 0-0 : un pari côté Arsenal passe en tête, avec la mention
  const red10 = L.funPicks(FOOT, L.parseEspn(footEvent({clock: "30'", details: [red("363", "29'")]}), "foot"));
  assert.equal(red10[0].why, "🟥 Supériorité numérique");
  assert.ok(red10.filter(x => x.why).every(x => /Arsenal|Chelsea ne marque plus/.test(x.label)), JSON.stringify(red10));
  assert.equal(L.funPicks(FOOT, L.parseEspn(footEvent({state: "post", name: "STATUS_FULL_TIME", clock: "FT"}), "foot")).length, 0);
  assert.equal(L.fair(0.5), "2,00");
  // NHL : les paris restent dans la fourchette
  const nhl = L.funPicks(NHL, {state: "pre", sh: 0, sa: 0, period: 0, elapsed: 0, red: {h: 0, a: 0}, goals: []});
  assert.ok(nhl.length >= 1 && nhl.every(x => x.p >= 0.35 && x.p <= 0.66), JSON.stringify(nhl));
});

test("chacun ne reçoit que SES matchs ; résumé du matin seulement si activé", () => {
  const subs = {a: {favs: {"foot-1": 1}, morning: false}, b: {favs: {"nhl-2": 1}, morning: true}, c: {favs: {}, morning: false}};
  const r = route([{match: "foot-1", title: "but"}, {match: "nhl-2", title: "but nhl"}, {morning: true, title: "matin"}], subs);
  assert.deepEqual(r.map(x => `${x.key}:${x.msg.title}`).sort(), ["a:but", "b:but nhl", "b:matin"]);
});

test("seules les adresses des vrais services de notification sont acceptées", () => {
  assert.ok(PUSH_HOSTS.test("https://fcm.googleapis.com/fcm/send/abc"));
  assert.ok(PUSH_HOSTS.test("https://web.push.apple.com/QGx"));
  assert.ok(PUSH_HOSTS.test("https://updates.push.services.mozilla.com/wpush/v2/x"));
  assert.ok(!PUSH_HOSTS.test("https://exemple.com/fcm.googleapis.com/"));
  assert.ok(!PUSH_HOSTS.test("http://fcm.googleapis.com/x"));
});

// déroulé complet d'un match en favori : seulement les buts et le résultat
const io = feed => ({site: "https://site/", getJSON: async url => url.includes("live.json") ? LIVE : {events: feed()}});
let current = footEvent({state: "pre", clock: "0'"});
const st = {}, favs = {"foot-1": T - 3600e3};
const run = async at => (await tick(st, favs, at, io(() => [current]))).msgs;

let msgs = await run(T - 40 * MIN);
assert.equal(msgs.length, 0, "rien avant le match");
assert.equal((await run(T - 15 * MIN)).length, 0, "pas de rappel avant le match");
current = footEvent({clock: "5'"});
assert.equal((await run(T + 5 * MIN)).length, 0, "pas d'alerte sans but");
current = footEvent({clock: "28'", sh: 1, details: [goal("359", "Bukayo Saka", "28'")]});
msgs = await run(T + 29 * MIN);
assert.equal(msgs.length, 1);
assert.equal(msgs[0].match, "foot-1");
assert.match(msgs[0].title, /⚽ But Arsenal ! Arsenal 1-0 Chelsea/);
assert.match(msgs[0].body, /28' Bukayo Saka/);
assert.match(msgs[0].body, /Bukayo Saka marque : 35 % → 100 % ✅/);
assert.match(msgs[0].body, /\n🎲 Fun du moment : .+ \(\d+ %\)/);           // nouveau pari proposé après le but
assert.ok(!/Safe du moment : Arsenal ou nul/.test(msgs[0].body));          // jamais le Safe d'avant-match en double
current = footEvent({clock: "60'", period: 2, sh: 1, details: [goal("359", "Bukayo Saka", "28'"), red("363", "59'")]});
assert.equal((await run(T + 76 * MIN)).length, 0, "pas d'alerte pour un carton rouge");
current = footEvent({clock: "70'", period: 2, sh: 1, sa: 1, details: [goal("359", "Bukayo Saka", "28'"), goal("363", "Cole Palmer", "69'")]});
msgs = await run(T + 86 * MIN);
assert.match(msgs[0].title, /But Chelsea/);
current = footEvent({clock: "75'", period: 2, sh: 1, sa: 0, details: [goal("359", "Bukayo Saka", "28'")]});
msgs = await run(T + 91 * MIN);
assert.match(msgs[0].title, /↩️ But annulé : Arsenal 1-0 Chelsea/);
current = footEvent({state: "post", name: "STATUS_FULL_TIME", clock: "FT", period: 2, sh: 2, sa: 0,
                     details: [goal("359", "Bukayo Saka", "28'"), goal("359", "Kai Havertz", "88'")]});
msgs = await run(T + 115 * MIN);
assert.match(msgs[0].title, /🏁 Terminé : Arsenal 2-0 Chelsea/);
assert.match(msgs[0].body, /^3\/5 pronostics validés/);
assert.match(msgs[0].body, /✅ Safe : Arsenal ou nul/);
assert.match(msgs[0].body, /❌ Fun 🎲 : Premier but : Chelsea/);
assert.match(msgs[0].body, /\n❌ Score exact 1-0/);
assert.equal(L.settle({market: "exact", value: "2-0"}, FOOT, L.parseEspn(current, "foot")), true);
assert.equal(L.settle({market: "fg_h"}, FOOT, L.parseEspn(current, "foot")), true);
assert.equal(L.settle({market: "u25"}, FOOT, L.parseEspn(current, "foot")), true);
assert.equal(L.settle({market: "o25"}, FOOT, L.parseEspn(footEvent({clock: "80'"}), "foot")), null, "pas de résultat avant la fin");
assert.equal((await run(T + 116 * MIN)).length, 0, "match terminé : plus d'alerte");
ok++; console.log("ok - déroulé d'un match en favori : buts, but annulé, résultat (ni rappel ni carton)");

// favori ajouté en plein match : pas d'alerte rétroactive ; retiré : plus rien
{
  const st2 = {}, favs2 = {};
  current = footEvent({clock: "50'", period: 2, sh: 2, sa: 1});
  favs2["foot-1"] = T + 66 * MIN;
  assert.equal((await tick(st2, favs2, T + 67 * MIN, io(() => [current]))).msgs.length, 0, "pas d'alerte rétroactive");
  current = footEvent({clock: "70'", period: 2, sh: 2, sa: 2});
  assert.match((await tick(st2, favs2, T + 88 * MIN, io(() => [current]))).msgs[0].title, /But Chelsea/);
  delete favs2["foot-1"];
  current = footEvent({clock: "80'", period: 2, sh: 3, sa: 2});
  assert.equal((await tick(st2, favs2, T + 98 * MIN, io(() => [current]))).msgs.length, 0, "favori retiré");
  ok++; console.log("ok - favori ajouté en cours de match, puis retiré");
}

// résumé du matin, une seule fois par jour
{
  const st3 = {}, morning = Date.parse("2026-10-18T06:10:00Z");     // 8 h 10 à Paris
  assert.equal(parisNow(morning).hour, 8);
  const r1 = (await tick(st3, {}, morning, io(() => []))).msgs;
  assert.ok(r1[0].morning);
  assert.match(r1[0].body, /Hier : 7 pronostics réussis sur 10/);
  assert.equal((await tick(st3, {}, morning + 5 * MIN, io(() => []))).msgs.length, 0);
  ok++; console.log("ok - résumé du matin une fois par jour");
}

// ESPN en panne : pas de plantage, erreur notée
{
  const st4 = {};
  const broken = {site: "https://site/", getJSON: async url => { if (url.includes("live.json")) return LIVE; throw new Error("site.api.espn.com a répondu 403"); }};
  await tick(st4, {"foot-1": 0}, T + 10 * MIN, broken);
  assert.match(st4.errors[0].text, /403/);
  ok++; console.log("ok - panne ESPN notée sans plantage");
}
console.log(`\n${ok} tests OK`);
