import { TIMEZONE } from "./config.js";
import { STRINGS } from "./strings.js";

// ── time formatting (Europe/Budapest hardcoded, per project convention:
// storage/comparisons stay UTC, only rendering converts) ────────────────
//
// Both formatters take `locale` explicitly (STRINGS[lang].locale below)
// rather than deriving it from lang themselves — Intl does all the actual
// EN/HU formatting work (weekday/month names, date ordering) once given the
// right locale tag; this file never hand-builds a Hungarian date string.

// One Intl formatter per (options x locale), built on first use and reused
// forever after: Intl constructors are the expensive half of formatting (ICU
// data lookup), and the index page used to construct one PER ROW — with the
// daily/weekly views' LIMIT 1000, that is up to ~2,000 constructions per
// render, all of exactly two distinct formatters. Lazy (nothing built at
// isolate startup), and module-level, so the cache also survives across
// requests for as long as the isolate does. Only two locales ever exist
// (STRINGS.en.locale / STRINGS.hu.locale), so the Maps stay tiny.
export function memoIntl(build) {
  const cache = new Map();
  return (locale) => {
    let fmt = cache.get(locale);
    if (!fmt) {
      fmt = build(locale);
      cache.set(locale, fmt);
    }
    return fmt;
  };
}

export const dayHeaderFmt = memoIntl(
  (locale) =>
    new Intl.DateTimeFormat(locale, {
      timeZone: TIMEZONE,
      weekday: "long",
      day: "numeric",
      month: "long",
      year: "numeric",
    }),
);

export function formatDayHeader(date, locale) {
  // en-GB: "Wednesday, 5 August 2026" · hu-HU: "2026. augusztus 6., csütörtök"
  return dayHeaderFmt(locale).format(date);
}

export function formatTime(date, locale) {
  // "18:00" in both locales (hour12: false makes the locale irrelevant here,
  // but it's threaded through for consistency/future-proofing).
  return timeFmt(locale).format(date);
}

export const timeFmt = memoIntl(
  (locale) =>
    new Intl.DateTimeFormat(locale, {
      timeZone: TIMEZONE,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }),
);

export function formatShortDate(date, locale) {
  // A compact form for <title> (see pageChrome's `title` param): en-GB
  // "Fri 8 Aug" · hu-HU "aug. 8., P" — same fields as formatDayHeader, just
  // abbreviated, so a browser tab/history entry stays legible without
  // eating the whole title budget.
  return shortDateFmt(locale).format(date);
}

export const shortDateFmt = memoIntl(
  (locale) =>
    new Intl.DateTimeFormat(locale, {
      timeZone: TIMEZONE,
      weekday: "short",
      day: "numeric",
      month: "short",
    }),
);

// Budapest's UTC offset, in minutes, AT the given instant — the numeric
// generalization of tzAbbr's CET/CEST lookup below, also reused by
// weekBoundsUtc (roadmap 3 step 2) to convert Budapest-local midnight to a
// UTC instant. Recent ICU versions render timeZoneName:"short" for Europe/*
// zones as a GMT offset ("GMT+1"/"GMT+2") rather than "CET"/"CEST", so the
// offset is derived ourselves instead of trusting a zone-abbreviation
// string; Europe/Budapest only ever has these two (whole-hour) offsets, so
// the parse is exact. Locale-independent numeric parse (not user-facing
// text), so it stays on "en-GB" regardless of the page's language.
export const offsetFmt = memoIntl(
  (locale) =>
    new Intl.DateTimeFormat(locale, {
      timeZone: TIMEZONE,
      timeZoneName: "shortOffset",
    }),
);

export function budapestOffsetMinutes(date) {
  const parts = offsetFmt("en-GB").formatToParts(date);
  const offset = parts.find((p) => p.type === "timeZoneName")?.value ?? "";
  // Match "+2" AND "+02" (ICU emits "GMT+2" for shortOffset today, but a
  // runtime that ever hands back the padded "GMT+02:00" long form must not
  // silently fall through to +1h in August — the exact bug this function
  // exists to avoid).
  const m = offset.match(/([+-])0?(\d)/);
  const sign = m && m[1] === "-" ? -1 : 1;
  const hours = m ? Number(m[2]) : 1; // fail-safe default: CET, +1h
  return sign * hours * 60;
}

export function tzAbbr(date) {
  return budapestOffsetMinutes(date) === 120 ? "CEST" : "CET";
}

// ── ISO week helpers (roadmap 3 step 2 — "weekly pagination"): the
// correctness core of this step. All Budapest-local, DST-safe, and built on
// the same "convert to Budapest calendar y/m/d, then do plain date
// arithmetic on a UTC-noon PROXY date" technique throughout — noon rather
// than midnight so none of the day-shift arithmetic below can ever cross a
// UTC calendar-date boundary next to an actual DST transition (which always
// happens near local midnight, never near local noon). A "proxy" date's
// UTC y/m/d fields are read back as the intended Budapest calendar date;
// its actual instant-in-time value is never used for anything else. ────────

// The Budapest-local calendar date (year/month/day) of a Date/instant, via
// Intl.formatToParts rather than a fixed offset — DST-correct year-round.
// Locale is irrelevant here (parts are picked by `type`, not parsed as
// text), so "en-GB" is used unconditionally, same reasoning as tzAbbr.
export const ymdFmt = memoIntl(
  (locale) =>
    new Intl.DateTimeFormat(locale, {
      timeZone: TIMEZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }),
);

export function budapestDateParts(date) {
  const parts = ymdFmt("en-GB").formatToParts(date);
  const get = (type) => Number(parts.find((p) => p.type === type).value);
  return { y: get("year"), m: get("month"), d: get("day") };
}

// The ISO 8601 week (Monday-first, week 1 is the week containing Jan 4) of
// the Budapest-local calendar date of `date`. Standard algorithm: shift the
// proxy date to "this week's Thursday" (Thursday's calendar year is always
// the correct ISO year, including at year boundaries), then count whole
// weeks from that ISO year's own Jan 1.
export function isoWeekOf(date) {
  const { y, m, d } = budapestDateParts(date);
  const proxy = new Date(Date.UTC(y, m - 1, d, 12));
  const isoWeekday = proxy.getUTCDay() || 7; // Mon=1 .. Sun=7 (Sun is 0 in JS)
  proxy.setUTCDate(proxy.getUTCDate() + 4 - isoWeekday); // -> this week's Thursday
  const isoYear = proxy.getUTCFullYear();
  // Noon-vs-noon (not noon-vs-midnight) so the difference below is an exact
  // whole-day count — see the block comment above on why noon is used
  // throughout rather than midnight.
  const yearStart = new Date(Date.UTC(isoYear, 0, 1, 12));
  const diffDays = (proxy - yearStart) / 86400000;
  const week = Math.ceil((diffDays + 1) / 7);
  return { year: isoYear, week };
}

// Whether ISO year `year` has 53 weeks (rather than the usual 52) — the
// standard rule: true iff Jan 1 falls on a Thursday, or (in a leap year) on
// a Wednesday. Pure calendar arithmetic, no timezone involved — an ISO week
// YEAR is not a Budapest-local concept, just a numbering scheme. Used to
// validate a "w/YYYY-W53/" URL: week 53 is only a real week for years this
// returns true for (see the route match in fetch()).
export function isoWeeksInYear(year) {
  const jan1Weekday = new Date(Date.UTC(year, 0, 1)).getUTCDay(); // 0=Sun..6=Sat
  const isLeap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  return jan1Weekday === 4 || (isLeap && jan1Weekday === 3) ? 53 : 52;
}

// (year, week) tuple comparison, ISO-week-ordinal-aware (a week always
// compares by year first, then week number within it) — same "compare the
// tuple" pattern handleDigestPage already uses for (created_at, id) via
// SQLite row values, just done in JS since these are two small integers,
// not a SQL expression.
export function compareIsoWeek(a, b) {
  return a.year !== b.year ? a.year - b.year : a.week - b.week;
}

// The Budapest calendar date of Jan 4 of `year`, as a UTC-noon proxy — Jan 4
// is always in ISO week 1 by definition, which is what anchors the whole
// per-year week grid below (mondayOfIsoWeek).
export function jan4Proxy(year) {
  return new Date(Date.UTC(year, 0, 4, 12));
}

// Monday of ISO week `week` of `year`, as a UTC-noon proxy holding that
// Monday's Budapest calendar date — the shared anchor for weekBoundsUtc,
// adjacentWeek, and formatWeekRangeLabel below. Back up from Jan 4 (always
// in week 1) to ITS OWN Monday to get week 1's Monday; every other week's
// Monday is exactly (week-1)*7 days later. Plain day-count arithmetic, valid
// across ISO year boundaries too with no special-casing: the Monday-to-
// Monday sequence has no gaps, so "week 53's Monday + 7d" lands correctly on
// next year's week-1 Monday on its own (see weekBoundsUtc's comment for why
// this matters there). `week` may be 0 or negative or run past a year's own
// week count — callers (weekBoundsUtc, adjacentWeek) rely on exactly that to
// step across year boundaries without their own carry logic.
export function mondayOfIsoWeek(year, week) {
  const jan4 = jan4Proxy(year);
  const jan4Weekday = jan4.getUTCDay() || 7; // Mon=1 .. Sun=7
  const monday = new Date(jan4);
  monday.setUTCDate(monday.getUTCDate() - (jan4Weekday - 1) + (week - 1) * 7);
  return monday;
}

// Budapest-local midnight of the given (UTC-noon-proxy-derived) calendar
// date, as a UTC ISO string. Date.UTC(y, m-1, d) is a naive UTC-midnight
// guess for that calendar date; subtracting Budapest's actual UTC offset at
// that boundary shifts it to the real instant. The offset is sampled at the
// NOON proxy of that same calendar date (not at the naive midnight guess
// itself) so a DST transition landing exactly at local midnight can never
// make the offset lookup read the wrong side of the transition.
export function budapestMidnightUtcIso(y, m, d) {
  const offsetMinutes = budapestOffsetMinutes(new Date(Date.UTC(y, m - 1, d, 12)));
  return new Date(Date.UTC(y, m - 1, d) - offsetMinutes * 60000).toISOString();
}

// UTC ISO bounds of ISO week `week` of `year`: Budapest-local Monday 00:00
// of that week through Budapest-local Monday 00:00 of the following week —
// a half-open [start, end) range, matching how every other created_at
// comparison in this file works. Passing `week + 1` into mondayOfIsoWeek
// for the end boundary — rather than computing "next week" via a separate
// adjacentWeek call — is deliberate: it's the exact same plain day-count
// arithmetic that makes ISO-year rollovers (week 52/53 -> next year's week 1)
// fall out correctly with no extra branching, see mondayOfIsoWeek's comment.
export function weekBoundsUtc(year, week) {
  const start = mondayOfIsoWeek(year, week);
  const end = mondayOfIsoWeek(year, week + 1);
  return {
    startIso: budapestMidnightUtcIso(
      start.getUTCFullYear(),
      start.getUTCMonth() + 1,
      start.getUTCDate(),
    ),
    endIso: budapestMidnightUtcIso(end.getUTCFullYear(), end.getUTCMonth() + 1, end.getUTCDate()),
  };
}

// (year, week) shifted by `delta` ISO weeks — used for the week rail's
// older/newer targets (delta ±1) in handleIndexPage. Goes through the
// Monday-date + isoWeekOf round trip (add delta*7 days, then re-derive which
// ISO week that Monday falls in) rather than hand-rolling year/week carry
// arithmetic, so ISO year boundaries reuse the exact same logic as
// weekBoundsUtc/isoWeekOf instead of a second, possibly-diverging copy of it.
export function adjacentWeek(year, week, delta) {
  const monday = mondayOfIsoWeek(year, week);
  monday.setUTCDate(monday.getUTCDate() + delta * 7);
  return isoWeekOf(monday);
}

// The week rail's date-range text, e.g. "3–9 Aug" (en-GB) / "aug. 3–9."
// (hu-HU) — Intl's formatRange collapses the shared month between the two
// boundary dates on its own (this is NOT hand-built by formatting each date
// separately and joining strings, which would repeat the month/produce the
// wrong shape). A week's Mon-Sun span crosses a CALENDAR year boundary near
// ISO year edges (ISO week 1 can start in late December) — show the year in
// that one case so the range isn't ambiguous about which year each date
// falls in; the common case omits it, matching the plain "3–9 Aug" shape.
export const weekRangeFmt = memoIntl(
  (locale) =>
    new Intl.DateTimeFormat(locale, { timeZone: TIMEZONE, day: "numeric", month: "short" }),
);

export const weekRangeYearFmt = memoIntl(
  (locale) =>
    new Intl.DateTimeFormat(locale, {
      timeZone: TIMEZONE,
      day: "numeric",
      month: "short",
      year: "numeric",
    }),
);

export function formatWeekRangeLabel(year, week, locale) {
  const monday = mondayOfIsoWeek(year, week);
  const sunday = new Date(monday);
  sunday.setUTCDate(sunday.getUTCDate() + 6);
  const spansCalendarYearBoundary = monday.getUTCFullYear() !== sunday.getUTCFullYear();
  // Two memoized variants, not one keyed on the flag: the year field is the
  // only difference, and a composite cache key would be the only composite
  // key in the file for two entries' worth of savings.
  const fmt = spansCalendarYearBoundary ? weekRangeYearFmt(locale) : weekRangeFmt(locale);
  // formatRange picks the range separator AND its spacing from ICU's locale
  // data, and that spacing is not stable across ICU versions: ICU 77 renders
  // "24-30 Aug", newer builds render "24 - 30 Aug" (with spaces). Three
  // different ICU builds are in play for this one string — the laptop that
  // generates the golden pages, the Linux image that verifies them, and
  // workerd, which is what readers actually see — so leaving the choice to
  // ICU makes a user-visible label depend on which runtime happened to
  // render it, and makes the golden comparison fail on nothing but a
  // toolchain difference (it did: the first CI build died here).
  //
  // Normalizing the spacing keeps every bit of ICU's locale intelligence —
  // field order, month abbreviation, which parts collapse when the range
  // shares a month — and pins only the one thing that drifts. Tight is the
  // documented intent: see the "3-9 Aug" shape this label is specified as
  // above.
  return fmt.formatRange(monday, sunday).replace(/\s*([\u2013\u2014])\s*/g, "$1");
}

// "Last updated" relative label (arc pages only). Intl.RelativeTimeFormat
// formats ONE unit at a time — it doesn't pick the unit for you — so the
// diff is bucketed by hand into minutes/hours/days, the smallest unit that
// keeps the magnitude under 60/24 respectively; numeric:"auto" lets a locale
// with an idiom for it (e.g. "yesterday") use it instead of a bare count.
// `nowMs` is threaded in from the caller (handleArcPage's `Date.now()`)
// rather than read here, same "inject the current instant" convention
// isoWeekOf/handleIndexPage already use elsewhere in this file — keeps this
// function a pure, stub-testable computation.
export const relTimeFmt = memoIntl(
  (locale) => new Intl.RelativeTimeFormat(locale, { numeric: "auto" }),
);

export function formatRelativeTime(date, locale, nowMs) {
  const diffMs = date.getTime() - nowMs; // negative here: always a past appearance
  const rtf = relTimeFmt(locale);
  const minutes = Math.round(diffMs / 60000);
  if (Math.abs(minutes) < 60) return rtf.format(minutes, "minute");
  const hours = Math.round(diffMs / 3600000);
  if (Math.abs(hours) < 24) return rtf.format(hours, "hour");
  const days = Math.round(diffMs / 86400000);
  return rtf.format(days, "day");
}
