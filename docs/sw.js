// Service worker du site : reçoit les notifications du robot d'alertes (Web Push), même site fermé, et garde
// une copie du site et des dernières analyses pour l'ouvrir sans réseau (au stade, dans le train).
// Toujours le réseau d'abord : la copie ne sert que si le réseau ne répond pas (5 s au plus).
const CACHE = "site-v1";
const FILES = ["index.html", "data.json", "live.js", "manifest.webmanifest", "icon-192.png"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", e => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
  await self.clients.claim();
})()));

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  const scope = new URL(self.registration.scope).pathname;
  const name = req.mode === "navigate" ? "index.html" : url.pathname.slice(scope.length);
  if (!FILES.includes(name)) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE), key = new URL(name, self.registration.scope).href;
    try {
      const r = await fetch(req, {cache: "no-store", signal: AbortSignal.timeout(5000)});
      if (r.ok) e.waitUntil(cache.put(key, r.clone()));
      return r;
    } catch (err) {
      const old = await cache.match(key);
      if (!old) throw err;
      const headers = new Headers(old.headers);
      headers.set("x-hors-ligne", "1");
      return new Response(old.body, {status: 200, headers});
    }
  })());
});

self.addEventListener("push", e => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (err) { m = {title: "Analyse du jour", body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(m.title || "Analyse du jour", {
    body: m.body || "", tag: m.tag, icon: "icon-192.png", badge: "icon-192.png",
    data: {url: m.url || self.registration.scope},
  }));
});

// le navigateur renouvelle parfois l'abonnement : on prévient le robot pour que ce téléphone garde ses favoris
const ROBOT = "https://analyse-sport-alertes.barrierecedric-pro.workers.dev";
self.addEventListener("pushsubscriptionchange", e => {
  e.waitUntil((async () => {
    const old = e.oldSubscription, key = old && old.options && old.options.applicationServerKey;
    const sub = e.newSubscription || (key && await self.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key}));
    if (!sub) return;
    await fetch(ROBOT + "/abonnement", {method: "POST", headers: {"content-type": "text/plain"},
      body: JSON.stringify({sub: sub.toJSON(), old: old ? old.endpoint : sub.endpoint})});
  })());
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data || {}).url || self.registration.scope;
  const id = (url.match(/#m=((?:foot|nhl)-\d+)$/) || [])[1];      // alerte d'un match : on ouvre sa fiche
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) if (c.url.startsWith(self.registration.scope) && "focus" in c){
      if (id) c.postMessage({open: id});
      return c.focus();
    }
    return self.clients.openWindow(url);
  }));
});
