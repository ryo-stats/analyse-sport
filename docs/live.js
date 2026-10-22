// Pronostics en direct : recalcul des probabilités à partir du score, de la minute et des cartons rouges.
// Partagé entre le site (onglet Pronostics > En direct) et le robot d'alertes Cloudflare (worker/).
//
// Principe : avant le match, chaque équipe a un nombre de buts attendus (xgH, xgA) tiré des cotes.
// Pendant le match, on ne garde que la part du temps qui reste, on ajoute ces buts « à venir » au score
// actuel, et on recalcule chaque pronostic. Un carton rouge baisse les buts attendus de l'équipe réduite
// (×0,7) et augmente ceux de l'adversaire (×1,25) : ordres de grandeur publiés, pas une mesure exacte.

//
// Foot : le déroulement du match compte aussi. Les tirs, tirs cadrés et corners de chaque équipe (tableau des
// scores ESPN) donnent une mesure grossière des occasions créées, convertie en « buts mérités ». Ce rythme observé
// est mélangé à celui d'avant-match : l'avant-match pèse comme 2 matchs entiers, donc à la mi-temps les tirs
// comptent pour environ 20 %, en fin de match pour un tiers (30 minutes de tirs, c'est encore très bruité). Une équipe qui domine voit ses chances monter
// même sans avoir marqué. NHL : ESPN ne donne pas les tirs du match en direct, seuls le score et le temps comptent.

export const FOOT_LENGTH = 97;      // minutes réellement jouées en moyenne, temps additionnel compris
export const NHL_LENGTH = 60;
const RED_SELF = 0.7, RED_OPP = 1.25;
// valeur moyenne en buts : calée pour qu'une équipe moyenne (12-13 tirs dont 4-5 cadrés, 5 corners) vaille ~1,35 but
const XG_SOT = 0.22, XG_OFF = 0.04, XG_CORNER = 0.02;
const PRIOR_WEIGHT = 2;             // poids de l'avant-match, en matchs entiers
const DOMINATES = 0.68;             // part des occasions à partir de laquelle une équipe « domine »

export const norm = s => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();

function poisson(lam, kmax = 12){
  const out = [];
  let p = Math.exp(-lam);
  for (let k = 0; k <= kmax; k++){ out.push(p); p *= lam / (k + 1); }
  return out;
}

// Libellé court de l'état d'un match ESPN (« 67' », « Mi-temps », « 2e pér. 12:34 », « Terminé »…).
export function liveLabel(st, sport){
  const t = st.type || {}, name = t.name || "", short = t.shortDetail || "";
  if (t.state === "post"){
    if (/POSTPONED|CANCEL|ABANDON/.test(name)) return "Reporté";
    if (sport === "nhl") return /SO/.test(short) ? "Terminé (t.a.b.)" : /OT/.test(short) ? "Terminé (prol.)" : "Terminé";
    return "Terminé";
  }
  if (t.state !== "in") return "";
  // attention : « STATUS_SECOND_HALF » contient aussi HALF, seul HALFTIME désigne la mi-temps
  if (sport === "foot") return /HALFTIME/.test(name) || short === "HT" ? "Mi-temps" : (st.displayClock || "En direct");
  if (/END_PERIOD|INTERMISSION/.test(name)) return st.period >= 3 ? "Fin 3e pér." : `Pause ${st.period}e`;
  return `${st.period > 3 ? "Prol." : st.period + "e pér."} ${st.displayClock || ""}`.trim();
}

// Lit un événement du tableau des scores ESPN : score, temps écoulé, cartons rouges, buteurs.
export function parseEspn(e, sport){
  const c = (e.competitions || [])[0] || {};
  const comp = c.competitors || [];
  const H = comp.find(x => x.homeAway === "home") || {}, A = comp.find(x => x.homeAway === "away") || {};
  const st = e.status || {}, t = st.type || {}, name = t.name || "";
  const s = {
    state: t.state || "pre", sh: Number(H.score) || 0, sa: Number(A.score) || 0, period: st.period || 0,
    label: liveLabel(st, sport), cancelled: t.state === "post" && /POSTPONED|CANCEL|ABANDON/.test(name),
    red: {h: 0, a: 0}, goals: [], elapsed: 0,
  };
  if (sport === "foot"){
    const m = /(\d+)'(?:\s*\+\s*(\d+))?/.exec(st.displayClock || "");
    const minute = m ? +m[1] + (+m[2] || 0) : (st.clock || 0) / 60;
    // temps réellement écoulé, sur ~97 minutes : 47 en première période (additionnel compris), puis 50
    if (/HALFTIME/.test(name) || t.shortDetail === "HT") s.elapsed = 47;
    else if ((st.period || 1) <= 1) s.elapsed = Math.min(minute, 50);
    else s.elapsed = 47 + Math.max(0, minute - 45);
    const stat = x => {
      const v = Object.fromEntries((x.statistics || []).map(t => [t.name, Number(t.displayValue)]));
      return {shots: v.totalShots, sot: v.shotsOnTarget, corners: v.wonCorners, poss: v.possessionPct};
    };
    const sh = stat(H), sa = stat(A);
    if (t.state !== "pre" && Number.isFinite(sh.shots) && Number.isFinite(sa.shots)) s.stats = {h: sh, a: sa};
    const hid = String(H.id || (H.team || {}).id || ""), aid = String(A.id || (A.team || {}).id || "");
    for (const d of c.details || []){
      const tid = String((d.team || {}).id || "");
      const side = tid && tid === hid ? "h" : tid && tid === aid ? "a" : null;
      if (!side) continue;
      if (d.redCard) s.red[side]++;
      if (d.scoringPlay) s.goals.push({side, name: ((d.athletesInvolved || [])[0] || {}).displayName || "",
                                       own: !!d.ownGoal, pen: !!d.penaltyKick, clock: (d.clock || {}).displayValue || ""});
    }
  } else {
    const p = st.period || 0;
    if (/END_PERIOD|INTERMISSION/.test(name)) s.elapsed = Math.min(p, 3) * 20;
    else {
      const m = /(\d+):(\d+)/.exec(st.displayClock || "");
      const left = m ? +m[1] + +m[2] / 60 : 20;
      s.elapsed = p >= 4 ? 60 : Math.max(0, (p - 1) * 20 + (20 - left));
    }
  }
  if (s.state === "pre") s.elapsed = 0;
  return s;
}

// Occasions créées par une équipe, en buts « mérités » (voir XG_SOT…).
export function chances(t){
  if (!t) return 0;
  const sot = t.sot || 0, off = Math.max(0, (t.shots || 0) - sot);
  return XG_SOT * sot + XG_OFF * off + XG_CORNER * (t.corners || 0);
}

// Foot : buts attendus d'ici la fin pour chaque équipe (lh, la), d'après l'avant-match, le temps qui reste,
// les cartons rouges et les occasions déjà créées. dom = équipe qui domine nettement ("h", "a" ou null).
export function liveRates(m, s){
  const played = Math.min(1, (s.elapsed || 0) / FOOT_LENGTH);
  const rem = s.state === "post" ? 0 : Math.max(0.01, 1 - played);
  const red = s.red || {h: 0, a: 0};
  let rh = m.xgH || 0, ra = m.xgA || 0, dom = null;
  const st = s.stats;
  if (st && played > 0){
    const ch = chances(st.h), ca = chances(st.a);
    rh = (rh * PRIOR_WEIGHT + ch) / (PRIOR_WEIGHT + played);
    ra = (ra * PRIOR_WEIGHT + ca) / (PRIOR_WEIGHT + played);
    if (ch + ca >= 0.6) dom = ch / (ch + ca) >= DOMINATES ? "h" : ca / (ch + ca) >= DOMINATES ? "a" : null;
  }
  return {rem, dom, lh: rh * rem * RED_SELF ** red.h * RED_OPP ** red.a, la: ra * rem * RED_SELF ** red.a * RED_OPP ** red.h};
}

// Probabilités en direct de chaque marché, à partir du score actuel et du temps restant.
export function liveProbs(m, s){
  const done = s.state === "post";
  if (m.sport === "foot"){
    const {rem, lh, la} = liveRates(m, s);
    const ph = poisson(lh), pa = poisson(la);
    let p1 = 0, pN = 0, p2 = 0, o25 = 0, bt = 0, tot = 0;
    for (let i = 0; i < ph.length; i++) for (let j = 0; j < pa.length; j++){
      const w = ph[i] * pa[j], fh = s.sh + i, fa = s.sa + j;
      tot += w;
      if (fh > fa) p1 += w; else if (fh === fa) pN += w; else p2 += w;
      if (fh + fa >= 3) o25 += w;
      if (fh > 0 && fa > 0) bt += w;
    }
    [p1, pN, p2, o25, bt] = [p1, pN, p2, o25, bt].map(x => x / tot);
    return {rem, p1, pN, p2, o25, btts: bt,
            markets: {"1": p1, "2": p2, "1N": p1 + pN, "N2": pN + p2, "12": p1 + p2,
                      o25, u25: 1 - o25, btts_y: bt, btts_n: 1 - bt}};
  }
  // NHL : 60 minutes, puis prolongation / tirs au but en cas d'égalité
  const rem = done || s.period >= 4 ? 0 : Math.max(0, (NHL_LENGTH - (s.elapsed || 0)) / NHL_LENGTH);
  // chance de l'équipe à domicile de gagner une prolongation, déduite de l'analyse d'avant-match
  let q = 0.5;
  if (m.pD > 0 && m.winH != null && m.pH != null) q = Math.min(0.75, Math.max(0.25, (m.winH - m.pH) / m.pD));
  const ph = poisson((m.xgH || 0) * rem), pa = poisson((m.xgA || 0) * rem);
  let h = 0, d = 0, a = 0, o55 = 0, tot = 0;
  for (let i = 0; i < ph.length; i++) for (let j = 0; j < pa.length; j++){
    const w = ph[i] * pa[j], fh = s.sh + i, fa = s.sa + j;
    tot += w;
    if (fh > fa) h += w; else if (fh === fa) d += w; else a += w;
    if (fh + fa >= 6) o55 += w;    // une égalité donne un total pair : le but de la prolongation ne change rien
  }
  [h, d, a, o55] = [h, d, a, o55].map(x => x / tot);
  const winH = done ? (s.sh > s.sa ? 1 : 0) : h + d * q;
  return {rem, pH: h, pD: d, pA: a, winH, q, markets: {win_h: winH, win_a: 1 - winH, o55, u55: 1 - o55}};
}

// Probabilité en direct d'un pronostic. null = impossible à suivre en direct (vérifié après le match).
export function pickProb(pk, m, s, lp){
  if (!pk) return null;
  if (pk.market in lp.markets) return lp.markets[pk.market];
  if ((pk.market === "scorer" || pk.market === "dec") && m.sport === "foot"){
    if (scored(pk.player, s)) return 1;
    if (s.state === "post") return pk.market === "scorer" ? 0 : null;     // passes décisives : vérifiées après le match
    return 1 - (1 - pk.p) ** lp.rem;        // s'il est encore sur le terrain
  }
  return null;
}

// Résultat d'un pronostic une fois le match fini : true (validé), false (pas passé), null (pas vérifiable ici).
// Mêmes règles que resolve_picks dans app.py.
export function settle(pk, m, s){
  if (!pk || !s || s.state !== "post" || s.cancelled) return null;
  const h = s.sh, a = s.sa, t = h + a, mk = pk.market || "";
  const line = /^([ou])(\d)5$/.exec(mk);                 // o25 = plus de 2,5 buts, u35 = moins de 3,5 buts…
  if (line) return line[1] === "o" ? t > +line[2] : t <= +line[2];
  if (mk === "exact") return pk.value === `${h}-${a}`;
  const combo = /^w([ha])_(btts|nil|by1|o(\d)5)$/.exec(mk);     // victoire + autre condition (paris fun)
  if (combo){
    const [mine, other] = combo[1] === "h" ? [h, a] : [a, h];
    if (!(mine > other)) return false;
    return combo[2] === "btts" ? other > 0 : combo[2] === "nil" ? other === 0 : combo[2] === "by1" ? mine - other === 1 : t > +combo[3];
  }
  if (mk === "h3" || mk === "a3") return mk === "h3" ? h - a >= 3 : a - h >= 3;
  if (m.sport === "foot"){
    const r = {"1": h > a, "2": a > h, X: h === a, "1N": h >= a, N2: a >= h, "12": h !== a,
               btts_y: h > 0 && a > 0, btts_n: h === 0 || a === 0, h2: h - a >= 2, a2: a - h >= 2,
               cs_h: a === 0, cs_a: h === 0, btts_o25: h > 0 && a > 0 && t > 2};
    if (mk in r) return r[mk];
    const goals = s.goals || [];
    if (mk === "htft_h" || mk === "htft_a"){                 // mène à la mi-temps (buts jusqu'à 45'+) et gagne
      const first = goals.filter(g => (parseInt(g.clock, 10) || 99) <= 45);
      const hh = first.filter(g => g.side === "h").length, ha = first.length - hh;
      return mk === "htft_h" ? hh > ha && h > a : ha > hh && a > h;
    }
    if (mk === "scw" || mk === "sc2"){
      const n = goals.filter(g => !g.own && scored(pk.player, {goals: [g]})).length;
      if (mk === "sc2") return n >= 2;
      return pk.side ? n >= 1 && (pk.side === "h" ? h > a : a > h) : null;
    }
    if (mk === "fg_h" || mk === "fg_a") return t === 0 ? false : goals.length ? goals[0].side === mk.slice(-1) : null;
    if (mk === "scorer") return t === 0 || goals.length ? scored(pk.player, s) : null;
    if (mk === "dec") return scored(pk.player, s) ? true : null;    // sans but : il faut les passes (Bilan)
    return null;
  }
  const ot = (s.period || 0) >= 4;                        // prolongation ou tirs au but
  const r = {win_h: h > a, win_a: a > h, ot, reg_h: h > a && !ot, reg_a: a > h && !ot, h2: h - a >= 2, a2: a - h >= 2};
  return mk in r ? r[mk] : null;
}

export function scored(player, s){
  const want = norm(player), last = want.split(" ").pop();
  return (s.goals || []).some(g => !g.own && (norm(g.name) === want || (last.length > 3 && norm(g.name).split(" ").pop() === last)));
}

export const pct = p => Math.round(p * 100);
export const fair = p => (1 / Math.max(p, 0.01)).toFixed(2).replace(".", ",");      // cote juste, sans marge

// ---- cohérence : tous les pronostics d'un match racontent la même histoire.
// Événement d'un pari sur le score final (h, a) : 1 s'il passe, 0 sinon (NHL : q = part des prolongations gagnées
// à domicile). Même table que market_fn dans app.py. null = pas un pari sur le score.
export function marketTest(sport, mk, q = 0.5){
  const line = /^([ou])(\d)5$/.exec(mk || ""), ot = sport === "nhl" ? 1 : 0;
  if (line){ const k = +line[2]; return line[1] === "o" ? (h, a) => +(h + a + ot * (h === a) > k) : (h, a) => +(h + a + ot * (h === a) <= k); }
  const combo = /^w([ha])_(btts|nil|by1|o(\d)5)$/.exec(mk || "");
  if (combo) return (h, a) => {
    const [mine, other] = combo[1] === "h" ? [h, a] : [a, h];
    const win = mine > other ? 1 : sport === "nhl" && h === a ? (combo[1] === "h" ? q : 1 - q) : 0;
    return combo[2] === "btts" ? win * (other > 0) : combo[2] === "nil" ? win * (other === 0)
         : combo[2] === "by1" ? +(mine - other === 1) : win * (h + a + ot * (h === a) > +combo[3]);
  };
  if (mk === "h3" || mk === "a3") return mk === "h3" ? (h, a) => +(h - a >= 3) : (h, a) => +(a - h >= 3);
  if (mk === "btts_o25") return (h, a) => +(h > 0 && a > 0 && h + a > 2);
  const T = sport === "nhl"
    ? {win_h: (h, a) => h > a ? 1 : h === a ? q : 0, win_a: (h, a) => a > h ? 1 : h === a ? 1 - q : 0,
       reg_h: (h, a) => +(h > a), reg_a: (h, a) => +(a > h), ot: (h, a) => +(h === a),
       h2: (h, a) => +(h - a >= 2), a2: (h, a) => +(a - h >= 2)}
    : {"1": (h, a) => +(h > a), "2": (h, a) => +(a > h), X: (h, a) => +(h === a), "1N": (h, a) => +(h >= a), N2: (h, a) => +(a >= h),
       "12": (h, a) => +(h !== a), btts_y: (h, a) => +(h > 0 && a > 0), btts_n: (h, a) => +(h === 0 || a === 0),
       h2: (h, a) => +(h - a >= 2), a2: (h, a) => +(a - h >= 2), cs_h: (h, a) => +(a === 0), cs_a: (h, a) => +(h === 0)};
  return T[mk] || null;
}

// Avant le match, l'histoire, c'est l'analyse : son Safe et son Vainqueur. Pendant le match, c'est l'équipe qui a
// le plus de chances de gagner à cet instant (score, minute, tirs, cartons) ; à égalité ou presque (moins de
// 3 points d'écart), on garde le Vainqueur de l'analyse.
export function storyAnchors(m, s){
  const pk = m.picks || {};
  if (s.state !== "in") return [pk.main, pk.winner].filter(x => x && x.market).map(x => x.market);
  const lp = liveProbs(m, s), pre = (pk.winner || {}).market;
  const [ph, pa, H, A] = m.sport === "foot" ? [lp.p1, lp.p2, "1", "2"] : [lp.winH, 1 - lp.winH, "win_h", "win_a"];
  if (Math.abs(ph - pa) < 0.03) return [H, A].includes(pre) ? [pre] : [];
  return [ph > pa ? H : A];
}

// ---- paris « fun » : de vrais paris à cote (cote juste d'au moins ~2,4, soit 18 à 42 %), recalculés pendant le
// match. Sur ces marchés, la marge des bookmakers est forte (15 à 20 % en mi-temps/fin de match) : sous 42 %, la
// cote réelle reste en général au-dessus de 2. Mêmes paris que fun_picks dans app.py (sauf mi-temps/fin de match
// et paris joueur, calculés avant le match seulement).
export const FUN_LO = 0.18, FUN_HI = 0.42;
const ENG_SHIFT = 0.25;     // NHL : part des victoires d'un but qui deviennent +2 grâce au but dans la cage vide
// Même modèle que liveProbs : buts restants selon le temps qui reste, les cartons rouges et (foot) les tirs.
// anchors : pronostics que le pari ne doit pas contredire (marchés comme "1N", ou paris déjà choisis) : quand le
// pari passe, chacun d'eux doit avoir au moins autant de chances de passer (corrélation positive ou nulle).
// Un pari qui profite à l'équipe en supériorité numérique est mis en avant.
// dedupe : refuser aussi le même marché qu'un appui (avant le match : jamais le Safe ou le Vainqueur sous un autre nom).
// fun : paris fun seulement (combinés, écarts, buts) ; sans fun, tous les paris (sert aussi au Safe du moment).
export function funPicks(m, s, {lo = FUN_LO, hi = FUN_HI, max = 3, anchors = [], exclude = [], dedupe = true, fun = false} = {}){
  const out = [], H = m.home, A = m.away, n = s.sh + s.sa;
  const red = s.red || {h: 0, a: 0};
  const adv = red.h < red.a ? "h" : red.a < red.h ? "a" : null;     // équipe à 11 contre 10
  let rem, lh, la, dom = null, q = 0.5;
  if (s.state === "post" || s.cancelled) return [];
  if (m.sport === "foot"){
    ({rem, lh, la, dom} = liveRates(m, s));
  } else {
    rem = s.period >= 4 ? 0 : Math.max(0, (NHL_LENGTH - (s.elapsed || 0)) / NHL_LENGTH);
    lh = (m.xgH || 0) * rem; la = (m.xgA || 0) * rem;
    if (m.pD > 0 && m.winH != null && m.pH != null) q = Math.min(0.75, Math.max(0.25, (m.winH - m.pH) / m.pD));
  }
  const ph = poisson(lh), pa = poisson(la);
  // f(h, a, i, j) : score final h-a, dont i et j buts encore à marquer ; renvoie 0, 1 ou une probabilité
  const P = f => {
    let t = 0, z = 0;
    for (let i = 0; i < ph.length; i++) for (let j = 0; j < pa.length; j++){
      const w = ph[i] * pa[j]; z += w;
      t += w * f(s.sh + i, s.sa + j, i, j);
    }
    return t / z;
  };
  const tests = anchors.map(x => typeof x === "function" ? x : typeof x === "string" ? marketTest(m.sport, x, q)
                                  : x && x.test ? x.test : x && x.market ? marketTest(m.sport, x.market, q) : null).filter(Boolean);
  const anchorMk = new Set(dedupe ? anchors.map(x => typeof x === "string" ? x : x && x.market).filter(Boolean) : []);
  const coherent = (f, p) => tests.every(g => P((h, a, i, j) => g(h, a, i, j) * f(h, a, i, j)) >= P(g) * p - 1e-9);
  const domWhy = side => { const t = s.stats[side]; return `📈 ${side === "h" ? H : A} domine (${t.shots} tirs, ${t.sot} cadrés)`; };
  const add = (fam, label, f, side = null, market = null, prob = null) => {
    const p = prob == null ? P(f) : prob;
    if (!(p >= lo && p <= hi) || exclude.includes(label) || (market && anchorMk.has(market)) || !coherent(f, p)) return;
    const num = adv && side === adv;
    out.push({fam, label, p, market, test: f, red: !!num, why: num ? "🟥 Supériorité numérique" : dom && side === dom ? domWhy(dom) : ""});
  };
  const mk = (sport, code) => (h, a) => marketTest(sport, code, q)(h, a);
  const goals = k => `${k + 0.5}`.replace(".", ",");
  const line = (o, k) => k <= 9 ? `${o}${k}5` : null;
  // prochain but : chaque but à venir est de l'équipe à domicile avec la probabilité lh / (lh + la)
  const nextH = (h, a, i, j) => i + j ? i / (i + j) : 0, nextA = (h, a, i, j) => i + j ? j / (i + j) : 0;
  const names = {h: H, a: A};
  if (m.sport === "foot" && fun){
    for (const sd of ["h", "a"]){
      const N = names[sd], other = sd === "h" ? s.sa : s.sh;
      if (other === 0) add("win_nil", `${N} gagne sans encaisser de but`, mk("foot", `w${sd}_nil`), sd, `w${sd}_nil`);
      if (other === 0) add("win_btts", `${N} gagne et les deux équipes marquent`, mk("foot", `w${sd}_btts`), sd, `w${sd}_btts`);
      if (n <= 2) add("win_o25", `${N} gagne et plus de 2,5 buts`, mk("foot", `w${sd}_o25`), sd, `w${sd}_o25`);
      add("by1", `${N} gagne d'un seul but`, mk("foot", `w${sd}_by1`), sd, `w${sd}_by1`);
      add("margin3", `${N} gagne avec 3 buts d'écart ou plus`, mk("foot", `${sd}3`), sd, `${sd}3`);
    }
    if (!(s.sh > 0 && s.sa > 0) && n <= 2) add("btts_o25", "Les deux équipes marquent et plus de 2,5 buts", mk("foot", "btts_o25"), null, "btts_o25");
    add("draw", "Match nul", mk("foot", "X"), null, "X");
  }
  if (m.sport === "foot"){
    if (!fun) for (const [fam, code, label, side] of [["res", "1", `Victoire ${H}`, "h"], ["res", "2", `Victoire ${A}`, "a"], ["res", "X", "Match nul", null],
                                            ["dc", "1N", `${H} ou nul`, "h"], ["dc", "N2", `Nul ou ${A}`, "a"]])
      add(fam, label, mk("foot", code), side, code);
    add("margin", `${H} gagne avec 2 buts d'écart ou plus`, mk("foot", "h2"), "h", "h2");
    add("margin", `${A} gagne avec 2 buts d'écart ou plus`, mk("foot", "a2"), "a", "a2");
    for (const k of [n, n + 1, n + 2, n + 3]) if (!fun || k >= 2) add("ou", `Plus de ${goals(k)} buts`, (h, a) => +(h + a > k), null, line("o", k));
    if (!fun) for (const k of [n + 1, n + 2, n + 3]) add("under", `Moins de ${goals(k)} buts`, (h, a) => +(h + a <= k), null, line("u", k));
    if (!(s.sh > 0 && s.sa > 0)) add("btts", "Les deux équipes marquent", mk("foot", "btts_y"), null, "btts_y");
    add("next", `${n ? "Prochain" : "Premier"} but : ${H}`, nextH, "h", n ? null : "fg_h");
    add("next", `${n ? "Prochain" : "Premier"} but : ${A}`, nextA, "a", n ? null : "fg_a");
    if (!fun){            // pari simple : en mode fun, « X gagne sans encaisser de but » le remplace
      add("cs", `${A} ne marque plus`, (h, a, i, j) => +(j === 0), "h", s.sa === 0 ? "cs_h" : null);
      add("cs", `${H} ne marque plus`, (h, a, i, j) => +(i === 0), "a", s.sh === 0 ? "cs_a" : null);
    }
  } else {
    // prolongation : le vainqueur marque 1 but de plus (une égalité donne un total pair)
    if (!fun){
      add("res", `Victoire ${H} (prolongation comprise)`, mk("nhl", "win_h"), "h", "win_h");
      add("res", `Victoire ${A} (prolongation comprise)`, mk("nhl", "win_a"), "a", "win_a");
    } else for (const sd of ["h", "a"]){
      const N = names[sd], d = (h, a) => sd === "h" ? h - a : a - h;
      for (const k of [5, 6]) if (n <= k) add(`win_o${k}5`, `${N} gagne et plus de ${k},5 buts`, mk("nhl", `w${sd}_o${k}5`), sd, `w${sd}_o${k}5`);
      add("puckline", `${N} gagne par 2 buts d'écart ou plus`, (h, a) => +(d(h, a) >= 2), sd, `${sd}2`,
          P((h, a) => +(d(h, a) >= 2)) + ENG_SHIFT * P((h, a) => +(d(h, a) === 1)) * (s.period < 4 ? 1 : 0));
    }
    add("reg", `${H} gagne dans le temps réglementaire`, mk("nhl", "reg_h"), "h", "reg_h");
    add("reg", `${A} gagne dans le temps réglementaire`, mk("nhl", "reg_a"), "a", "reg_a");
    if (s.period < 4) add("ot", "Il y aura prolongation", mk("nhl", "ot"), null, "ot");
    const b = Math.max(n, 4);
    for (const k of [b, b + 1, b + 2]) add("ou", `Plus de ${goals(k)} buts`, (h, a) => +(h + a + (h === a) > k), null, line("o", k));
    for (const k of [b + 1, b + 2, b + 3]) add("under", `Moins de ${goals(k)} buts`, (h, a) => +(h + a + (h === a) <= k), null, line("u", k));
    add("next", `${n ? "Prochain" : "Premier"} but : ${H}`, nextH, "h");
    add("next", `${n ? "Prochain" : "Premier"} but : ${A}`, nextA, "a");
  }
  // un seul pari par famille ; la supériorité numérique passe devant, puis le plus probable
  const score = x => (x.red ? 0.15 : 0) + x.p;
  const best = {};
  for (const x of out.sort((a, b) => score(b) - score(a))) best[x.fam] ||= x;
  return Object.values(best).sort((a, b) => score(b) - score(a)).slice(0, max);
}

// Pronostics du moment d'un match : un Safe (75 à 90 %, pendant le match) puis un Fun (cote ≥ ~2,4) qui ne
// contredit ni l'histoire du match (storyAnchors) ni ce Safe. Jamais le Safe d'avant-match en double.
export function momentPicks(m, s){
  if (s.state === "post" || s.cancelled) return {safe: null, fun: null};
  const anchors = storyAnchors(m, s), exclude = [((m.picks || {}).main || {}).label].filter(Boolean);
  const live = s.state === "in";       // en direct, le Safe peut être la victoire de l'équipe favorite
  const safe = live ? funPicks(m, s, {lo: 0.75, hi: 0.90, max: 1, anchors, exclude, dedupe: false})[0] || null : null;
  const fun = funPicks(m, s, {max: 1, fun: true, anchors: safe ? [...anchors, safe] : anchors, dedupe: !live,
                              exclude: safe ? [...exclude, safe.label] : exclude})[0] || null;
  return {safe, fun};
}
