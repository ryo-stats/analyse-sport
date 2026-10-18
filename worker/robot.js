// Logique du robot d'alertes, sans dépendance à Cloudflare (testable avec Node : node worker/test.mjs).
//
// Chaque minute : relit les analyses (live.json, toutes les 10 min), puis pour chaque match en favori :
//   - rappel 15 min avant le coup d'envoi, avec les pronostics ;
//   - but, but annulé, carton rouge : score + pronostics recalculés ;
//   - pronostic qui bascule (±20 points, ou passage au-dessus de 95 % / sous 5 %) ;
//   - fin du match : chaque pronostic gagné ou perdu.
// Plus un résumé chaque matin vers 8 h (heure de Paris).
import * as L from "../docs/live.js";

const MIN = 60e3, HOUR = 3600e3;
export const ESPN = "https://site.api.espn.com/apis/site/v2/sports/";
const stars = n => "★".repeat(n) + "☆".repeat(5 - n);

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

export function parisNow(now){
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone: "Europe/Paris", year: "numeric",
    month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23"}).formatToParts(new Date(now)).map(p => [p.type, p.value]));
  return {date: `${parts.year}-${parts.month}-${parts.day}`, hour: Number(parts.hour)};
}

function reminder(list){
  const one = list.length === 1;
  const body = list.map(w => {
    const m = w.m;
    const picks = followedPicks(m).map(([, p]) => `• ${p.label} : ${p.prob} % ${stars(p.stars)}`).join("\n");
    return (one ? "" : `${m.time} ${m.home} – ${m.away}\n`) + picks;
  }).join("\n\n");
  const m = list[0].m;
  return {title: one ? `⏰ ${m.time} : ${m.home} – ${m.away}` : `⏰ ${list.length} matchs favoris commencent bientôt`,
          body, priority: 3};
}

function morning(sum){
  const y = sum.yesterday || {};
  const lines = [y.n ? `Hier : ${y.ok} pronostic${y.ok > 1 ? "s" : ""} réussi${y.ok > 1 ? "s" : ""} sur ${y.n}.` : "Hier : aucun pronostic vérifié.",
                 `Aujourd'hui : ${sum.foot} match${sum.foot > 1 ? "s" : ""} de foot, ${sum.nhl} match${sum.nhl > 1 ? "s" : ""} NHL.`];
  if ((sum.top || []).length){
    lines.push("Les plus sûrs :");
    for (const t of sum.top) lines.push(`• ${t.label} (${t.match}, ${t.time}) : ${t.prob} %`);
  }
  lines.push("Touchez ☆ à côté d'un match sur le site pour recevoir ses alertes.");
  return {title: "☀️ Résumé du matin", body: lines.join("\n"), priority: 2};
}

// Compare le nouvel état d'un match au précédent et renvoie les alertes à envoyer.
export function update(w, s){
  const m = w.m, out = [];
  if (s.cancelled){
    if (!w.done) out.push({title: `⏸️ Reporté : ${m.home} – ${m.away}`, body: "Plus d'alerte pour ce match.", priority: 3});
    w.done = true;
    return out;
  }
  if (s.state === "pre") return out;
  const prev = w.last;
  w.last = {sh: s.sh, sa: s.sa, red: {...s.red}, state: s.state, goals: s.goals.length};
  const lp = L.liveProbs(m, s);
  const picks = followedPicks(m).map(([k, pk]) => ({k, pk, p: L.pickProb(pk, m, s, lp)}));
  const before = x => w.lastP[x.k] ?? x.pk.p;
  const commit = () => picks.forEach(x => { if (x.p != null) w.lastP[x.k] = x.p; });
  if (!prev){                 // favori ajouté en cours de match : point de départ, pas d'alerte rétroactive
    commit();
    if (s.state === "post") w.done = true;
    return out;
  }
  const score = `${m.home} ${s.sh}-${s.sa} ${m.away}`;
  const line = x => {
    if (x.p == null) return `• ${x.pk.label}`;
    const mark = x.p >= 0.9999 ? " ✅" : "";               // acquis (buteur qui a marqué) ; la fin du match a son propre message
    return `• ${x.pk.label} : ${L.pct(before(x))} % → ${L.pct(x.p)} %${mark}`;
  };
  if (s.state === "post"){
    w.done = true;
    out.push({title: `🏁 Terminé : ${score}`, priority: 3,
              body: picks.map(x => x.p == null ? `• ${x.pk.label} : résultat dans le Bilan` : `${x.p >= 0.5 ? "✅" : "❌"} ${x.pk.label}`).join("\n")});
    commit();
    return out;
  }
  const when = s.label ? ` (${s.label})` : "";
  const events = [];
  let priority = 3;
  const total = s.sh + s.sa, prevTotal = prev.sh + prev.sa;
  if (total > prevTotal){
    const team = s.sh > prev.sh && s.sa > prev.sa ? "" : s.sh > prev.sh ? ` ${m.home}` : ` ${m.away}`;
    events.push(`${m.sport === "nhl" ? "🏒" : "⚽"} But${team} ! ${score}`);
    const who = s.goals.slice(prev.goals || 0).map(g => `${g.clock} ${g.name}${g.own ? " (contre son camp)" : g.pen ? " (penalty)" : ""}`.trim()).filter(Boolean);
    events.push(who.length ? who.join(", ") : s.label);
    priority = 4;
  } else if (total < prevTotal){
    events.push(`↩️ But annulé : ${score}${when}`);
  }
  for (const [side, team] of [["h", m.home], ["a", m.away]]){
    if ((s.red[side] || 0) > ((prev.red || {})[side] || 0)) events.push(`🟥 Carton rouge pour ${team}${events.length ? "" : when}`);
  }
  if (events.length){
    if (!events[0].includes(score)) events.push(score);
    out.push({title: events[0], body: [...events.slice(1), ...picks.map(line)].filter(Boolean).join("\n"), priority});
    commit();
    return out;
  }
  // pas d'action marquante : un pronostic a-t-il beaucoup bougé avec le temps qui passe ?
  const moved = picks.filter(x => {
    if (x.p == null) return false;
    const b = before(x);
    return Math.abs(x.p - b) >= 0.2 || (x.p >= 0.95 && b < 0.95) || (x.p <= 0.05 && b > 0.05);
  });
  if (moved.length){
    const up = moved[0].p > before(moved[0]);
    out.push({title: `${up ? "📈" : "📉"} ${m.home} – ${m.away} : pronostic qui ${up ? "se confirme" : "s'éloigne"}`,
              body: [`${s.sh}-${s.sa}${when}`, ...moved.map(line)].join("\n"), priority: 3});
    commit();
  }
  return out;
}

function note(st, now, text){
  st.errors = [{t: new Date(now).toISOString(), text}, ...(st.errors || [])].slice(0, 15);
}

// Un passage du robot. st : état conservé d'une minute à l'autre ; favs : {id du match: date d'ajout}.
// io.getJSON(url) lit une adresse ; io.site est l'adresse du site. Renvoie {msgs, expired}.
export async function tick(st, favs, now, io){
  st.watch ||= {};
  const msgs = [];
  if (!st.data || now - st.data.fetched > 10 * MIN){
    try {
      const d = await io.getJSON(`${io.site}live.json?t=${Math.floor(now / MIN)}`);
      if (d && Array.isArray(d.matches)) st.data = {...d, fetched: now};
    } catch (e){
      note(st, now, `live.json : ${e.message}`);
      if (st.data) st.data.fetched = now - 5 * MIN;          // nouvel essai dans 5 minutes
    }
  }
  const matches = (st.data || {}).matches || [];
  const byId = Object.fromEntries(matches.map(m => [m.id, m]));

  // favoris -> matchs suivis, et rappel 15 minutes avant
  for (const id of Object.keys(st.watch)) if (!favs[id]) delete st.watch[id];
  const soon = [];
  for (const id of Object.keys(favs)){
    const m = byId[id] || (st.watch[id] || {}).m;
    if (!m) continue;
    const k = Date.parse(m.kickoff);
    let w = st.watch[id];
    if (!w){
      if (k - now > 16 * MIN || now - k > 5 * HOUR) continue;
      w = st.watch[id] = {m, last: null, lastP: {}, done: false, reminded: false};
      // suivi commencé avant le coup d'envoi : on part de 0-0 pour ne rater aucun but
      if (now < k) w.last = {sh: 0, sa: 0, red: {h: 0, a: 0}, state: "pre", goals: 0};
    } else if (byId[id] && now < k) w.m = byId[id];          // analyses plus récentes avant le match
    if (!w.reminded){
      w.reminded = true;
      if (k - now >= 2 * MIN) soon.push(w);
    }
  }
  if (soon.length) msgs.push(reminder(soon.sort((a, b) => Date.parse(a.m.kickoff) - Date.parse(b.m.kickoff))));

  // matchs en cours : un appel ESPN par championnat
  const feeds = {};
  for (const w of Object.values(st.watch)){
    if (w.done || now < Date.parse(w.m.kickoff) - 3 * MIN) continue;
    if (now - Date.parse(w.m.kickoff) > 6 * HOUR){ w.done = true; continue; }
    (feeds[feedUrl(w.m)] ||= []).push(w);
  }
  for (const [url, list] of Object.entries(feeds)){
    let j;
    try { j = await io.getJSON(url); } catch (e){ note(st, now, `ESPN : ${e.message}`); continue; }
    for (const w of list){
      const e = findEvent(w.m, (j && j.events) || []);
      if (e) msgs.push(...update(w, L.parseEspn(e, w.m.sport)));
    }
  }

  // résumé du matin (une fois par jour, après la mise à jour de 7 h 20)
  const P = parisNow(now), sum = (st.data || {}).summary;
  if (P.hour >= 8 && P.hour < 12 && st.morning !== P.date && sum && sum.date === P.date){
    st.morning = P.date;
    msgs.push(morning(sum));
  }

  // favoris périmés : 36 h après le match (ou après l'ajout si le match n'est plus dans les analyses)
  const expired = Object.keys(favs).filter(id => {
    const m = byId[id] || (st.watch[id] || {}).m;
    return now - (m ? Date.parse(m.kickoff) : favs[id]) > 36 * HOUR;
  });
  for (const id of expired) delete st.watch[id];
  return {msgs: msgs.slice(0, 10), expired};
}
