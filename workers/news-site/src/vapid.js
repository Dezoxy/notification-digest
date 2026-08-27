// ── VAPID (RFC 8292) ────────────────────────────────────────────────────
//
// The one piece of cryptography this repo owns, and it stays this small
// entirely because of the payload-less design (PLAN.md §11.7, "Why the push
// carries no payload"): identifying the application server to a push
// service needs an ES256 JWT and nothing else. Sending an actual PAYLOAD
// would additionally require RFC 8291 message encryption — ECDH against the
// subscription's p256dh, HKDF, AES-128-GCM — which is the named contingency
// if Apple's gateway turns out to reject body-less pushes, not the design.
//
// No dependency, and none is possible: this Worker deploys as one
// self-contained script with no build configuration. WebCrypto has
// everything needed. ECDSA over P-256 with SHA-256 is exactly JWS "ES256",
// and crypto.subtle.sign returns the raw r||s concatenation (IEEE P1363)
// that JWS wants — no DER unwrapping, which is the usual trap when porting
// this from a Node library that uses the `crypto` module instead.

const ENCODER = new TextEncoder();

// One imported CryptoKey per isolate. importKey is not free and the private
// key never changes for the life of a deployment; the JWK string is the
// cache key so a secret rotation (which restarts isolates anyway) can never
// be served by a stale import.
let cachedKey = null;
let cachedJwk = null;

async function privateKey(jwkText) {
  if (cachedKey && cachedJwk === jwkText) return cachedKey;
  const jwk = JSON.parse(jwkText);
  cachedKey = await crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign"],
  );
  cachedJwk = jwkText;
  return cachedKey;
}

function base64url(bytes) {
  let binary = "";
  const view = new Uint8Array(bytes);
  for (let i = 0; i < view.length; i++) binary += String.fromCharCode(view[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64urlText(text) {
  return base64url(ENCODER.encode(text));
}

// 12 hours. RFC 8292 leaves this open but Apple rejects anything more than
// 24h out, so half of the strictest ceiling is the safe choice — and a
// notification JWT has no reason to be long-lived anyway: it is minted per
// fan-out and used within a second.
const JWT_TTL_SECONDS = 12 * 60 * 60;

// `audience` is the ORIGIN of the push endpoint (not the endpoint itself,
// and not this site) — that is what the spec means by the token's audience,
// and getting it wrong is the most common cause of a 401 from a push
// service. `subject` must be a mailto: or https: URL identifying whoever
// operates this sender, so a push service has someone to contact about
// abuse; Apple validates it more strictly than the others.
export async function vapidAuthorization(env, audience, subject) {
  const header = base64urlText(JSON.stringify({ typ: "JWT", alg: "ES256" }));
  const payload = base64urlText(
    JSON.stringify({
      aud: audience,
      exp: Math.floor(Date.now() / 1000) + JWT_TTL_SECONDS,
      sub: subject,
    }),
  );
  const signingInput = `${header}.${payload}`;
  const signature = await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" },
    await privateKey(env.VAPID_PRIVATE_JWK),
    ENCODER.encode(signingInput),
  );
  // The "vapid" scheme (RFC 8292 §3.1): the token and the public key travel
  // together in one header, so the push service can verify the signature
  // without having been told the key in advance.
  return `vapid t=${signingInput}.${base64url(signature)}, k=${env.VAPID_PUBLIC_KEY}`;
}

// A contact URL for whoever operates this sender. VAPID_SUBJECT if the
// owner set one; otherwise this site's own https origin, which RFC 8292
// explicitly permits and which is always correct without needing another
// secret. Deliberately NOT defaulting to a mailto: — inventing an address
// that may not receive mail is worse than naming a URL that certainly
// resolves.
export function vapidSubject(env, siteOrigin) {
  return env.VAPID_SUBJECT || siteOrigin;
}
