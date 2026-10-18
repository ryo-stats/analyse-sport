// Robot d'alertes Cloudflare : se réveille chaque minute (cron) et envoie les alertes sur l'app ntfy.
// Toute la logique est dans robot.js ; ce fichier gère le stockage, ntfy et les adresses web :
//   GET  /favoris            liste des matchs en favori (lue par le site)
//   POST /favoris            {code, id, on} ajoute ou retire un favori (code = canal ntfy, secret NTFY_TOPIC)
//   POST /test               {code} envoie une notification de test et vérifie ESPN et le site
//   GET  /etat               état du robot (matchs suivis, dernières alertes, erreurs)
import { DurableObject } from "cloudflare:workers";
import { tick, ESPN } from "./robot.js";

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

async function ntfy(env, m){
  if (!env.NTFY_TOPIC) return "NTFY_TOPIC manquant";
  // Sans compte, ntfy.sh limite par adresse IP ; or les robots Cloudflare partagent leurs IP (erreur 429).
  // Avec un compte ntfy gratuit, le jeton NTFY_TOKEN fait compter les envois sur le compte et non sur l'IP.
  const headers = {"content-type": "application/json"};
  if (env.NTFY_TOKEN) headers.authorization = `Bearer ${env.NTFY_TOKEN}`;
  const r = await fetch("https://ntfy.sh/", {method: "POST", headers,
    body: JSON.stringify({topic: env.NTFY_TOPIC, title: m.title, message: m.body || "", priority: m.priority || 3,
                          click: env.SITE})});
  return r.status;
}

export class Etat extends DurableObject {
  async favs(){ return (await this.ctx.storage.get("favs")) || {}; }

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
      try { status = await ntfy(this.env, m); } catch (e){ status = e.message; }
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
    return {analyses: (st.data || {}).generated || null, favoris: Object.keys(await this.favs()),
            suivis: Object.entries(st.watch || {}).map(([id, w]) => ({id, match: `${w.m.home} – ${w.m.away}`,
              coup_envoi: w.m.kickoff, score: w.last ? `${w.last.sh}-${w.last.sa}` : null, termine: w.done})),
            alertes: st.log || [], erreurs: st.errors || []};
  }

  async test(){
    const st = (await this.ctx.storage.get("st")) || {};
    const now = Date.now();
    if (now - (st.lastTest || 0) < 60e3) return {ok: false, message: "Un test par minute au plus."};
    st.lastTest = now;
    await this.ctx.storage.put("st", st);
    const check = async url => { try { const j = await getJSON(url); return {ok: true, events: (j.events || []).length}; }
                                 catch (e){ return {ok: false, erreur: e.message}; } };
    const espn = await check(`${ESPN}soccer/eng.1/scoreboard`);
    const nhl = await check(`${ESPN}hockey/nhl/scoreboard`);
    let site;
    try { site = {ok: true, analyses: (await getJSON(`${this.env.SITE}live.json?t=${now}`)).generated}; }
    catch (e){ site = {ok: false, erreur: e.message}; }
    let notif;
    try {
      notif = await ntfy(this.env, {title: "✅ Test réussi", priority: 3,
        body: `Le robot d'alertes fonctionne.\nESPN foot : ${espn.ok ? "OK" : "bloqué"} · ESPN NHL : ${nhl.ok ? "OK" : "bloqué"} · Site : ${site.ok ? "OK" : "injoignable"}`});
    } catch (e){ notif = e.message; }
    return {ok: notif === 200, notification: notif, espn_foot: espn, espn_nhl: nhl, site};
  }
}

const robot = env => env.STATE.get(env.STATE.idFromName("principal"));
const validCode = (env, code) => !!env.NTFY_TOPIC && code === env.NTFY_TOPIC;

export default {
  async scheduled(event, env, ctx){
    ctx.waitUntil(robot(env).run());
  },

  async fetch(req, env){
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, {headers: CORS});
    if (url.pathname === "/favoris" && req.method === "GET") return json({ids: Object.keys(await robot(env).favs())});
    if (url.pathname === "/favoris" && req.method === "POST"){
      let body = {};
      try { body = await req.json(); } catch (e){}
      if (!validCode(env, body.code)) return json({ok: false, message: "Code d'alerte incorrect."}, 403);
      if (!/^(foot|nhl)-\d+$/.test(String(body.id || ""))) return json({ok: false, message: "Match inconnu."}, 400);
      return json({ok: true, ids: await robot(env).setFav(body.id, !!body.on)});
    }
    if (url.pathname === "/test" && req.method === "POST"){
      let body = {};
      try { body = await req.json(); } catch (e){}
      if (!validCode(env, body.code)) return json({ok: false, message: "Code d'alerte incorrect."}, 403);
      return json(await robot(env).test());
    }
    if (url.pathname === "/etat") return json(await robot(env).etat());
    return json({ok: true, message: "Robot d'alertes analyse-sport en marche. Voir /etat."});
  },
};
