# Rules for AI agents (read first, these override everything else)

This folder is the **program**. You may **run** it. You may **never change** it.

## Never

- Never edit, create, rename, move or delete any file in this folder
  (`manhwa_ocr/`, `tests/`, `AGENTS.md`, `GEMINI.md`, `.agent/`, `pyproject.toml`, ...).
- Never write scripts, notes or scratch files here. Put scratch under the
  series' `.work/` folder in the output folder (`C:\Users\akash\Documents\ManhwaRecaps\<series>\.work\`).
- Never run `python -m manhwa_ocr guard unlock` or `guard lock`. Only the user does that.
- Never use `attrib`, `icacls`, `chmod`, `git checkout`, `git reset`, `pip install` or similar
  to get around a read-only or locked file in this folder.
- Never "fix" the tool by patching its code, even if a command fails or the output looks wrong.

## When something goes wrong

Stop and tell the user: the exact command, the exact error, and what you expected.
That's the whole job. The user decides about code changes.

## What you may do

- Run `python -m manhwa_ocr ...` commands (from any folder).
- Read `AGENTS.md` in this folder for how to drive the tool.
- Write only the files those instructions name, inside the output folder
  (for example `pairs.json`, `fixes.json`, `cues.json`, `tile_results.json`, `parts.json`).
- Log every manual edit (file, what, why) in `.work\agent_log.md`. Never edit `plan.json` or `alignment.json` by hand.
- The full rules every pasted prompt starts with are `COMMON_RULES` in `manhwa_ocr/agent_prompts.py`; read `AGENTS.md`.

The program is locked: if any file here changes, every command refuses to run
until the user restores it. Editing it only stops your own work.
