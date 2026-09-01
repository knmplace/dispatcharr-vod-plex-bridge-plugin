# v2.5.0 Release Notes

Covers everything since the last committed release (v2.4.9, `12eb205`). Versions 2.4.10–2.4.16 were working/uncommitted increments along the way — this release bundles all of that work into one clean, documented boundary.

**Status: in user testing, not yet tagged/committed.** See handoff bead `gtvb` for current test status before treating this as final.

---

## Callout: duplicate catalog entries

Not every catalog has duplicates — if your provider setup doesn't produce overlapping titles, none of the duplicate-handling work below changes anything you'll see. But if it does (the same movie or series showing up more than once, often from multiple providers or repeated imports), this release is aimed squarely at that problem:

- Duplicate rows now collapse into a single dashboard card instead of showing as separate entries.
- The card that gets activated into Plex is the **best available copy**, not just whichever one happened to load first — for movies that means preferring the copy with a poster and a confirmed TMDB match; for series it means preferring the copy with the most complete episode set.
- If you're ever unsure why one copy won over another (especially on a close call, like a tie), hovering the "N copies" badge now tells you the score **and which provider each copy came from** — so a `99 vs 82` (or an `81 vs 81` tie) isn't just numbers anymore, it's "Provider A (99) vs Provider B (82)."

Net effect: when you review the catalog before activating into Plex, you're looking at one clean entry per title, and you can trust that entry is the strongest copy available — with the option to check exactly why, if you want to.

---

## Features

- **Duplicate detection and grouping** — movies and series with matching (or near-matching, after normalization) titles/years are grouped and shown as a single card with a "N copies" badge, instead of cluttering the browse grid with redundant entries.
- **Ranked-winner activation** — within a duplicate group, the plugin now picks a single "best" candidate to represent (and activate) the group, using a ranking signal: for movies, poster + confirmed TMDB match; for series, episode-count completeness.
- **Provider-name tooltip on duplicate badges** — hovering a "N copies" badge shows each candidate's score paired with the provider it came from (e.g. `score 99 (Provider A) vs 82 (Provider B)`), so tie cases and close calls are explainable at a glance instead of being just numbers.
- **Adult-category filter** — new "Hide Adult Categories" setting (on by default) excludes any category whose name contains "Adult"/"Adults" from the browse grid, category filters, and catalog summary. Can be unchecked to show them again.

## Fixes

- **Duplicate-grouping title normalization** — several real-world title variants that previously failed to group together now correctly collapse into one card:
  - Embedded year suffixes in different formats (`Title (1984)` vs `Title - 1984` vs bare `Title`).
  - Leading "The " article differences (`The Host (2020)` vs `Host (2020)`).
  - Doubled year tags (`42 (2013) (2013)` vs `42 (2013)`).
  - Mixed dash/colon subtitle separators (`Name - Subtitle` vs `Name: Subtitle`).
- **Series tab load-time fix** — a per-row N+1 query (`episodes.count()` called once per series across the *entire* filtered/grouped set, not just the visible page) was causing 30–45 second load times on catalogs with many series. Replaced with a single batched query. Measured load time after the fix: ~1.9 seconds.
- **Duplicate-badge visual rendering** — the "N copies" badge is now a full-width ribbon anchored cleanly to the bottom of the poster art on every card size, fixing two CSS positioning bugs (a percentage-based offset that miscalculated against the card's total height instead of the poster's, and an inherited style that caused the badge to stretch across most of the card instead of sizing to its content).

## Performance

- **O(n²) → near-linear duplicate grouping** — the original duplicate-detection pass compared every catalog item against every other item; reworked to bucket by normalized title first, cutting comparison volume dramatically on large catalogs.
- **Batched provider-name lookups** — the new provider-name tooltip fetches all provider names for a page of results in one query, rather than one query per duplicate group, so the transparency feature doesn't reintroduce the N+1 pattern that was just fixed elsewhere.

## Not changed

- **Activation logic itself is unaffected.** The ranking signal that picks the "best" candidate in a duplicate group (poster/TMDB presence for movies, episode-count for series) is unchanged from prior behavior — this release only adds visibility into *why* that pick was made (via the provider-name tooltip) and *where* it applies (grouping/display), not a new decision rule.
- **No database-level merge or deletion.** Duplicate collapsing is a display/activation-selection behavior only — all underlying catalog rows remain intact in Dispatcharr's database. A manual duplicate-picker UI and DB-level consolidation are still out of scope for this release (tracked separately).

---

## Testing

- `pytest` suite: 20/20 passing (grouping-logic and adult-category-filter unit tests, no DB dependency required).
- Live-verified against the production catalog post-deploy: adult-category filter (0/60 categories match "adult" with the default setting), series tab load time (~1.9s), duplicate-badge visual rendering (Movies and Series tabs), and the provider-name tooltip against a real tie case.

## Tracking

- GitHub issue: [#6](https://github.com/knmplace/dispatcharr-vod-plex-bridge-plugin/issues/6) — before/after progress comment posted.
- Beads: `cuyc` (master tracking issue), `gtvb` (this release's handoff bead).
