// A stand-in for the D1 binding, good enough for rendering: it dispatches on
// distinctive substrings of the SQL worker.js actually issues (the full
// inventory of those statements lives in the handlers; this file must keep up
// if a query is added or reshaped — an unrecognized statement THROWS rather
// than returning empty, so drift surfaces as a failing test, not a silently
// emptier golden page).
//
// It is NOT a SQL engine. Each dispatch arm reimplements just enough of its
// one query's semantics over the fixture rows (lexicographic ISO-8601
// comparison stands in for D1's TEXT ordering, which is the same trick the
// real queries rely on).

import { DIGESTS, ARC_CONTEXTS, ARC_IDENTITY } from "./fixtures.mjs";

const LIST_COLUMNS = [
  "id",
  "created_at",
  "tldr",
  "tldr_hu",
  "item_count",
  "section_count",
  "has_attention",
  "kind",
  "source_counts",
  "failed_sources",
];

function pick(row, cols) {
  const out = {};
  for (const c of cols) out[c] = row[c];
  return out;
}

function byCreatedDesc(a, b) {
  return a.created_at === b.created_at ? b.id - a.id : a.created_at < b.created_at ? 1 : -1;
}

function parsedTopics(row) {
  if (!row.topics) return [];
  try {
    const t = JSON.parse(row.topics);
    return Array.isArray(t) ? t : [];
  } catch {
    return [];
  }
}

function identityOf(topic) {
  return topic?.key ?? topic?.slug ?? null;
}

// CHAR(1)/CHAR(2) are the real FTS snippet() mark bytes the renderer
// re-maps to <mark> — reproduce them, not placeholders.
const M1 = String.fromCharCode(1);
const M2 = String.fromCharCode(2);

export function makeDb({ writes, pushSubs, pushSentIds } = {}) {
  // Web Push (PLAN.md §11.7 PR B). Unlike every other arm in this file,
  // these need real MUTABLE state: subscribe/unsubscribe are the first
  // handlers whose whole behavior is what the table contains afterwards
  // (idempotent re-subscribe, the cap, delete-then-lookup), and a
  // read-only fixture cannot express any of that. Callers pass their own
  // array in to seed it and to assert against it.
  const subs = pushSubs ?? [];
  const pushSent = pushSentIds ?? new Set();

  function dispatch(sql, binds) {
    const s = String(sql);

    // Push arms come FIRST so their distinctive tables/shapes are matched
    // before the broader digests patterns below can claim them.
    if (/push_subscriptions/.test(s)) {
      if (/^\s*INSERT INTO push_subscriptions/.test(s)) {
        const [endpoint, p256dh, auth, lang, label, created_at] = binds;
        const existing = subs.find((r) => r.endpoint === endpoint);
        if (existing) {
          // ON CONFLICT DO UPDATE — note created_at is deliberately NOT
          // overwritten by the real statement, so it is not here either.
          Object.assign(existing, { p256dh, auth, lang, label, fail_count: 0 });
        } else {
          subs.push({
            endpoint,
            p256dh,
            auth,
            lang,
            label,
            created_at,
            last_ok_at: null,
            fail_count: 0,
          });
        }
        return { run: async () => ({ success: true }) };
      }
      if (/^\s*DELETE FROM push_subscriptions/.test(s)) {
        const i = subs.findIndex((r) => r.endpoint === binds[0]);
        if (i >= 0) subs.splice(i, 1);
        return { run: async () => ({ success: true }) };
      }
      if (/COUNT\(\*\)/.test(s)) {
        return { first: async () => ({ total: subs.length }) };
      }
      if (/SELECT lang/.test(s)) {
        const row = subs.find((r) => r.endpoint === binds[0]);
        return { first: async () => (row ? { lang: row.lang } : null) };
      }
      if (/SELECT endpoint, fail_count/.test(s)) {
        return { all: async () => ({ results: subs.map((r) => ({ ...r })) }) };
      }
      if (/SELECT 1/.test(s)) {
        const row = subs.find((r) => r.endpoint === binds[0]);
        return { first: async () => (row ? { 1: 1 } : null) };
      }
    }

    if (/push_sent/.test(s)) {
      // Claim-once. INSERT OR IGNORE semantics, and meta.changes is what
      // the sender actually branches on — returning a constant 1 here would
      // make the "a retried ingest does not re-notify" test meaningless.
      const id = Number(binds[0]);
      const already = pushSent.has(id);
      if (!already) pushSent.add(id);
      return { run: async () => ({ meta: { changes: already ? 0 : 1 } }) };
    }

    if (/^\s*UPDATE push_subscriptions/.test(s)) {
      const endpoint = binds[binds.length - 1];
      const row = subs.find((r) => r.endpoint === endpoint);
      if (row) {
        if (/last_ok_at/.test(s)) {
          row.last_ok_at = binds[0];
          row.fail_count = 0;
        } else {
          row.fail_count = binds[0];
        }
      }
      return { run: async () => ({ success: true }) };
    }

    if (/SELECT MAX\(id\) AS max_id/.test(s)) {
      const max = DIGESTS.reduce((m, d) => (d.id > m ? d.id : m), 0);
      return { first: async () => ({ max_id: max }) };
    }

    if (/SELECT id, kind, has_attention FROM digests WHERE id/.test(s)) {
      const row = DIGESTS.find((d) => d.id === Number(binds[0]));
      return { first: async () => (row ? pick(row, ["id", "kind", "has_attention"]) : null) };
    }

    // handlePushLatest's "newest digest" read. Distinctive enough on its
    // own (nothing else orders by id) to sit ahead of the list queries.
    if (/ORDER BY id DESC LIMIT 1/.test(s)) {
      const newest = [...DIGESTS].sort((a, b) => b.id - a.id)[0];
      return {
        first: async () =>
          newest ? pick(newest, ["id", "kind", "tldr", "tldr_hu", "has_attention"]) : null,
      };
    }

    if (/INSERT INTO digests/.test(s)) {
      writes?.push({ table: "digests", binds });
      return { run: async () => ({ success: true }) };
    }
    if (/INSERT INTO arc_context/.test(s)) {
      writes?.push({ table: "arc_context", binds });
      return { run: async () => ({ success: true }) };
    }

    if (/MIN\(created_at\)/.test(s)) {
      const oldest = DIGESTS.reduce(
        (m, d) => (m === null || d.created_at < m ? d.created_at : m),
        null,
      );
      return { first: async () => ({ oldest }) };
    }

    if (/digests_fts/.test(s)) {
      // buildFtsMatch quotes each term ("term"); dispatch on the raw text.
      const q = String(binds[0] ?? "").toLowerCase();
      if (!q.includes("markets")) return { all: async () => ({ results: [] }) };
      const rows = DIGESTS.filter((d) => d.body_md.toLowerCase().includes("markets"))
        .sort(byCreatedDesc)
        .map((d) => ({
          id: d.id,
          created_at: d.created_at,
          kind: d.kind,
          snip: `${M1}Markets${M2} extended losses into the close…`,
          snip_hu: d.body_md_hu ? `A ${M1}piacok${M2} tovább estek…` : null,
        }));
      return { all: async () => ({ results: rows }) };
    }

    if (/json_each/.test(s) && /LIMIT 500/.test(s)) {
      // Arc chain: every topic appearance whose COALESCE(key, slug) matches.
      const wanted = binds[0];
      const rows = [];
      for (const d of DIGESTS) {
        for (const t of parsedTopics(d)) {
          if (identityOf(t) === wanted) {
            rows.push({
              id: d.id,
              created_at: d.created_at,
              kind: d.kind,
              label: t.label,
              topicSlug: t.slug,
              deltas: d.deltas,
            });
          }
        }
      }
      rows.sort(byCreatedDesc);
      return { all: async () => ({ results: rows }) };
    }

    if (/json_each/.test(s)) {
      // NOW section: every topic appearance in the trailing window, ASC.
      const since = binds[0];
      const rows = [];
      for (const d of DIGESTS) {
        if (d.created_at < since) continue;
        for (const t of parsedTopics(d)) {
          rows.push({
            label: t.label,
            identity: identityOf(t),
            created_at: d.created_at,
            id: d.id,
          });
        }
      }
      rows.sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
      return { all: async () => ({ results: rows }) };
    }

    if (/SELECT topics FROM digests/.test(s)) {
      const [startIso, endIso, excludeId] = binds;
      const rows = DIGESTS.filter(
        (d) =>
          d.topics !== null &&
          d.created_at >= startIso &&
          d.created_at < endIso &&
          d.id !== Number(excludeId),
      ).map((d) => ({ topics: d.topics }));
      return { all: async () => ({ results: rows }) };
    }

    if (/body_html FROM digests WHERE id IN/.test(s)) {
      const ids = new Set(binds.map(Number));
      const rows = DIGESTS.filter((d) => ids.has(d.id)).map((d) => ({
        id: d.id,
        body_html: d.body_html,
      }));
      return { all: async () => ({ results: rows }) };
    }

    if (/context_md FROM arc_context/.test(s)) {
      const row = ARC_CONTEXTS.find((c) => c.key === binds[0]) ?? null;
      return { first: async () => (row ? { context_md: row.context_md } : null) };
    }

    if (/\(created_at, id\) </.test(s) || /\(created_at, id\) >/.test(s)) {
      const older = /\(created_at, id\) </.test(s);
      const kindMatch = s.match(/kind = '(\w+)'/);
      const [ca, id] = [binds[0], Number(binds[1])];
      const candidates = DIGESTS.filter((d) => {
        if (kindMatch && d.kind !== kindMatch[1]) return false;
        const cmp = d.created_at === ca ? d.id - id : d.created_at < ca ? -1 : 1;
        return older ? cmp < 0 : cmp > 0;
      }).sort(byCreatedDesc);
      const row = older ? candidates[0] : candidates[candidates.length - 1];
      return { first: async () => (row ? { id: row.id, created_at: row.created_at } : null) };
    }

    if (/WHERE id = \?/.test(s) && /body_html/.test(s)) {
      const row = DIGESTS.find((d) => d.id === Number(binds[0])) ?? null;
      return { first: async () => (row ? { ...row } : null) };
    }

    if (/kind = 'daily'/.test(s)) {
      const rows = DIGESTS.filter((d) => d.kind === "daily").sort(byCreatedDesc);
      return { all: async () => ({ results: rows.map((r) => pick(r, LIST_COLUMNS)) }) };
    }
    if (/kind = 'weekly'/.test(s)) {
      const rows = DIGESTS.filter((d) => d.kind === "weekly").sort(byCreatedDesc);
      return { all: async () => ({ results: rows.map((r) => pick(r, LIST_COLUMNS)) }) };
    }
    if (/FROM digests WHERE created_at >= \? AND created_at </.test(s)) {
      const [startIso, endIso] = binds;
      const rows = DIGESTS.filter((d) => d.created_at >= startIso && d.created_at < endIso).sort(
        byCreatedDesc,
      );
      return { all: async () => ({ results: rows.map((r) => pick(r, LIST_COLUMNS)) }) };
    }

    throw new Error(`stub-db: unrecognized SQL — add a dispatch arm:\n${s}`);
  }

  return {
    prepare(sql) {
      let binds = [];
      const stmt = {
        bind(...args) {
          binds = args;
          return stmt;
        },
        async all() {
          const arm = dispatch(sql, binds);
          if (!arm.all) throw new Error(`stub-db: .all() on a .first()-shaped statement:\n${sql}`);
          return arm.all();
        },
        async first() {
          const arm = dispatch(sql, binds);
          if (!arm.first)
            throw new Error(`stub-db: .first() on a .all()-shaped statement:\n${sql}`);
          return arm.first();
        },
        async run() {
          const arm = dispatch(sql, binds);
          if (!arm.run) throw new Error(`stub-db: .run() on a read-shaped statement:\n${sql}`);
          return arm.run();
        },
      };
      return stmt;
    },
    async batch(stmts) {
      return stmts.map(() => ({ success: true }));
    },
  };
}

export { ARC_IDENTITY };
