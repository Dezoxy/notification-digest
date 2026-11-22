// Minimal MCP server (Model Context Protocol, Streamable HTTP, stateless):
// POST /mcp/:token, JSON-RPC 2.0 over a single request/response. Auth is
// MCP_TOKEN in the path (mcpTokenMatches, checked in worker.js before this
// module runs). Four read-only tools over the daily editions: `health`,
// `get_daily_latest`, `get_daily_by_date` and `list_daily_editions`, all built
// from the shared functions in api.js so MCP and /api/v1 cannot drift. No
// sessions, no SSE, no batching, no writes. Accept, Origin and
// MCP-Protocol-Version are deliberately not checked: the path token is the
// only gate.

import {
  API_HEADERS,
  DEFAULT_LIMIT,
  MAX_LIMIT,
  SCHEMA_VERSION,
  dailyEditionForDate,
  healthPayload,
  latestDailyEdition,
  listDailyEditions,
} from "./api.js";
import { TIMEZONE } from "./config.js";

const SERVER_INFO = { name: "news-digest", version: "0.2.0" };
const INSTRUCTIONS =
  "Read-only access to the owner's daily news digest. Use health to check whether today's edition is out, get_daily_latest for the newest edition, list_daily_editions for recent summaries and get_daily_by_date for one full edition. Dates are Europe/Budapest calendar dates (YYYY-MM-DD); today's edition appears around 20:30 Budapest time.";
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

const DATE_TOOL = {
  name: "get_daily_by_date",
  title: "Daily edition by date",
  description:
    "Returns the full daily edition for a Europe/Budapest calendar date (the same object as the JSON API's digests/daily/<date>). Today's edition appears around 20:30 Budapest time; use list_daily_editions to see which dates exist.",
  inputSchema: {
    type: "object",
    properties: {
      date: {
        type: "string",
        pattern: "^\\d{4}-\\d{2}-\\d{2}$",
        description: "Europe/Budapest calendar date, YYYY-MM-DD",
      },
    },
    required: ["date"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
};

const LIST_TOOL = {
  name: "list_daily_editions",
  title: "List daily editions",
  description:
    "Lists the newest daily editions, newest first, as summaries without the article body (`content`) and `sources`; call get_daily_by_date to fetch a full edition.",
  inputSchema: {
    type: "object",
    properties: {
      limit: {
        type: "integer",
        minimum: 1,
        maximum: MAX_LIMIT,
        default: DEFAULT_LIMIT,
        description: "How many of the newest daily editions to list",
      },
    },
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
};

const TOOL_NAMES = ["health", "get_daily_latest", "get_daily_by_date", "list_daily_editions"];

const toolResult = (payload) => ({
  content: [{ type: "text", text: JSON.stringify(payload) }],
  structuredContent: payload,
  isError: false,
});

// A tool-level failure: HTTP 200 JSON-RPC result with isError, not a JSON-RPC error.
const toolError = (error, message) => ({
  content: [{ type: "text", text: message }],
  structuredContent: { error, message },
  isError: true,
});

const DATE_MESSAGES = {
  invalid_date: () => "Date must be a real calendar date, YYYY-MM-DD.",
  not_published_yet: (date) =>
    `No daily edition for ${date} yet; it is published around 20:30 Europe/Budapest. Check again later.`,
  not_found: (date) => `No daily edition exists for ${date}.`,
};

async function getDailyByDate(env, args) {
  const date = args?.date;
  const { edition, error } = await dailyEditionForDate(env, typeof date === "string" ? date : "");
  return edition ? toolResult(edition) : toolError(error, DATE_MESSAGES[error](date));
}

// absent -> default; an integer number or a string of digits, 1..MAX_LIMIT.
function parseLimit(raw) {
  if (raw === undefined) return DEFAULT_LIMIT;
  const n =
    typeof raw === "number"
      ? raw
      : typeof raw === "string" && /^\d+$/.test(raw)
        ? Number(raw)
        : NaN;
  return Number.isInteger(n) && n >= 1 && n <= MAX_LIMIT ? n : null;
}

async function listEditions(env, args) {
  const limit = parseLimit(args?.limit);
  if (limit === null) {
    return toolError("invalid_limit", `limit must be an integer from 1 to ${MAX_LIMIT}.`);
  }
  const editions = (await listDailyEditions(env, limit)).map(
    ({ content: _content, sources: _sources, ...summary }) => summary,
  );
  return toolResult({
    schema_version: SCHEMA_VERSION,
    timezone: TIMEZONE,
    count: editions.length,
    editions,
  });
}

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
      return rpc(
        { jsonrpc: "2.0", id, result: { tools: [HEALTH_TOOL, LATEST_TOOL, DATE_TOOL, LIST_TOOL] } },
        method,
      );
    case "tools/call": {
      const name = params?.name;
      if (!TOOL_NAMES.includes(name)) {
        return rpcError(id, -32602, "Unknown tool", method);
      }
      try {
        let result;
        if (name === "health") {
          result = toolResult(await healthPayload(env));
        } else if (name === "get_daily_by_date") {
          result = await getDailyByDate(env, params.arguments);
        } else if (name === "list_daily_editions") {
          result = await listEditions(env, params.arguments);
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
