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

// The read-only JSON API has its own credential, separate from SITE_TOKEN:
// a leaked reader link must not unlock machine access, and vice versa.
// Fails closed when API_TOKEN is unset, so an unconfigured deploy has no API.
export async function apiTokenMatches(env, token) {
  return Boolean(env.API_TOKEN) && (await keyMatches(token, env.API_TOKEN));
}

// The MCP endpoint (/mcp/:token) has its own credential too, so connecting a
// chat client never shares a secret with the JSON API or the reader link.
// Fails closed when MCP_TOKEN is unset.
export async function mcpTokenMatches(env, token) {
  return Boolean(env.MCP_TOKEN) && (await keyMatches(token, env.MCP_TOKEN));
}
