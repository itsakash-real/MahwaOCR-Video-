# Build prompt — Solo Swordmaster, chapters 1-10

Paste this whole file's content to your agent (Claude Code, Antigravity, etc.)
to kick off a real 10-chapter build end to end.

```
/make-recap-video Solo Swordmaster, chapters 1-10

https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/1
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/2
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/3
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/4
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/5
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/6
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/7
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/8
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/9
https://asurascans.com/comics/solo-swordmaster-11fe7da3/chapter/10

This is a real build, not a test — run all 10 chapters, in order, not just
chapter 1. Follow docs/FULL_PIPELINE_WORKFLOW.md exactly for each chapter
(transcript -> recap -> consistency check -> voice/TTS-readiness -> Deepgram
synthesis -> audio self-check), then docs/CHAPTER_BUILD_WORKFLOW.md for that
chapter's video half. Re-read both files fresh before starting each new
chapter's Stage 0, even if you just finished the previous chapter a moment
ago — don't rely on memory of them from earlier in this same conversation.

Do all 10 chapters in this one request; don't stop after chapter 1 unless
you hit something that needs my decision (a missing API key, a genuine code
bug, an ambiguous pairing that needs a judgment call you're not confident
in).
```

## Notes for next time

- Change the series name, chapter count, and links below for a new series —
  everything else in the bracketed instructions stays the same.
- If you only want a cheap connectivity/pipeline test before committing to
  all 10 chapters, add back: `Use only chapter 1 for now — this is a test of
  the pipeline, not a real build.` That line is what was in the earlier draft
  of this prompt and is why it stopped after chapter 1 last time; it's
  removed here on purpose since this is the real 10-chapter run.
- Before sending this, confirm both API keys are visible in the terminal the
  agent will actually run in (new terminal needed if you just set them):
  ```powershell
  echo $env:DEEPSEEK_API_KEY
  echo $env:DEEPGRAM_API_KEY
  ```
