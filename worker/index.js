// Robot d'alertes Cloudflare : se réveille chaque minute (cron) et envoie les notifications du site (Web Push),
// sans appli à installer : Android (Chrome), iPhone (site ajouté à l'écran d'accueil, iOS 16.4+), PC.
// Toute la logique des matchs est dans robot.js ; ce fichier gère le stockage, l'envoi et les adresses web :
//   GET  /vapid              clé publique du robot (le navigateur s'abonne avec)
//   POST /abonnement         {code, sub} enregistre un appareil (code d'alerte = secret ALERT_CODE ou NTFY_TOPIC)
//   POST /desabonnement      {endpoint} retire un appareil
//   GET  /favoris            liste des matchs en favori (lue par le site)
//   POST /favoris            {code, id, on} ajoute ou retire un favori
//   POST /test               {code} envoie une notification de test à tous les appareils
//   GET  /etat               état du robot (matchs suivis, dernières alertes, erreurs)
import { DurableObject } from "cloudflare:workers";
import { tick, ESPN } from "./robot.js";
import { newVapidKeys, sendPush } from "./webpush.js";

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

const subKey = endpoint => endpoint.slice(-48);

export class Etat extends DurableObject {
  async favs(){ return (await this.ctx.storage.get("favs")) || {}; }
  async subs(){ return (await this.ctx.storage.get("subs")) || {}; }

  async vapid(){
    let v = await this.ctx.storage.get("vapid");
    if (!v){ v = await newVapidKeys(); await this.ctx.storage.put("vapid", v); }
    return v;
  }

  async subscribe(sub, ua){
    if (!sub || !/^https:\/\//.test(sub.endpoint || "") || !sub.keys || !sub.keys.p256dh || !sub.keys.auth) return {ok: false, message: "Abonnement invalide."};
    const subs = await this.subs();
    subs[subKey(sub.endpoint)] = {sub: {endpoint: sub.endpoint, keys: sub.keys}, ua: String(ua || "").slice(0, 120), added: Date.now()};
    const keys = Object.keys(subs);
    if (keys.length > 10) delete subs[keys[0]];               // 10 appareils au plus
    await this.ctx.storage.put("subs", subs);
    const res = await this.deliver({title: "🔔 Notifications activées", body: "Cet appareil recevra les alertes des matchs en favori (★)."}, {[subKey(sub.endpoint)]: subs[subKey(sub.endpoint)]});
    return {ok: res.sent > 0, appareils: Object.keys(subs).length, envoi: res.statuses};
  }

  async unsubscribe(endpoint){
    const subs = await this.subs();
    delete subs[subKey(String(endpoint || ""))];
    await this.ctx.storage.put("subs", subs);
    return {ok: true, appareils: Object.keys(subs).length};
  }

  // Envoie une alerte à tous les appareils abonnés ; retire ceux dont l'abonnement a expiré (404 / 410).
  async deliver(m, only = null){
    const subs = only || await this.subs();
    const vapid = await this.vapid();
    const message = {title: m.title, body: m.body || "", url: this.env.SITE, tag: m.tag || String(Date.now())};
    const statuses = [], dead = [];
    for (const [k, s] of Object.entries(subs)){
      let status;
      try { status = await sendPush(s.sub, message, vapid, {subject: this.env.SITE, urgency: m.priority >= 4 ? "high" : "normal"}); }
      catch (e){ status = e.message; }
      statuses.push(status);
      if (status === 404 || status === 410) dead.push(k);
    }
    if (dead.length){
      const all = await this.subs();
      for (const k of dead) delete all[k];
      await this.ctx.storage.put("subs", all);
    }
    return {sent: statuses.filter(s => s === 201 || s === 200).length, statuses};
  }

  async run(){
    const st = (await this.ctx.storage.get("st")) || {};
    const now = Date.now();
    let res = {msgs: [], expired: []};
    try {
      res = await tick(st, await this.favs(), now, {site: this.env.SITE, getJSON});
    } catch (e){
      st.errors = [{t: new Date(now).toISOString(), text: `robot : ${e.message}`}, ...(st.errors || [])].slice(0, 15);
    }
    for (const m of res.msgs){
      let status;
      try { const r = await this.deliver(m); status = `${r.sent}/${r.statuses.length} appareil(s)`; } catch (e){ status = e.message; }
      st.log = [{t: new Date(now).toISOString(), title: m.title, status}, ...(st.log || [])].slice(0, 30);
    }
    if (res.expired.length){                       // relu juste avant d'écrire : un favori a pu changer entre-temps
      const favs = await this.favs();
      for (const id of res.expired) delete favs[id];
      await this.ctx.storage.put("favs", favs);
    }
    await this.ctx.storage.put("st", st);
    return res.msgs.length;
  }

  async setFav(id, on){
    const favs = await this.favs();
    if (on) favs[id] = Date.now(); else delete favs[id];
    const ids = Object.keys(favs);
    if (ids.length > 80) delete favs[ids[0]];
    await this.ctx.storage.put("favs", favs);
    return Object.keys(favs);
  }

  async etat(){
    const st = (await this.ctx.storage.get("st")) || {};
    return {analyses: (st.data || {}).generated || null, appareils: Object.keys(await this.subs()).length,
            favoris: Object.keys(await this.favs()),
            suivis: Object.entries(st.watch || {}).map(([id, w]) => ({id, match: `${w.m.home} – ${w.m.away}`,
              coup_envoi: w.m.kickoff, score: w.last ? `${w.last.sh}-${w.last.sa}` : null, termine: w.done})),
            alertes: st.log || [], erreurs: st.errors || []};
  }

  async test(){
    const st = (await this.ctx.storage.get("st")) || {};
    const now = Date.now();
    if (now - (st.lastTest || 0) < 30e3) return {ok: false, message: "Un test toutes les 30 secondes au plus."};
    st.lastTest = now;
    await this.ctx.storage.put("st", st);
    let espn;
    try { espn = {ok: true, events: ((await getJSON(`${ESPN}soccer/eng.1/scoreboard`)).events || []).length}; }
    catch (e){ espn = {ok: false, erreur: e.message}; }
    const n = Object.keys(await this.subs()).length;
    if (!n) return {ok: false, message: "Aucun appareil abonné : appuie d'abord sur « Activer les notifications »."};
    const r = await this.deliver({title: "✅ Test réussi", body: `Le robot d'alertes fonctionne. ESPN : ${espn.ok ? "OK" : "bloqué"}.`});
    return {ok: r.sent > 0, appareils: n, recus: r.sent, envoi: r.statuses, espn};
  }
}

const robot = env => env.STATE.get(env.STATE.idFromName("principal"));
const validCode = (env, code) => {
  const want = env.ALERT_CODE || env.NTFY_TOPIC;
  return !!want && code === want;
};
async function body(req){ try { return await req.json(); } catch (e){ return {}; } }

export default {
  async scheduled(event, env, ctx){
    ctx.waitUntil(robot(env).run());
  },

  async fetch(req, env){
    const url = new URL(req.url), post = req.method === "POST";
    if (req.method === "OPTIONS") return new Response(null, {headers: CORS});
    if (url.pathname === "/vapid") return json({key: (await robot(env).vapid()).publicKey});
    if (url.pathname === "/favoris" && !post) return json({ids: Object.keys(await robot(env).favs())});
    if (url.pathname === "/etat") return json(await robot(env).etat());
    if (url.pathname === "/desabonnement" && post) return json(await robot(env).unsubscribe((await body(req)).endpoint));
    if (post && ["/favoris", "/abonnement", "/test"].includes(url.pathname)){
      const b = await body(req);
      if (!validCode(env, b.code)) return json({ok: false, message: "Code d'alerte incorrect."}, 403);
      if (url.pathname === "/abonnement") return json(await robot(env).subscribe(b.sub, req.headers.get("user-agent")));
      if (url.pathname === "/test") return json(await robot(env).test());
      if (!/^(foot|nhl)-\d+$/.test(String(b.id || ""))) return json({ok: false, message: "Match inconnu."}, 400);
      return json({ok: true, ids: await robot(env).setFav(b.id, !!b.on)});
    }
    return json({ok: true, message: "Robot d'alertes analyse-sport en marche. Voir /etat."});
  },
};
