// Service worker du site : reçoit les notifications du robot d'alertes (Web Push), même site fermé.
// Il ne met rien en cache : le site reste toujours à jour.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (err) { m = {title: "Analyse du jour", body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(m.title || "Analyse du jour", {
    body: m.body || "", tag: m.tag, icon: "icon-192.png", badge: "icon-192.png",
    data: {url: m.url || self.registration.scope},
  }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data || {}).url || self.registration.scope;
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) if (c.url.startsWith(self.registration.scope) && "focus" in c) return c.focus();
    return self.clients.openWindow(url);
  }));
});
