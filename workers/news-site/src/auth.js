export async function tokenMatches(env, token) {
  return Boolean(env.SITE_TOKEN) && (await keyMatches(token, env.SITE_TOKEN));
}

// ── auth helper (shared pattern with workers/polymarket-proxy) ─────────

// Own instance rather than importing ingest.js's: the two modules are
// independent (auth runs on every request, ingest only on PUT), and a
// TextEncoder is stateless — sharing one across module boundaries would
// couple them for nothing.
const TEXT_ENCODER = new TextEncoder();

export async function keyMatches(provided, expected) {
  // Hash both sides first: crypto.subtle.timingSafeEqual requires
  // equal-length inputs, and comparing digests leaks nothing about the
  // secret's length or content.
  // TEXT_ENCODER is the module-level instance — this runs on every request
  // (tokenMatches), and byteLength reuses it on every ingest field.
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", TEXT_ENCODER.encode(provided)),
    crypto.subtle.digest("SHA-256", TEXT_ENCODER.encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(a, b);
}
