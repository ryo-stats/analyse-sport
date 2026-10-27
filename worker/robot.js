// Logique du robot d'alertes, sans dépendance à Cloudflare (testable avec Node : node worker/test.mjs).
//
// Site ouvert à tous : chaque téléphone a SES favoris (15 au plus) et ne reçoit que les alertes de SES matchs.
// Chaque minute : relit les analyses (live.json, toutes les 10 min), puis pour chaque match en favori d'au moins
// une personne :
//   - but (et but annulé par la vidéo) : score, buteur, pronostics recalculés ;
//   - fin du match : chaque pronostic gagné ou perdu.
// Plus un résumé vers 8 h (heure de Paris), seulement pour ceux qui l'ont activé.
import * as L from "../docs/live.js";

const MIN = 60e3, HOUR = 3600e3;
export const ESPN = "https://site.api.espn.com/apis/site/v2/sports/";
export const MAX_FAVS = 15;
// adresses d'abonnement acceptées : les vrais services de notification des navigateurs (Google, Apple, Mozilla, Microsoft)
export const PUSH_HOSTS = /^https:\/\/(fcm\.googleapis\.com|android\.googleapis\.com|web\.push\.apple\.com|updates\.push\.services\.mozilla\.com|[\w.-]+\.notify\.windows\.com)\//;

export function feedUrl(m){
  return m.sport === "foot" ? `${ESPN}soccer/${m.code}/scoreboard?dates=${m.edate}`
                            : `${ESPN}hockey/nhl/scoreboard?dates=${m.edate}`;
}

function findEvent(m, events){
  if (m.sport === "foot") return events.find(e => "foot-" + e.id === m.id);
  const byId = events.find(e => m.espn_id && String(e.id) === String(m.espn_id));
  if (byId) return byId;
  return events.find(e => {
    const c = (e.competitions || [])[0] || {};
    const h = (c.competitors || []).find(x => x.homeAway === "home"), a = (c.competitors || []).find(x => x.homeAway === "away");
    return h && a && L.norm(h.team.displayName) === L.norm(m.home) && L.norm(a.team.displayName) === L.norm(m.away);
  });
}

// Pronostics suivis pour un match : le plus sûr, le vainqueur (s'il diffère), le buteur (foot).
// Les pronostics joueur NHL ne sont pas suivis en direct (le tableau des scores ESPN ne donne pas les passeurs).
export function followedPicks(m){
  const pk = m.picks || {}, out = [];
  if (pk.main) out.push(["main", pk.main]);
  if (pk.winner && (!pk.main || pk.winner.market !== pk.main.market)) out.push(["winner", pk.winner]);
  if (m.sport === "foot" && pk.player) out.push(["player", pk.player]);
  return out;
}

// Bilan de fin de match : tous les pronostics affichés sur le site, Fun et score exact compris.
const KIND = {main: "Safe", winner: "Vainqueur", player: "Joueur", decisive: "Joueur", fun: "Fun 🎲", score: ""};
export function finalPicks(m){
  const pk = m.picks || {}, out = followedPicks(m);
  if (m.sport !== "foot" && pk.player) out.push(["player", pk.player]);
  if (pk.decisive) out.push(["decisive", pk.decisive]);
  if (pk.fun) out.push(["fun", pk.fun]);
  if (pk.score && pk.score.market) out.push(["score", pk.score]);
  return out;
}

function finalBody(m, s){
  const res = finalPicks(m).map(([k, pk]) => ({k, pk, ok: L.settle(pk, m, s)}));
  const known = res.filter(x => x.ok != null);
  const head = known.length ? `${known.filter(x => x.ok).length}/${known.length} pronostic${known.length > 1 ? "s" : ""} validé${known.length > 1 ? "s" : ""}` : "";
  return [head, ...res.map(x => `${x.ok == null ? "•" : x.ok ? "✅" : "❌"} ${KIND[x.k] ? KIND[x.k] + " : " : ""}${x.pk.label}${x.ok == null ? " (résultat dans le Bilan)" : ""}`)]
    .filter(Boolean).join("\n");
}

// Pronostics du moment (mêmes calculs que le site) : un « safe » (75 à 90 %) et un « fun » (cote juste ≥ ~2,4),
// d'après le score, la minute, les cartons et (foot) les tirs ; le Fun ne contredit ni le Safe ni l'équipe favorite.
export function momentIdeas(m, s){
  const {safe, fun} = L.momentPicks(m, s);
  return [safe && `💡 Safe du moment : ${safe.label} (${L.pct(safe.p)} %)`,
          fun && `🎲 Fun du moment : ${fun.label} (${L.pct(fun.p)} %)`].filter(Boolean);
}

export function parisNow(now){
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone: "Europe/Paris", year: "numeric",
    month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23"}).formatToParts(new Date(now)).map(p => [p.type, p.value]));
  return {date: `${parts.year}-${parts.month}-${parts.day}`, hour: Number(parts.hour)};
}

function morning(sum){
  const y = sum.yesterday || {};
  const lines = [y.n ? `Hier : ${y.ok} pronostic${y.ok > 1 ? "s" : ""} réussi${y.ok > 1 ? "s" : ""} sur ${y.n}.` : "Hier : aucun pronostic vérifié.",
                 `Aujourd'hui : ${sum.foot} match${sum.foot > 1 ? "s" : ""} de foot, ${sum.nhl} match${sum.nhl > 1 ? "s" : ""} NHL.`];
  if ((sum.top || []).length){
    lines.push("Les plus sûrs :");
    for (const t of sum.top) lines.push(`• ${t.label} (${t.match}, ${t.time}) : ${t.prob} %`);
  }
  return {title: "☀️ Résumé du matin", body: lines.join("\n"), priority: 2, morning: true};
}

// Compare le nouvel état d'un match au précédent et renvoie les alertes à envoyer (buts et fin du match).
export function update(w, s){
  const m = w.m, out = [];
  if (s.cancelled){
    if (!w.done) out.push({match: m.id, title: `⏸️ Reporté : ${m.home} – ${m.away}`, body: "Plus d'alerte pour ce match.", priority: 3});
    w.done = true;
    return out;
  }
  if (s.state === "pre") return out;
  const prev = w.last;
  w.last = {sh: s.sh, sa: s.sa, state: s.state, goals: s.goals.length};
  const lp = L.liveProbs(m, s);
  const picks = followedPicks(m).map(([k, pk]) => ({k, pk, p: L.pickProb(pk, m, s, lp)}));
  const before = x => w.lastP[x.k] ?? x.pk.p;
  const commit = () => picks.forEach(x => { if (x.p != null) w.lastP[x.k] = x.p; });
  if (!prev){                 // suivi commencé en cours de match : point de départ, pas d'alerte rétroactive
    commit();
    if (s.state === "post") w.done = true;
    return out;
  }
  const score = `${m.home} ${s.sh}-${s.sa} ${m.away}`;
  if (s.state === "post"){
    w.done = true;
    out.push({match: m.id, title: `🏁 Terminé : ${score}`, priority: 3, body: finalBody(m, s)});
    commit();
    return out;
  }
  const line = x => x.p == null ? `• ${x.pk.label}`
    : `• ${x.pk.label} : ${L.pct(before(x))} % → ${L.pct(x.p)} %${x.p >= 0.9999 ? " ✅" : ""}`;
  const total = s.sh + s.sa, prevTotal = prev.sh + prev.sa;
  if (total > prevTotal){
    const team = s.sh > prev.sh && s.sa > prev.sa ? "" : s.sh > prev.sh ? ` ${m.home}` : ` ${m.away}`;
    const who = s.goals.slice(prev.goals || 0).map(g => `${g.clock} ${g.name}${g.own ? " (contre son camp)" : g.pen ? " (penalty)" : ""}`.trim()).filter(Boolean);
    out.push({match: m.id, title: `${m.sport === "nhl" ? "🏒" : "⚽"} But${team} ! ${score}`, priority: 4,
              body: [who.length ? who.join(", ") : s.label, ...picks.map(line), ...momentIdeas(m, s)].filter(Boolean).join("\n")});
    commit();
  } else if (total < prevTotal){
    out.push({match: m.id, title: `↩️ But annulé : ${score}`, priority: 3,
              body: [s.label, ...picks.map(line), ...momentIdeas(m, s)].filter(Boolean).join("\n")});
    commit();
  }
  return out;
}

function note(st, now, text){
  st.errors = [{t: new Date(now).toISOString(), text}, ...(st.errors || [])].slice(0, 15);
}

// Lecture tolérante : un deuxième essai tout de suite (coupure réseau, réponse lente), puis, pour ESPN, une adresse
// de secours sans date (le tableau des scores du jour). Un but manqué pendant une panne n'est pas perdu : le robot
// compare le score au dernier connu, donc il l'annonce dès qu'ESPN répond de nouveau.
async function readJSON(io, urls){
  let err;
  for (const url of urls){
    for (let i = 0; i < 2; i++){
      try { return await io.getJSON(url); } catch (e){ err = e; }
    }
  }
  throw err;
}

// santé du robot, visible sur /etat : depuis quand ESPN et les analyses répondent
function health(st, now, key, ok){
  st.health ||= {};
  const h = st.health[key] ||= {};
  if (ok){ h.ok = now; h.fails = 0; } else { h.fails = (h.fails || 0) + 1; h.lastFail = now; }
}
export function healthReport(st, now){
  const h = st.health || {}, mins = t => Math.round((now - t) / MIN);
  const espn = h.espn && h.espn.fails ? `en panne (${h.espn.fails} échecs, dernier succès il y a ${h.espn.ok ? mins(h.espn.ok) + " min" : "longtemps"})` : "ok";
  const gen = st.data && Date.parse(st.data.generated), age = gen ? (now - gen) / HOUR : null;
  const gh = h.github;
  return {espn, analyses: age == null ? "pas encore lues" : age > 3 ? `en retard (${age.toFixed(1).replace(".", ",")} h)` : "à jour",
          live_json: h.live && h.live.fails ? `en panne (${h.live.fails} échecs)` : "ok",
          relance_github: !gh ? "pas encore utilisée (ou jeton GH_TOKEN absent)" : gh.fails ? `en panne (${gh.fails} échecs)`
            : `ok (dernière il y a ${mins(gh.ok)} min)`};
}

// Relance des analyses (secours). GitHub retarde ou saute parfois ses lancements programmés : le 26/10, rien
// entre 0 h 21 et 12 h 16. Si les analyses publiées ont plus de 25 minutes, le robot demande un passage, au plus
// toutes les 14 minutes, de 6 h à 2 h (heure de Paris) ; horaires.py décide ensuite s'il faut vraiment recalculer.
// Ne fait rien sans io.dispatch (jeton GitHub absent).
export function relaunchDue(st, now){
  const hour = parisNow(now).hour;
  if (hour >= 2 && hour < 6) return false;
  if (now - (st.relaunch || 0) < 14 * MIN) return false;
  const gen = st.data && Date.parse(st.data.generated);
  return !gen || now - gen > 25 * MIN;
}
async function relaunch(st, now, io){
  if (!io.dispatch || !relaunchDue(st, now)) return;
  st.relaunch = now;
  try { await io.dispatch(); health(st, now, "github", true); }
  catch (e){ note(st, now, `relance GitHub : ${e.message}`); health(st, now, "github", false); }
}

// Un passage du robot. st : état conservé d'une minute à l'autre ; favs : {id du match: date d'ajout}, réunion
// des favoris de tous les téléphones. io.getJSON(url) lit une adresse ; io.site est l'adresse du site ;
// io.dispatch() (facultatif) demande un passage des analyses à GitHub.
// Renvoie {msgs, expired} : alertes (avec .match ou .morning) et favoris périmés à retirer partout.
export async function tick(st, favs, now, io){
  st.watch ||= {};
  const msgs = [];
  if (!st.data || now - st.data.fetched > 10 * MIN){
    try {
      const d = await readJSON(io, [`${io.site}live.json?t=${Math.floor(now / MIN)}`]);
      if (d && Array.isArray(d.matches)) st.data = {...d, fetched: now};
      health(st, now, "live", true);
    } catch (e){
      note(st, now, `live.json : ${e.message}`);
      health(st, now, "live", false);
      if (st.data) st.data.fetched = now - 5 * MIN;          // nouvel essai dans 5 minutes (on garde les anciennes analyses)
    }
  }
  const matches = (st.data || {}).matches || [];
  const byId = Object.fromEntries(matches.map(m => [m.id, m]));

  // matchs en favori d'au moins une personne -> matchs suivis (dès 15 min avant le coup d'envoi)
  for (const id of Object.keys(st.watch)) if (!favs[id]) delete st.watch[id];
  for (const id of Object.keys(favs)){
    const m = byId[id] || (st.watch[id] || {}).m;
    if (!m) continue;
    const k = Date.parse(m.kickoff);
    const w = st.watch[id];
    if (!w){
      if (k - now > 16 * MIN || now - k > 5 * HOUR) continue;
      // suivi commencé avant le coup d'envoi : on part de 0-0 pour ne rater aucun but
      st.watch[id] = {m, lastP: {}, done: false, last: now < k ? {sh: 0, sa: 0, state: "pre", goals: 0} : null};
    } else if (byId[id] && now < k) w.m = byId[id];          // analyses plus récentes avant le match
  }

  // matchs en cours : un appel ESPN par championnat
  const feeds = {};
  for (const w of Object.values(st.watch)){
    if (w.done || now < Date.parse(w.m.kickoff) - 3 * MIN) continue;
    if (now - Date.parse(w.m.kickoff) > 6 * HOUR){ w.done = true; continue; }
    (feeds[feedUrl(w.m)] ||= []).push(w);
  }
  for (const [url, list] of Object.entries(feeds)){
    let j;
    try {
      j = await readJSON(io, [url, url.split("?")[0]]);
      health(st, now, "espn", true);
    } catch (e){ note(st, now, `ESPN : ${e.message}`); health(st, now, "espn", false); continue; }
    for (const w of list){
      const e = findEvent(w.m, (j && j.events) || []);
      if (e) msgs.push(...update(w, L.parseEspn(e, w.m.sport)));
    }
  }

  // résumé du matin (une fois par jour, après la mise à jour de 7 h 20 ; jusqu'à 13 h si elle arrive en retard)
  const P = parisNow(now), sum = (st.data || {}).summary;
  if (P.hour >= 8 && P.hour < 13 && st.morning !== P.date && sum && sum.date === P.date){
    st.morning = P.date;
    msgs.push(morning(sum));
  }

  // favoris périmés, pour libérer la place (15 au plus) : 8 h après le coup d'envoi (match fini, alertes envoyées),
  // ou 24 h après l'ajout si le match n'est plus dans les analyses
  const expired = Object.keys(favs).filter(id => {
    const m = byId[id] || (st.watch[id] || {}).m;
    return m ? now - Date.parse(m.kickoff) > 8 * HOUR : now - favs[id] > 24 * HOUR;
  });
  for (const id of expired) delete st.watch[id];
  await relaunch(st, now, io);
  return {msgs, expired};
}

// À qui envoyer quoi : une alerte de match va aux seuls téléphones qui ont ce match en favori,
// le résumé du matin aux seuls téléphones qui l'ont activé. subs : {clé: {favs, morning}}.
export function route(msgs, subs){
  const out = [];
  for (const m of msgs){
    for (const [key, s] of Object.entries(subs)){
      if (m.match ? (s.favs || {})[m.match] : m.morning ? s.morning : false) out.push({key, msg: m});
    }
  }
  return out;
}
