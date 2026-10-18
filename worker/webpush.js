// Web Push sans bibliothèque (WebCrypto seulement) : fonctionne dans Cloudflare Workers et dans Node 20+.
//   - VAPID (RFC 8292) : le robot signe chaque envoi avec sa clé ; le navigateur n'accepte que lui.
//   - Chiffrement aes128gcm (RFC 8291 / RFC 8188) : seul l'appareil abonné peut lire le message.
// Les clés VAPID sont créées par le robot lui-même au premier besoin (aucune clé à copier à la main).

const enc = new TextEncoder();
export const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
export const unb64u = s => {
  const t = s.replace(/\s/g, "").replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(t + "===".slice((t.length + 3) % 4)), c => c.charCodeAt(0));
};
const concat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let i = 0;
  for (const p of parts){ out.set(p, i); i += p.length; }
  return out;
};

export async function newVapidKeys(){
  const kp = await crypto.subtle.generateKey({name: "ECDSA", namedCurve: "P-256"}, true, ["sign", "verify"]);
  const pub = new Uint8Array(await crypto.subtle.exportKey("raw", kp.publicKey));
  return {publicKey: b64u(pub), privateJwk: await crypto.subtle.exportKey("jwk", kp.privateKey)};
}

export async function vapidHeader(endpoint, vapid, subject, now){
  const aud = new URL(endpoint).origin;
  const header = b64u(enc.encode(JSON.stringify({typ: "JWT", alg: "ES256"})));
  const payload = b64u(enc.encode(JSON.stringify({aud, exp: Math.floor(now / 1000) + 12 * 3600, sub: subject})));
  const key = await crypto.subtle.importKey("jwk", vapid.privateJwk, {name: "ECDSA", namedCurve: "P-256"}, false, ["sign"]);
  const sig = await crypto.subtle.sign({name: "ECDSA", hash: "SHA-256"}, key, enc.encode(`${header}.${payload}`));
  return `vapid t=${header}.${payload}.${b64u(sig)}, k=${vapid.publicKey}`;
}

async function hkdf(salt, ikm, info, bytes){
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({name: "HKDF", hash: "SHA-256", salt, info}, key, bytes * 8));
}

// Chiffre `plaintext` pour un abonnement {keys: {p256dh, auth}}. `fixed` (tests) : {salt, asPrivateD, asPublic}.
export async function encrypt(plaintext, sub, fixed = null){
  const uaPublic = unb64u(sub.keys.p256dh), authSecret = unb64u(sub.keys.auth);
  let asPrivate, asPublic;
  if (fixed){
    asPublic = fixed.asPublic;
    asPrivate = await crypto.subtle.importKey("jwk", {kty: "EC", crv: "P-256", d: b64u(fixed.asPrivateD),
      x: b64u(asPublic.slice(1, 33)), y: b64u(asPublic.slice(33, 65))}, {name: "ECDH", namedCurve: "P-256"}, false, ["deriveBits"]);
  } else {
    const kp = await crypto.subtle.generateKey({name: "ECDH", namedCurve: "P-256"}, true, ["deriveBits"]);
    asPrivate = kp.privateKey;
    asPublic = new Uint8Array(await crypto.subtle.exportKey("raw", kp.publicKey));
  }
  const uaKey = await crypto.subtle.importKey("raw", uaPublic, {name: "ECDH", namedCurve: "P-256"}, false, []);
  const ecdh = new Uint8Array(await crypto.subtle.deriveBits({name: "ECDH", public: uaKey}, asPrivate, 256));
  const ikm = await hkdf(authSecret, ecdh, concat(enc.encode("WebPush: info\0"), uaPublic, asPublic), 32);
  const salt = fixed ? fixed.salt : crypto.getRandomValues(new Uint8Array(16));
  const cek = await hkdf(salt, ikm, enc.encode("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(salt, ikm, enc.encode("Content-Encoding: nonce\0"), 12);
  const data = typeof plaintext === "string" ? enc.encode(plaintext) : plaintext;
  const key = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["encrypt"]);
  const ct = new Uint8Array(await crypto.subtle.encrypt({name: "AES-GCM", iv: nonce}, key, concat(data, new Uint8Array([2]))));
  const rs = new Uint8Array([0, 0, 16, 0]);                     // taille d'enregistrement : 4096
  return concat(salt, rs, new Uint8Array([asPublic.length]), asPublic, ct);
}

// Envoie une notification. Renvoie le code HTTP (201 = reçu ; 404/410 = abonnement expiré, à supprimer).
export async function sendPush(sub, message, vapid, {subject, now = Date.now(), ttl = 3600, urgency = "high"} = {}){
  const body = await encrypt(JSON.stringify(message), sub);
  const r = await fetch(sub.endpoint, {method: "POST", body, headers: {
    "content-encoding": "aes128gcm", "content-type": "application/octet-stream", ttl: String(ttl), urgency,
    authorization: await vapidHeader(sub.endpoint, vapid, subject, now)}});
  return r.status;
}
