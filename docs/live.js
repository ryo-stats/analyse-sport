// Pronostics en direct : recalcul des probabilités à partir du score, de la minute et des cartons rouges.
// Partagé entre le site (onglet Pronostics > En direct) et le robot d'alertes Cloudflare (worker/).
//
// Principe : avant le match, chaque équipe a un nombre de buts attendus (xgH, xgA) tiré des cotes.
// Pendant le match, on ne garde que la part du temps qui reste, on ajoute ces buts « à venir » au score
// actuel, et on recalcule chaque pronostic. Un carton rouge baisse les buts attendus de l'équipe réduite
// (×0,7) et augmente ceux de l'adversaire (×1,25) : ordres de grandeur publiés, pas une mesure exacte.

export const FOOT_LENGTH = 97;      // minutes réellement jouées en moyenne, temps additionnel compris
export const NHL_LENGTH = 60;
const RED_SELF = 0.7, RED_OPP = 1.25;

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

// Probabilités en direct de chaque marché, à partir du score actuel et du temps restant.
export function liveProbs(m, s){
  const done = s.state === "post";
  if (m.sport === "foot"){
    const rem = done ? 0 : Math.max(0.01, (FOOT_LENGTH - (s.elapsed || 0)) / FOOT_LENGTH);
    const red = s.red || {h: 0, a: 0};
    const lh = (m.xgH || 0) * rem * RED_SELF ** red.h * RED_OPP ** red.a;
    const la = (m.xgA || 0) * rem * RED_SELF ** red.a * RED_OPP ** red.h;
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
  if (pk.market === "scorer" && m.sport === "foot"){
    if (scored(pk.player, s)) return 1;
    if (s.state === "post") return 0;
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
  if (m.sport === "foot"){
    const r = {"1": h > a, "2": a > h, X: h === a, "1N": h >= a, N2: a >= h, "12": h !== a,
               btts_y: h > 0 && a > 0, btts_n: h === 0 || a === 0, h2: h - a >= 2, a2: a - h >= 2,
               cs_h: a === 0, cs_a: h === 0};
    if (mk in r) return r[mk];
    const goals = s.goals || [];
    if (mk === "fg_h" || mk === "fg_a") return t === 0 ? false : goals.length ? goals[0].side === mk.slice(-1) : null;
    if (mk === "scorer") return t === 0 || goals.length ? scored(pk.player, s) : null;
    return null;
  }
  const ot = (s.period || 0) >= 4;                        // prolongation ou tirs au but
  const r = {win_h: h > a, win_a: a > h, ot, reg_h: h > a && !ot, reg_a: a > h && !ot};
  return mk in r ? r[mk] : null;
}

export function scored(player, s){
  const want = norm(player), last = want.split(" ").pop();
  return (s.goals || []).some(g => !g.own && (norm(g.name) === want || (last.length > 3 && norm(g.name).split(" ").pop() === last)));
}

export const pct = p => Math.round(p * 100);
export const fair = p => (1 / Math.max(p, 0.01)).toFixed(2).replace(".", ",");      // cote juste, sans marge

// ---- paris « fun » : plus risqués (35 à 66 %) mais plausibles, recalculés pendant le match.
// Même modèle que liveProbs : buts restants selon le temps qui reste et les cartons rouges.
// Un pari qui profite à l'équipe en supériorité numérique est mis en avant.
export function funPicks(m, s, {lo = 0.35, hi = 0.66, max = 3} = {}){
  const out = [], H = m.home, A = m.away, n = s.sh + s.sa;
  const red = s.red || {h: 0, a: 0};
  const adv = red.h < red.a ? "h" : red.a < red.h ? "a" : null;     // équipe à 11 contre 10
  const add = (fam, label, p, side = null) => {
    if (!(p >= lo && p <= hi)) return;
    out.push({fam, label, p, why: adv && side === adv ? "🟥 Supériorité numérique" : ""});
  };
  if (s.state === "post" || s.cancelled) return [];
  let rem, lh, la, q = 0.5;
  if (m.sport === "foot"){
    rem = Math.max(0.01, (FOOT_LENGTH - (s.elapsed || 0)) / FOOT_LENGTH);
    lh = (m.xgH || 0) * rem * RED_SELF ** red.h * RED_OPP ** red.a;
    la = (m.xgA || 0) * rem * RED_SELF ** red.a * RED_OPP ** red.h;
  } else {
    rem = s.period >= 4 ? 0 : Math.max(0, (NHL_LENGTH - (s.elapsed || 0)) / NHL_LENGTH);
    lh = (m.xgH || 0) * rem; la = (m.xgA || 0) * rem;
    if (m.pD > 0 && m.winH != null && m.pH != null) q = Math.min(0.75, Math.max(0.25, (m.winH - m.pH) / m.pD));
  }
  const ph = poisson(lh), pa = poisson(la);
  const P = f => {
    let t = 0, z = 0;
    for (let i = 0; i < ph.length; i++) for (let j = 0; j < pa.length; j++){
      const w = ph[i] * pa[j]; z += w;
      t += w * f(s.sh + i, s.sa + j, i, j);        // f renvoie 0, 1 ou une probabilité (prolongation)
    }
    return t / z;
  };
  const more = 1 - ph[0] * pa[0];                   // au moins un but de plus
  const next = side => lh + la > 0 ? more * (side === "h" ? lh : la) / (lh + la) : 0;
  const goals = k => `${k + 0.5}`.replace(".", ",");
  if (m.sport === "foot"){
    add("res", `Victoire ${H}`, P((h, a) => h > a), "h");
    add("res", `Victoire ${A}`, P((h, a) => a > h), "a");
    add("res", "Match nul", P((h, a) => h === a));
    add("dc", `${H} ou nul`, P((h, a) => h >= a), "h");
    add("dc", `Nul ou ${A}`, P((h, a) => a >= h), "a");
    for (const k of [n, n + 1, n + 2]) add("ou", `Plus de ${goals(k)} buts`, P((h, a) => h + a > k));
    for (const k of [n + 1, n + 2, n + 3]) add("under", `Moins de ${goals(k)} buts`, P((h, a) => h + a <= k));
    if (!(s.sh > 0 && s.sa > 0)) add("btts", "Les deux équipes marquent", P((h, a) => h > 0 && a > 0));
    add("margin", `${H} gagne avec 2 buts d'écart ou plus`, P((h, a) => h - a >= 2), "h");
    add("margin", `${A} gagne avec 2 buts d'écart ou plus`, P((h, a) => a - h >= 2), "a");
    add("next", `${n ? "Prochain" : "Premier"} but : ${H}`, next("h"), "h");
    add("next", `${n ? "Prochain" : "Premier"} but : ${A}`, next("a"), "a");
    add("cs", `${A} ne marque plus`, P((h, a, i, j) => j === 0), "h");
    add("cs", `${H} ne marque plus`, P((h, a, i, j) => i === 0), "a");
  } else {
    // prolongation : le vainqueur marque 1 but de plus (une égalité donne un total pair)
    const winH = P((h, a) => h > a ? 1 : h === a ? q : 0);
    add("res", `Victoire ${H} (prolongation comprise)`, winH, "h");
    add("res", `Victoire ${A} (prolongation comprise)`, 1 - winH, "a");
    add("reg", `${H} gagne dans le temps réglementaire`, P((h, a) => h > a), "h");
    add("reg", `${A} gagne dans le temps réglementaire`, P((h, a) => a > h), "a");
    if (s.period < 4) add("ot", "Il y aura prolongation", P((h, a) => h === a));
    for (const k of [Math.max(n, 4), Math.max(n, 4) + 1, Math.max(n, 4) + 2])
      add("ou", `Plus de ${goals(k)} buts`, P((h, a) => h + a + (h === a ? 1 : 0) > k));
    for (const k of [Math.max(n, 4) + 1, Math.max(n, 4) + 2, Math.max(n, 4) + 3])
      add("under", `Moins de ${goals(k)} buts`, P((h, a) => h + a + (h === a ? 1 : 0) <= k));
    add("next", `${n ? "Prochain" : "Premier"} but : ${H}`, next("h"), "h");
    add("next", `${n ? "Prochain" : "Premier"} but : ${A}`, next("a"), "a");
  }
  // un seul pari par famille ; la supériorité numérique passe devant, puis le plus probable
  const score = x => (x.why ? 0.15 : 0) + x.p;
  const best = {};
  for (const x of out.sort((a, b) => score(b) - score(a))) best[x.fam] ||= x;
  return Object.values(best).sort((a, b) => score(b) - score(a)).slice(0, max);
}
