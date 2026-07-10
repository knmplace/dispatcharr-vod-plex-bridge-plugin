# Audio Detection Plan

## Goal

Detect VOD movie streams that have video but no usable audio track, so the plugin can avoid sending a bad `302` redirect to Plex whenever possible.

This plan is intentionally conservative:

- Do not add a heavy probe to every play request by default.
- Do not reintroduce the old hot-path liveness-probe problem.
- Prefer one-time or rare validation, then cache the result.
- Keep provider impact low and predictable.

## Current Reality

Today the plugin does **not** inspect media streams before activation or before the `302` redirect.

- Activation only records state and writes `.strm` / `.nfo`.
- `HEAD /vod/...` is synthetic metadata only.
- `GET /vod/...` chooses a relation and returns a `302` to Dispatcharr.
- The stall watchdog only reacts after Plex is already trying to play.

So a stream with no audio can currently make it all the way to Plex and appear as endless buffering.

## Recommendation

Use a **cached audio-validation model** instead of a per-play probe.

Recommended phases:

1. Add a lightweight design for storing per-movie/per-stream audio validation state.
2. Add an **on-demand/manual probe** first so we can test safely against known-bad examples.
3. If that works reliably, add an **optional background or activation-time probe** with strict rate limits.
4. Only after that, consider a **pre-302 guard** that uses cached results only, not a fresh live probe.

This gives us the best chance of blocking no-audio streams without adding noticeable playback delay.

## Best Future Behavior

### Before play

On `GET /vod/...`:

- Look up the chosen `stream_id`.
- Check cached validation state.
- If cached state says `audio_missing`, skip that relation and try the next provider mapping.
- If cached state says `audio_ok`, return the `302` immediately.
- If there is no cached result, return the `302` immediately unless the user explicitly enables a stricter mode.

Important: the default pre-`302` behavior should use **cache only**.

That avoids adding new latency to normal playback.

### During activation or manual Reactivate

Optional future behavior:

- Probe the selected stream once.
- Save:
  - `last_audio_check_at`
  - `audio_status` = `ok` / `missing` / `unknown` / `probe_failed`
  - `audio_stream_count`
  - `codec_summary`
  - `checked_stream_id`
  - `checked_account_id`
- If the probe says `missing`, immediately try the next relation and probe that one instead.

This is the safest place to spend a little extra time because it happens far less often than playback.

## Minimal-Impact Probe Strategy

### Preferred approach

Use a **single short ffprobe-style media inspection** against the Dispatcharr proxy URL for one chosen relation, with strict caps:

- One probe per movie/relation unless manually refreshed.
- Short timeout.
- Small read budget.
- No parallel bursts.
- No repeated retries in the same request path.

What we want from the probe:

- Are there any audio streams?
- How many?
- What codec(s)?
- Did the probe fail before media streams were identified?

### Why not HEAD

`HEAD` is not enough.

- It can confirm the route responds.
- It cannot reliably confirm that a real audio stream exists.
- It cannot distinguish "video only" from "normal file" in a dependable way.

### Why not probe every play

Per-play probing would likely:

- Add startup latency to Plex playback.
- Increase provider hits during seeks/retries.
- Recreate the same class of hot-path side effects this plugin already backed away from.

## Suggested Validation States

Store validation by `movie_id` and `stream_id`, not just by movie.

Example state:

```json
{
  "audio_validation": {
    "12345": {
      "stream_id": "998877",
      "account_id": "12",
      "status": "ok",
      "audio_stream_count": 1,
      "audio_codecs": ["aac"],
      "checked_at": 1780000000,
      "method": "ffprobe",
      "expires_at": 1780604800
    }
  }
}
```

Suggested statuses:

- `ok`
- `missing`
- `unknown`
- `probe_failed`

Suggested TTL:

- Reuse existing result for several days.
- Clear or recheck when:
  - stream pick is manually refreshed
  - movie is reactivated
  - relation changes to a different provider
  - user explicitly requests revalidation

## Recommended Rollout

### Phase 1: Planning and sample collection

Goal:

- Identify one or more real Dispatcharr movies that reproduce the "video but no audio" Plex-buffering problem.

Need from testing:

- Movie id
- Movie title
- Provider account / relation if known
- Whether Dispatcharr web playback has video but no audio
- Whether Plex buffers forever or eventually errors

### Phase 2: Manual diagnostic tool only

Add a manual diagnostic entry point, not tied to playback yet.

Examples:

- dashboard button: `Check Audio`
- API route: `POST /api/movies/check-audio`

Behavior:

- Probe one selected movie/relation.
- Return structured results.
- Save the result to state/logs.

This lets us validate whether the detection is technically reliable before changing playback behavior.

### Phase 3: Optional activation-time validation

If Phase 2 works:

- Add an optional setting such as `validate_audio_on_activate`.
- When enabled, activation probes only newly activated movies.
- Limit checks to one movie at a time with delay between probes.

This keeps the footprint bounded and predictable.

### Phase 4: Cache-only pre-302 guard

If cached results prove trustworthy:

- Before returning a `302`, skip any relation already marked `audio_missing`.
- Fall through to another relation if one exists.
- If every known relation is bad, either:
  - return the normal best guess, or
  - fail fast with a clear error message

Recommended default:

- If all results are unknown, do not block playback.
- If one result is known-good, prefer it.
- If one result is known-bad, skip it.

## Latency Guidance

To protect playback startup:

- Do not run a fresh audio probe in the default `GET /vod/...` path.
- Only use cached audio results there.
- If a strict live-check mode ever exists, it should be opt-in and clearly labeled as slower.

This matches your priority:

- keep dead-stream handling
- avoid hammering providers
- avoid making Plex startup even slower

## Failure Modes To Expect

Even a careful probe may have edge cases:

- Some containers may need more than a tiny read to expose stream metadata.
- Some providers may rate-limit or stall probes.
- Some streams may have an audio track declared in metadata but still be effectively broken in playback.
- Some Plex buffering cases may still be unrelated to audio.

So the first goal should be:

Detect the obvious "no audio stream present" case reliably enough to improve provider selection, not solve every playback issue in one pass.

## Proposed Repo Work Items

When we move from planning to implementation, the likely steps are:

1. Add a new audio-validation state structure in `bridge.py`.
2. Add a manual diagnostic API route in `server.py`.
3. Add a small probe helper module or utility.
4. Add dashboard UI for manual testing and visibility.
5. Log audio-validation results to the activity log.
6. Add cache-aware relation skipping in `get_redirect_url()`.
7. Optionally add activation-time validation behind a setting.

## What I Need From You Next

The best next step is a real failing sample from Dispatcharr.

Once you find one, we can test that exact movie and answer the key question:

"Can we detect the missing-audio condition cheaply and reliably enough to cache it and use it before the `302`?"

That sample will tell us whether the plan should stay cache-first, become activation-time validation, or be kept manual-only.
