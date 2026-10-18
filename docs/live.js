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

export function scored(player, s){
  const want = norm(player), last = want.split(" ").pop();
  return (s.goals || []).some(g => !g.own && (norm(g.name) === want || (last.length > 3 && norm(g.name).split(" ").pop() === last)));
}

export const pct = p => Math.round(p * 100);
