# Redesign design guidance

Design-execution guidance for the PLAN.md §11 site work implemented in
toom-edge — §11.1/§11.2 first, §11.3's "what changed" rendering when it
comes. This doc carries only design principles — how the redesign
should look and behave — not feature specs or approval status; those live
in each §11 entry. Read this alongside the §11 entry you're executing, not
instead of it.

Distilled from the second Codex redesign brief (2026-08-10, "round 2").
Triaged: not everything in that brief was adopted — this is the part that
survived triage, reorganized as principles.

**Status (2026-08-25): historical, read with care.** §11.1, §11.2, §11.3 and
§11.6 have all shipped, and the site's visual identity has since been
REPLACED by the Front Page print-poster redesign (toom-edge #144 and the
pass that followed it). The "Identity to preserve" section below therefore
describes the pre-#144 wire-desk look, not the live site — check the current
design against toom-edge itself before treating any of it as a constraint.
The scanning/hierarchy principles and the anti-patterns at the end are
design judgment rather than a description of the site, and still hold.

## Identity to preserve

The shipped site already has an identity: dark charcoal background, subtle
borders, muted typography, monospace metadata, a purple/periwinkle accent,
timestamps everywhere, information density. None of that is up for
restyling — the redesign evolves this identity, it does not replace it.

Design dark-first: every new component gets designed for a dark surface
from the start, not sketched light and translated. A translated light
design reads as generic SaaS the moment it lands on charcoal.

## Scanning and hierarchy

The reader should understand the state of the world in about ten seconds.
That sets the eye path for any story or briefing unit: major story first,
then headline, then what changed, then summary, then metadata last.
Headline communicates the dominant change; summary sits under it and
supports; metadata is the last thing the eye reaches, not the first.

Progressive disclosure governs the homepage: show enough to decide whether
something deserves attention, not everything the backend knows — depth is
one click away, not zero and not buried. Delta over repetition applies
here too, not just in prompt design: communicate what changed since the
reader last looked, don't re-summarize the world every time.

## Layout

Reading content — headlines, summaries, prose — stays narrow, at a width
suited to reading, not to filling the viewport. Situational-overview
sections (a NOW grid, an arc list) may use width, but that's controlled
variation earned by the content, not a uniform multi-column grid applied
everywhere out of habit.

No card soup. Prefer whitespace, typography, thin separators, subtle
elevation, timeline structures, and meaningful alignment to communicate
structure. Reach for a card only when something is genuinely a distinct,
interactive object — not as the default container for every piece of
content.

## Typography

Monospace is reserved for timestamps, metadata, numbers, statuses, and
small labels — never body paragraphs. That's what breaks the
"intelligence terminal" read into "generic dev tool." Four registers need
to stay visually distinct: interface metadata (small mono), headlines
(editorial), summary text (comfortable reading size and line height), and
status labels (compact, understated). Today too many elements carry
similar visual weight, which is exactly what forces the reader to read
everything instead of scanning — the redesign's job is letting the eye
jump registers without reading every line.

## Color

Periwinkle stays the primary accent — navigation, interaction, the site's
signature. Additional colors are used sparingly and only when they carry
meaning: blue for informational/new, amber for an important change or
something that deserves attention, red reserved for genuine escalation,
green for resolution, gray for stable/historical content. Color
communicates state, not decoration — don't turn every piece of metadata
into a colored pill; a page where everything is colored is a page where
color means nothing.

## Calm urgency

Urgency gets communicated, not performed. Subtle markers — an "↑
ESCALATING"-style indicator — do the job that giant BREAKING banners,
flashing elements, or cable-news chrome would otherwise do, without the
theatrics. The posture is intelligence analyst, not news channel: the
interface stays calm even when the underlying news is chaotic. Calm is
also what makes a real escalation marker mean something the one time it's
used.

## Motion

Motion is a light touch, not a feature. A brief fade for new content, a
subtle pulse on genuinely unseen items, smooth expansion when a section
opens, a quiet appearance for a "you were here" marker — that's the
budget. No bouncing, no glow, no gradients used as decoration, no animated
backgrounds, nothing that moves without a reason tied to new or changed
content.

## Mobile

Mobile is one column, and density carries over — it is not an excuse to
inflate every element into a huge touch-friendly card. Metadata may
collapse or truncate, but nothing is hover-only, and diffs/deltas must
stay understandable at the narrower width. Mobile is not a shrunk desktop
page; it's a first-class layout in its own right. Desktop, in turn, can
use what mobile can't: 2–3 column overview layouts and richer hover
states.

## Evidence over certainty

Every label states only what's provable. Data-derived signals — an arrow
showing appearance frequency across digests, a count of updates — are
fine, because the data backs them. Severity words ("escalating") and
confidence percentages are claims about the world, and the UI doesn't get
to make them unless the pipeline that feeds it actually backs the claim
with a model judgment or a verification pass behind it. Never fake
precision: a number like "93.71% confidence" is worse than no number,
because it borrows authority the pipeline hasn't earned. See PLAN.md
§11.1's momentum-label guardrail and §11.4's confidence-badge guardrail for
where this principle is already load-bearing.

## Anti-patterns to name and reject

- **Generic AI-news app.** Gradient hero, "Stay informed with AI ✨" copy,
  emoji-laden marketing chrome. This product is not selling itself.
- **Generic SaaS dashboard.** KPI cards, pie charts, a metrics-dashboard
  aesthetic borrowed from B2B analytics tools. Nothing here is a KPI.
- **Bloomberg-terminal clone.** Take the density and the information
  discipline; leave the terminal appearance — green-on-black, ticker
  chrome, cramped multi-pane layouts — behind.
- **Cyberpunk command center.** No neon, no holographic effects, no
  glowing borders. Dark and periwinkle is not an invitation to go further.
- **Newspaper homepage.** The product's strength is structured
  information — arcs, deltas, timelines — not photography or a
  front-page-of-the-Times layout built around images.
