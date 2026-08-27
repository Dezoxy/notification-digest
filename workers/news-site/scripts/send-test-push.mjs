#!/usr/bin/env node
// One-off: send a single body-less Web Push to a stored subscription,
// without waiting for a digest to land (PLAN.md §11.7, PR C verification).
//
// This exists to answer ONE question quickly: does the target push service
// accept a push with no body? Chrome and Firefox definitively do. Apple's
// gateway is the strict one, and §11.7 names RFC 8291 payload encryption as
// the contingency if it does not — so finding out in ten seconds rather
// than at the next digest is worth a script.
//
// It reproduces exactly what src/notify.js's deliver() sends: same VAPID
// scheme, same headers, no body. If this succeeds and a real digest does
// not ring, the fault is upstream of delivery (claim-once, newest-only, or
// the subscription itself) rather than in the push path.
//
// Usage — the private key is passed by ENV, never as an argument, so it
// does not land in shell history. With no endpoint given, every stored
// subscription is read straight from D1 and pushed to:
//
//   VAPID_PRIVATE_JWK='<the JWK you generated>' node scripts/send-test-push.mjs
//   VAPID_PRIVATE_JWK='<...>' node scripts/send-test-push.mjs <endpoint> --urgent
//
// The public key is DERIVED from the private JWK rather than passed in,
// which also proves the pair set in Cloudflare actually matches — a
// mismatch there is the most common cause of a 401 and says nothing about
// itself in the response.

import { execFileSync } from "node:child_process";

const urgent = process.argv.includes("--urgent");
const jwkText = process.env.VAPID_PRIVATE_JWK;
const subject = process.env.VAPID_SUBJECT || "https://news.toomhorvath.com";

if (!jwkText) {
  console.error("VAPID_PRIVATE_JWK is required (pass it by env, not as an argument)");
  console.error(
    "usage: VAPID_PRIVATE_JWK='<jwk>' node scripts/send-test-push.mjs [endpoint] [--urgent]",
  );
  process.exit(2);
}

function endpointsFromD1() {
  const out = execFileSync(
    "npx",
    [
      "wrangler",
      "d1",
      "execute",
      "news-digests",
      "--remote",
      "--command",
      "SELECT endpoint FROM push_subscriptions",
      "--json",
    ],
    { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
  );
  return JSON.parse(out)[0].results.map((r) => r.endpoint);
}

const explicit = process.argv[2] && !process.argv[2].startsWith("--") ? process.argv[2] : null;
const endpoints = explicit ? [explicit] : endpointsFromD1();

if (endpoints.length === 0) {
  console.error("no subscriptions stored — subscribe on a device first");
  process.exit(1);
}

const { subtle } = globalThis.crypto;
const enc = new TextEncoder();
const b64url = (bytes) =>
  Buffer.from(bytes).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

const jwk = JSON.parse(jwkText);
const privateKey = await subtle.importKey(
  "jwk",
  jwk,
  { name: "ECDSA", namedCurve: "P-256" },
  false,
  ["sign"],
);

// Derive the public key from the private JWK's own curve point. If this does
// not match what Cloudflare holds as VAPID_PUBLIC_KEY, the push service will
// answer 401 and that mismatch is the reason.
const publicKey = b64url(
  Buffer.concat([
    Buffer.from([0x04]),
    Buffer.from(jwk.x.replace(/-/g, "+").replace(/_/g, "/"), "base64"),
    Buffer.from(jwk.y.replace(/-/g, "+").replace(/_/g, "/"), "base64"),
  ]),
);

for (const endpoint of endpoints) {
  await send(endpoint);
}

async function send(endpoint) {
  const audience = new URL(endpoint).origin;
  const header = b64url(enc.encode(JSON.stringify({ typ: "JWT", alg: "ES256" })));
  const payload = b64url(
    enc.encode(
      JSON.stringify({
        aud: audience,
        exp: Math.floor(Date.now() / 1000) + 12 * 60 * 60,
        sub: subject,
      }),
    ),
  );
  const signingInput = `${header}.${payload}`;
  const signature = await subtle.sign(
    { name: "ECDSA", hash: "SHA-256" },
    privateKey,
    enc.encode(signingInput),
  );
  const authorization = `vapid t=${signingInput}.${b64url(signature)}, k=${publicKey}`;

  console.log(`aud:      ${audience}`);
  console.log(`sub:      ${subject}`);
  console.log(`pub key:  ${publicKey.slice(0, 16)}…  (must match VAPID_PUBLIC_KEY in Cloudflare)`);
  console.log(`endpoint: ${endpoint.slice(0, 48)}…`);
  console.log("");

  const res = await fetch(endpoint, {
    method: "POST",
    headers: {
      Authorization: authorization,
      TTL: "14400",
      Urgency: urgent ? "high" : "normal",
    },
  });

  const body = await res.text().catch(() => "");
  console.log(`-> ${res.status} ${res.statusText}`);
  if (body) console.log(body.slice(0, 500));
  console.log("");

  if (res.ok) {
    console.log("ACCEPTED. A body-less push is fine for this service.");
    console.log("Your phone should buzz within a few seconds.");
  } else if (res.status === 401 || res.status === 403) {
    console.log("VAPID REJECTED. Usual causes, in order of likelihood:");
    console.log("  - the public key above does not match VAPID_PUBLIC_KEY in Cloudflare");
    console.log("  - `sub` is not acceptable (Apple is strictest; try a real mailto:)");
    console.log("  - clock skew on this machine (exp is 12h out, so this is unlikely)");
  } else if (res.status === 400) {
    console.log("BAD REQUEST — read the body above carefully.");
    console.log("If it complains about a missing body or encryption, that is the answer");
    console.log("§11.7 was waiting for: this service requires a payload, so the");
    console.log("RFC 8291 path (ECDH -> HKDF -> AES-128-GCM) is now required.");
    console.log("p256dh/auth are already stored, so no device has to re-subscribe.");
  } else if (res.status === 404 || res.status === 410) {
    console.log("GONE — this subscription is dead. Re-subscribe on the device.");
    console.log("(notify.js deletes rows on these two statuses, so this is self-healing.)");
  }
}
