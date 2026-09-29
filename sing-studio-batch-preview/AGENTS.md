# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

Build app UI in `src/`. Keep `.openai/hosting.json`, `worker/index.js`, `scripts/prepare-sites-build.mjs`, and `tests/sites-worker.test.mjs` intact so the same local prototype can be handed to Sites. Before a Sites handoff, run `npm run build` and `npm run test:sites`; the build must leave `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.

## Confirmed brief — 2026-09-28
- Current scope: the user explicitly approved this UI on 2026-09-28 and requested actual functionality. Connect local production features and verify them. No repeated UI approval is required.
- Preserve existing forest-green / ivory Sing Studio design language.
- All future generations use YuE2. Whole songs, typically 3–5 minutes. Prefer quality over speed; aim to preserve original length, melody, section order.
- Support both reinterpretation and preserving the original vocal recording, pending backend verification.
- Import more than 20 songs at once; each reference audio sits on the left, its optional lyrics on the right.
- Missing lyrics: read embedded lyrics or transcribe automatically, then continue without per-song confirmation.
- Shared style and mode with per-song overrides. Failure retries once, then proceeds to next song.
- Deliver full WAV and MP3; stems are not requested.
- Remove fictional demo states when integrating production. Audio and lyrics stay local; do not upload them to external model services.

## Confirmed dual-version brief — 2026-09-28
- Continue YuE2 only and local processing. User now wants selectable speed versus quality; this supersedes the earlier unconditional quality-first preference.
- Target 5–10 minutes total for two complete versions of an approximately 3-minute song; report measured results and limitations, not guaranteed timing.
- Generate A/B full songs directly, with different styles and different performances. No short-preview approval step.
- Allow singing gender and timbre, globally and per song. Preserve-original mode keeps the recorded voice and disables those controls.
- Keep both versions, allow independent favorites and recoverable deletion.
- Queue supports individual/bulk removal and stopping an active job, preserving imported originals and unrelated works.
- Show actual processing time, excluding queue waiting; include active automatic retry processing, exclude paused/restart downtime.
- Preserve the approved forest-green / ivory interface. No new visual approval required for these requested controls.

## Confirmed change-control brief — 2026-09-29
- User wants substantial adjustable changes to style and timbre. Strength must affect the actual YuE2 request.
- Expose reference melody versus free recomposition; preserve-original always keeps reference melody and recorded vocals.
- Keep independent genre descriptions, explicit A/B requirements and per-song overrides.
- Show app release and generated-with version; update release.json, package/lock versions and CHANGELOG.md each release.
- Upload maintained source to the user-designated sing929/singmusic repository; exclude private audio, lyrics, state snapshots, credentials and models.
