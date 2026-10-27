// Robot d'alertes Cloudflare (offre gratuite) : se réveille chaque minute et envoie les notifications du site
// (Web Push), sans appli ni compte : Android (Chrome), iPhone (site ajouté à l'écran d'accueil, iOS 16.4+), PC.
// Site ouvert : chaque téléphone est reconnu par son adresse d'abonnement et a SES favoris (15 au plus).
// Toute la logique des matchs est dans robot.js ; ce fichier gère le stockage, l'envoi et les adresses web :
//   GET  /vapid               clé publique du robot (le navigateur s'abonne avec)
//   POST /abonnement          {sub, favs, morning, old} inscrit ou met à jour un téléphone
//   POST /moi                 {endpoint} favoris et réglages de ce téléphone
//   POST /favoris             {endpoint, id, on} ajoute ou retire un favori de ce téléphone
//   POST /reglages            {endpoint, morning} active ou coupe le résumé du matin
//   POST /test                {endpoint} notification de test sur ce téléphone
//   POST /desabonnement       {endpoint} oublie ce téléphone
//   GET  /etat                état général (nombres seulement, aucune donnée personnelle)
//
// Envoi réparti : chaque minute, au plus 10 lots de 40 notifications, chacun confié à un petit robot « Envoi »
// (une instance Durable Object par lot, avec sa propre limite d'appels). Le surplus part à la minute suivante.
import { DurableObject } from "cloudflare:workers";
import { tick, route, healthReport, ESPN, MAX_FAVS, PUSH_HOSTS } from "./robot.js";
import { newVapidKeys, sendPush } from "./webpush.js";

const BATCH = 40, MAX_BATCHES = 10, MAX_SUBS = 3000, OUTBOX_TTL = 15 * 60e3, MAX_TRIES = 3, RUN_LOCK = 4 * 60e3;
const ID_RE = /^(foot|nhl)-\d+$/;
const CORS = {"access-control-allow-origin": "*", "access-control-allow-methods": "GET, POST, OPTIONS",
              "access-control-allow-headers": "content-type"};
const json = (obj, status = 200) => new Response(JSON.stringify(obj, null, 1), {status,
  headers: {...CORS, "content-type": "application/json; charset=utf-8", "cache-control": "no-store"}});

async function getJSON(url){
  const r = await fetch(url, {headers: {"accept": "application/json", "user-agent": "Mozilla/5.0 (analyse-sport alertes)"},
                              signal: AbortSignal.timeout(10000)});
  if (!r.ok) throw new Error(`${new URL(url).hostname} a répondu ${r.status}`);
  return r.json();
}

// Demande à GitHub un passage des analyses (secours quand ses lancements programmés sautent). Jeton : secret
// GH_TOKEN du robot (Cloudflare > Workers > analyse-sport-alertes > Settings > Variables and Secrets), droit
// « Actions : Read and write » sur le seul dépôt du site. Sans jeton, rien n'est fait.
function dispatcher(env){
  if (!env.GH_TOKEN) return null;
  return async () => {
    const r = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/update.yml/dispatches`, {
      method: "POST", signal: AbortSignal.timeout(10000),
      headers: {"authorization": `Bearer ${env.GH_TOKEN}`, "accept": "application/vnd.github+json",
                "x-github-api-version": "2022-11-28", "user-agent": "analyse-sport-alertes", "content-type": "application/json"},
      body: JSON.stringify({ref: "main", inputs: {porte: "horaires"}})});
    if (r.status !== 204) throw new Error(`GitHub a répondu ${r.status}`);
  };
}

async function keyOf(endpoint){
  const h = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(endpoint))));
  return [...h.slice(0, 12)].map(b => b.toString(16).padStart(2, "0")).join("");
}
const favList = s => Object.keys(s.favs || {});

// Petit robot d'envoi : envoie un lot de notifications et renvoie le code de réponse de chacune.
export class Envoi extends DurableObject {
  async send(batch, vapid, subject){
    const out = [];
    for (const {key, sub, msg} of batch){
      let status;
      try { status = await sendPush(sub, msg, vapid, {subject, urgency: msg.urgency || "normal"}); }
      catch (e){ status = String(e.message || e).slice(0, 80); }
      out.push({key, status});
    }
    return out;
  }
}

export class Etat extends DurableObject {
  async subs(){ return (await this.ctx.storage.get("subs")) || {}; }
  async saveSubs(subs){ await this.ctx.storage.put("subs", subs); }

  async vapid(){
    let v = await this.ctx.storage.get("vapid");
    if (!v){ v = await newVapidKeys(); await this.ctx.storage.put("vapid", v); }
    return v;
  }

  push(sub, title, body, urgency = "normal"){
    return this.vapid().then(v => sendPush(sub, {title, body, url: this.env.SITE, tag: String(Date.now())}, v,
                                           {subject: this.env.SITE, urgency}));
  }

  // ---- téléphones
  async register(sub, favs, morning, old){
    if (!sub || !PUSH_HOSTS.test(sub.endpoint || "") || !sub.keys || !sub.keys.p256dh || !sub.keys.auth)
      return {ok: false, message: "Abonnement refusé par le robot."};
    const key = await keyOf(sub.endpoint), oldKey = old ? await keyOf(old) : null;
    const subs = await this.subs();
    if (!subs[key] && Object.keys(subs).length >= MAX_SUBS) return {ok: false, message: "Trop d'inscrits pour l'instant, réessaie plus tard."};
    const prev = subs[key] || (oldKey && subs[oldKey]) || {favs: {}, morning: false, added: Date.now()};
    if (oldKey && oldKey !== key) delete subs[oldKey];
    const merged = {...prev.favs};
    for (const id of (Array.isArray(favs) ? favs : []).filter(id => ID_RE.test(id))) merged[id] ||= Date.now();
    const kept = Object.entries(merged).sort((a, b) => b[1] - a[1]).slice(0, MAX_FAVS);   // les 15 plus récents
    subs[key] = {sub: {endpoint: sub.endpoint, keys: sub.keys}, favs: Object.fromEntries(kept),
                 morning: morning == null ? !!prev.morning : !!morning, added: prev.added, seen: Date.now()};
    await this.saveSubs(subs);
    let status;
    if (!old){
      try { status = await this.push(sub, "🔔 C'est activé !", "Tu recevras les buts et le résultat de tes matchs en favori (★).", "high"); }
      catch (e){ status = String(e.message); }
    }
    return {ok: true, favs: favList(subs[key]), morning: subs[key].morning, envoi: status};
  }

  async me(endpoint){
    const s = (await this.subs())[await keyOf(endpoint)];
    return s ? {ok: true, favs: favList(s), morning: !!s.morning} : {ok: false, inconnu: true};
  }

  async setFav(endpoint, id, on){
    const subs = await this.subs(), s = subs[await keyOf(endpoint)];
    if (!s) return {ok: false, inconnu: true, message: "Notifications pas activées sur ce téléphone."};
    s.favs ||= {};
    if (on && !s.favs[id] && Object.keys(s.favs).length >= MAX_FAVS)
      return {ok: false, favs: favList(s), message: `${MAX_FAVS} favoris au maximum : retire d'abord une étoile.`};
    if (on) s.favs[id] = Date.now(); else delete s.favs[id];
    await this.saveSubs(subs);
    return {ok: true, favs: favList(s)};
  }

  async setMorning(endpoint, on){
    const subs = await this.subs(), s = subs[await keyOf(endpoint)];
    if (!s) return {ok: false, inconnu: true};
    s.morning = !!on;
    await this.saveSubs(subs);
    return {ok: true, morning: s.morning};
  }

  async unsubscribe(endpoint){
    const subs = await this.subs();
    delete subs[await keyOf(endpoint)];
    await this.saveSubs(subs);
    return {ok: true};
  }

  async test(endpoint){
    const subs = await this.subs(), key = await keyOf(endpoint), s = subs[key];
    if (!s) return {ok: false, inconnu: true, message: "Notifications pas activées sur ce téléphone."};
    if (Date.now() - (s.lastTest || 0) < 30e3) return {ok: false, message: "Attends 30 secondes entre deux tests."};
    s.lastTest = Date.now();
    await this.saveSubs(subs);
    let status;
    try { status = await this.push(s.sub, "✅ Test réussi", "Les notifications marchent sur ce téléphone.", "high"); }
    catch (e){ status = String(e.message); }
    return {ok: status === 201 || status === 200, envoi: status};
  }

  // ---- passage de chaque minute. Verrou : si le passage précédent traîne (ESPN lent), on ne le double pas
  // (deux passages en même temps enverraient deux fois la même alerte de but).
  async run(){
    const now = Date.now();
    if (this.busy && now - this.busy < RUN_LOCK) return 0;
    this.busy = now;
    try { return await this.pass(now); }
    finally { this.busy = 0; }
  }

  async pass(now){
    const st = (await this.ctx.storage.get("st")) || {};
    const subs0 = await this.subs();
    const union = {};
    for (const s of Object.values(subs0)) for (const [id, t] of Object.entries(s.favs || {})) union[id] = Math.min(union[id] || t, t);
    let res = {msgs: [], expired: []};
    try {
      res = await tick(st, union, now, {site: this.env.SITE, getJSON, dispatch: dispatcher(this.env)});
    } catch (e){
      st.errors = [{t: new Date(now).toISOString(), text: `robot : ${e.message}`}, ...(st.errors || [])].slice(0, 15);
    }
    // alertes -> boîte d'envoi (seulement les téléphones concernés)
    const outbox = ((await this.ctx.storage.get("outbox")) || []).filter(x => now - x.t < OUTBOX_TTL);
    const deliveries = route(res.msgs, subs0);
    for (const d of deliveries) outbox.push({key: d.key, t: now, tries: 0,
      msg: {title: d.msg.title, body: d.msg.body || "", url: this.env.SITE + (d.msg.match ? `#m=${d.msg.match}` : ""),   // ouvre le match
            tag: `${now}-${outbox.length}`,
            urgency: d.msg.priority >= 4 ? "high" : "normal"}});
    for (const m of res.msgs){
      const n = deliveries.filter(d => d.msg === m).length;
      st.log = [{t: new Date(now).toISOString(), title: m.title, telephones: n}, ...(st.log || [])].slice(0, 30);
    }
    // envoi réparti
    const take = outbox.splice(0, BATCH * MAX_BATCHES);
    const dead = new Set();
    if (take.length){
      const vapid = await this.vapid(), batches = [];
      const current = await this.subs();
      const items = take.map(x => current[x.key] ? {key: x.key, sub: current[x.key].sub, msg: x.msg, x} : null).filter(Boolean);
      for (let i = 0; i < items.length; i += BATCH) batches.push(items.slice(i, i + BATCH));
      const results = await Promise.allSettled(batches.map((b, i) =>
        this.env.SENDER.get(this.env.SENDER.idFromName(`lot-${i}`)).send(b.map(({key, sub, msg}) => ({key, sub, msg})), vapid, this.env.SITE)));
      let ok = 0, ko = 0;
      // échec passager (service de notification saturé, coupure) : la notification repart à la minute suivante
      const retry = item => { const n = (item && item.x.tries || 0) + 1; if (item && n < MAX_TRIES) outbox.push({...item.x, tries: n}); };
      results.forEach((r, i) => {
        if (r.status !== "fulfilled"){ ko += batches[i].length; batches[i].forEach(retry); return; }
        r.value.forEach((x, j) => {
          if (x.status === 201 || x.status === 200){ ok++; return; }
          ko++;
          if (x.status === 404 || x.status === 410) dead.add(x.key);       // abonnement expiré : on oublie ce téléphone
          else if (x.status === 429 || typeof x.status !== "number" || x.status >= 500) retry(batches[i][j]);
        });
      });
      st.sent = {t: new Date(now).toISOString(), ok, ko, attente: outbox.length};
    }
    await this.ctx.storage.put("outbox", outbox);
    // favoris périmés et téléphones expirés : relus juste avant d'écrire (un téléphone a pu changer entre-temps)
    if (res.expired.length || dead.size){
      const subs = await this.subs();
      for (const k of dead) delete subs[k];
      for (const s of Object.values(subs)) for (const id of res.expired) delete (s.favs || {})[id];
      await this.saveSubs(subs);
    }
    await this.ctx.storage.put("st", st);
    return res.msgs.length;
  }

  async etat(){
    const st = (await this.ctx.storage.get("st")) || {}, subs = await this.subs();
    const fav = new Set(Object.values(subs).flatMap(favList));
    return {sante: healthReport(st, Date.now()), analyses: (st.data || {}).generated || null, telephones: Object.keys(subs).length,
            matchs_en_favori: fav.size, en_attente: ((await this.ctx.storage.get("outbox")) || []).length,
            dernier_envoi: st.sent || null,
            suivis: Object.values(st.watch || {}).map(w => ({match: `${w.m.home} – ${w.m.away}`, coup_envoi: w.m.kickoff,
              score: w.last && w.last.state !== "pre" ? `${w.last.sh}-${w.last.sa}` : null, termine: w.done})),
            alertes: st.log || [], erreurs: st.errors || []};
  }
}

const robot = env => env.STATE.get(env.STATE.idFromName("principal"));
async function body(req){ try { return await req.json(); } catch (e){ return {}; } }

export default {
  async scheduled(event, env, ctx){
    ctx.waitUntil(robot(env).run());
  },

  async fetch(req, env){
    const url = new URL(req.url), p = url.pathname;
    if (req.method === "OPTIONS") return new Response(null, {headers: CORS});
    if (p === "/vapid") return json({key: (await robot(env).vapid()).publicKey});
    if (p === "/etat") return json(await robot(env).etat());
    if (req.method !== "POST") return json({ok: true, message: "Robot d'alertes analyse-sport en marche. Voir /etat."});
    const b = await body(req), r = robot(env);
    if (p === "/abonnement") return json(await r.register(b.sub, b.favs, b.morning, b.old));
    const endpoint = String(b.endpoint || "");
    if (!PUSH_HOSTS.test(endpoint)) return json({ok: false, message: "Téléphone inconnu."}, 400);
    if (p === "/moi") return json(await r.me(endpoint));
    if (p === "/favoris"){
      if (!ID_RE.test(String(b.id || ""))) return json({ok: false, message: "Match inconnu."}, 400);
      return json(await r.setFav(endpoint, b.id, !!b.on));
    }
    if (p === "/reglages") return json(await r.setMorning(endpoint, !!b.morning));
    if (p === "/test") return json(await r.test(endpoint));
    if (p === "/desabonnement") return json(await r.unsubscribe(endpoint));
    return json({ok: false, message: "Adresse inconnue."}, 404);
  },
};
