// Tests du stockage et de l'envoi du robot (worker/index.js) hors Cloudflare : node worker/test-etat.mjs
// « cloudflare:workers » est remplacé par une petite classe DurableObject de test.
import assert from "node:assert/strict";
import { register } from "node:module";
const stub = "data:text/javascript," + encodeURIComponent("export class DurableObject { constructor(ctx, env){ this.ctx = ctx; this.env = env; } }");
register("data:text/javascript," + encodeURIComponent(
  `export async function resolve(s, c, next){ return s === "cloudflare:workers" ? {url: ${JSON.stringify(stub)}, shortCircuit: true} : next(s, c); }`));
const W = await import("./index.js");

const store = new Map();
const ctx = {storage: {get: async k => structuredClone(store.get(k)), put: async (k, v) => { store.set(k, structuredClone(v)); }}};
let fail = true, slow = null;
const sender = {idFromName: n => n, get: () => ({send: async batch => batch.map(b => ({key: b.key, status: fail ? 503 : 201}))})};
const E = new W.Etat(ctx, {SITE: "https://site/", SENDER: sender});
globalThis.fetch = async () => { if (slow) await slow; return {ok: true, json: async () => ({matches: [], generated: new Date().toISOString()})}; };
store.set("vapid", {publicKey: "x"});
store.set("subs", {k1: {sub: {endpoint: "https://fcm.googleapis.com/x", keys: {}}, favs: {}, morning: true}});

// service de notification saturé (503) : la notification repart à la minute suivante, 3 essais au plus
store.set("outbox", [{key: "k1", t: Date.now(), msg: {title: "t"}}]);        // ancien format, sans « tries »
await E.run(); assert.equal(store.get("outbox")[0].tries, 1);
await E.run(); assert.equal(store.get("outbox")[0].tries, 2);
await E.run(); assert.equal(store.get("outbox").length, 0);
// envoi réussi : rien ne reste en attente
fail = false; store.set("outbox", [{key: "k1", t: Date.now(), tries: 0, msg: {title: "t"}}]);
await E.run(); assert.equal(store.get("outbox").length, 0); assert.equal(store.get("st").sent.ok, 1);
console.log("ok - notification réessayée après un échec passager, 3 essais au plus");

// passage lent (ESPN qui traîne) : le passage suivant ne le double pas (sinon alerte de but envoyée deux fois)
let release; slow = new Promise(r => { release = r; });
store.set("st", {});
const first = E.run(), second = E.run();
assert.equal(await second, 0);
release(); await first;
console.log("ok - un passage lent n'est jamais doublé");
console.log("\n2 tests OK");
