// Minimal MCP server (Model Context Protocol, Streamable HTTP, stateless):
// POST /mcp/:token, JSON-RPC 2.0 over a single request/response. Auth is
// MCP_TOKEN in the path (mcpTokenMatches, checked in worker.js before this
// module runs). This is a probe: two read-only tools, `health` and
// `get_daily_latest`. No sessions, no SSE, no batching, no writes. Accept,
// Origin and MCP-Protocol-Version are deliberately not checked: the path
// token is the only gate.

import { API_HEADERS, healthPayload, latestDailyEdition } from "./api.js";

const SERVER_INFO = { name: "news-digest", version: "0.1.0" };
const INSTRUCTIONS =
  "Read-only access to the owner's daily news digest; probe build with two tools: health and get_daily_latest.";
const PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"];
const DEFAULT_PROTOCOL_VERSION = "2025-06-18";
const MAX_BODY_BYTES = 65536;

const HEALTH_TOOL = {
  name: "health",
  title: "Digest health",
  description:
    "Reports whether today's daily edition is published and the latest daily edition's id and date.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
  annotations: { readOnlyHint: true, openWorldHint: false },
};

const LATEST_TOOL = {
  name: "get_daily_latest",
  title: "Latest daily edition",
  description:
    "Returns the newest published daily edition (the same object as the JSON API's digests/daily/latest). It may be from a previous day: check `date` / `edition_id` against today before treating it as current.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
  annotations: { readOnlyHint: true, openWorldHint: false },
};

const toolResult = (payload) => ({
  content: [{ type: "text", text: JSON.stringify(payload) }],
  structuredContent: payload,
  isError: false,
});

// Same header set as the JSON API: every response is as private as the URL.
function rpc(body, method, status = 200) {
  console.log(JSON.stringify({ event: "mcp", method, status }));
  return new Response(JSON.stringify(body), { status, headers: API_HEADERS });
}

const rpcError = (id, code, message, method, status = 200) =>
  rpc({ jsonrpc: "2.0", id, error: { code, message } }, method, status);

function accepted() {
  console.log(JSON.stringify({ event: "mcp", method: "notification", status: 202 }));
  const { "content-type": _drop, ...headers } = API_HEADERS;
  return new Response(null, { status: 202, headers });
}

export async function handleMcp(request, env) {
  const tooLarge = () => rpcError(null, -32600, "Request too large", "invalid", 413);
  if (Number(request.headers.get("content-length")) > MAX_BODY_BYTES) return tooLarge();
  const text = await request.text();
  if (text.length > MAX_BODY_BYTES) return tooLarge();
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    return rpcError(null, -32700, "Parse error", "invalid", 400);
  }
  if (Array.isArray(msg)) {
    return rpcError(null, -32600, "Invalid Request", "invalid", 400);
  }
  if (
    msg === null ||
    typeof msg !== "object" ||
    msg.jsonrpc !== "2.0" ||
    typeof msg.method !== "string"
  ) {
    return rpcError(null, -32600, "Invalid Request", "invalid", 400);
  }
  if (!("id" in msg)) return accepted();

  const { id, method, params } = msg;
  switch (method) {
    case "initialize": {
      const asked = params?.protocolVersion;
      const protocolVersion = PROTOCOL_VERSIONS.includes(asked) ? asked : DEFAULT_PROTOCOL_VERSION;
      return rpc(
        {
          jsonrpc: "2.0",
          id,
          result: {
            protocolVersion,
            capabilities: { tools: {} },
            serverInfo: SERVER_INFO,
            instructions: INSTRUCTIONS,
          },
        },
        method,
      );
    }
    case "ping":
      return rpc({ jsonrpc: "2.0", id, result: {} }, method);
    case "tools/list":
      return rpc({ jsonrpc: "2.0", id, result: { tools: [HEALTH_TOOL, LATEST_TOOL] } }, method);
    case "tools/call": {
      const name = params?.name;
      if (name !== "health" && name !== "get_daily_latest") {
        return rpcError(id, -32602, "Unknown tool", method);
      }
      try {
        let result;
        if (name === "health") {
          result = toolResult(await healthPayload(env));
        } else {
          const edition = await latestDailyEdition(env);
          result = edition
            ? toolResult(edition)
            : {
                content: [{ type: "text", text: "No daily edition has been published yet." }],
                isError: true,
              };
        }
        return rpc({ jsonrpc: "2.0", id, result }, method);
      } catch (err) {
        console.error(`mcp ${name} failed: ${err instanceof Error ? err.message : "unknown"}`);
        return rpcError(id, -32603, "Internal error", method);
      }
    }
    default:
      return rpcError(id, -32601, "Method not found", method);
  }
}
