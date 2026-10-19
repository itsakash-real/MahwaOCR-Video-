# Shorts guide — Shorts + end-screen + WATCH NEXT

## Cut the Shorts (after the long video renders)

```bash
python -m manhwa_ocr video shorts --chapter N --session "Your Series Name"
python -m manhwa_ocr video shorts --chapter N --count 2 --session "Your Series Name"   # fewer than 3
```

What it does:
- Picks up to 3 non-overlapping 30–45s windows: hook first, then
  shake/flash/callout peaks, ≥1s gaps, snapped to shot boundaries.
  Short chapters get one full-length window.
- Renders vertical 9:16 with the existing crop rules, keeps keyword
  callouts, ends each Short on a "Full story on channel" card.
- Slices your recorded narration via `audio_offset` — never re-records
  or generates audio.
- Writes `video/chapter_N.shorts_1..3.mp4` + `video/chapter_N.shorts.json`
  (manifest: title suggestion, hook, panels, end card). The manifest is
  validated on write.

Manual alternative (one hand-cut Short): `video clip --chapter N --from
45 --to 105 --format 9:16` writes `video/chapter_N.clip_45-105_9x16.mp4`.

## End-screen + WATCH NEXT

```bash
python -m manhwa_ocr video endscreen --chapter N --session "Your Series Name"
# -> video/chapter_N.endscreen.json + video/chapter_N.watch_next.txt
```

`video plan` / `video build` already write both files for every chapter.
The end-screen is a **spec**, not pixels: YouTube end-screen cards can
only be added in YouTube Studio (last 5–20s of the video). The spec
gives you the window (last 20s) plus slots: next_video / playlist /
subscribe. The `watch_next.txt` file is the WATCH NEXT block to paste
into the description.

In YouTube Studio: End screen → add the next video in the series
(playlist first), then Subscribe. Say the next video's name out loud in
the last 15–20s of the narration (the recap's bridge outro) so viewers
have a reason to click.

## Upload workflow

1. **Long video first.** Upload `video/chapter_N.mp4`. Title: weak/trash
   setup + hidden OP twist + stakes + `| Manhwa Recap` (≤~70 chars so it
   does not cut off on mobile). Description: 2 hook lines, then chapters
   / timestamps from the plan's chapter cards, then the WATCH NEXT block
   from `watch_next.txt`, then 5–8 hashtags.
2. **Shorts, one per day after.** For each entry in `shorts.json`: upload
   the MP4 as a Short, title from the manifest's title suggestion, first
   line = the hook. Do NOT upload all three at once — one per day keeps
   the funnel alive.
3. **Pin a comment** on the long video: the cliffhanger question from the
   recap's `[LOOP: ...]` / ending. Reply to every comment in the first
   24h.
4. **Playlist:** add the long video to the series playlist ("Full
   Story" / arc playlist), in order. The Shorts point back to the
   playlist via the end card + description link.

## Checklist before you publish a chapter

- [ ] Sync gate passed (render refused otherwise).
- [ ] `video status` shows zero `timing_warnings`.
- [ ] Spot-checked first 60s + one mid-video minute: no grey oval, no
      wrong picture, no frozen panel, no unreadable text panel.
- [ ] Chapter cards have stakes-based titles (override in `fixes.json`
      if an auto title is bland).
- [ ] Shorts manifest exists (3 Shorts, or fewer if you passed `--count`).
- [ ] End-screen spec + WATCH NEXT block exist.
