"""Persistent series memory -- the "story bible".

This is the piece that makes chapter 6 recognize a character introduced in
chapter 1. After each chapter the model proposes updates; those are merged into
a JSON file and injected into the prompt for every later chapter.

Design rules:
  * Append-biased. Existing facts are never silently dropped, because a later
    chapter seeing a character in shadow should not overwrite a clean
    description from their introduction.
  * Visual descriptors are first-class. "silver hair, red eyes, black gloves" is
    what lets the model re-identify someone it cannot name.
  * Tolerant of sloppy model output. Strings where lists belong, lists where
    dicts belong, name capitalisation drift -- all absorbed, never a crash.
  * Plain JSON, hand-editable. Every save keeps the previous version alongside
    as series_memory.prev.json, so a bad edit or a bad merge is one copy away
    from undone.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .util import UserError, as_list, atomic_write_json, chapter_key, read_text_any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, limit: int = 600) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value if v)
    return str(value).strip()[:limit]


def _strings(value: Any, limit: int = 300) -> List[str]:
    return [s for s in (_text(v, limit) for v in as_list(value)) if s]


def _chapter_num(label: str) -> tuple:
    """'chapter 12' -> sortable key, for recency ordering."""
    m = re.search(r"([\w.-]+)\s*$", label or "")
    return chapter_key(m.group(1) if m else "")


@dataclass
class SeriesMemory:
    series: str = "untitled"
    characters: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    world_facts: List[str] = field(default_factory=list)
    plot_threads: List[Dict[str, Any]] = field(default_factory=list)
    glossary: Dict[str, str] = field(default_factory=dict)
    chapters_done: List[str] = field(default_factory=list)
    # When each world fact / glossary term was first learned, so a recap of an
    # early chapter can be briefed without spoilers from later ones.
    learned: Dict[str, str] = field(default_factory=dict)
    updated_at: str = field(default_factory=_now)

    # ---------- persistence ----------

    @classmethod
    def load(cls, path: Path, series: str = "untitled") -> "SeriesMemory":
        if not path.exists():
            return cls(series=series)
        raw = read_text_any(path)
        if not raw.strip():
            return cls(series=series)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Never fall back to an empty memory here: the next save would
            # overwrite the user's real bible with nothing.
            prev = path.with_name("series_memory.prev.json")
            hint = f" The previous good version is in {prev.name}." if prev.exists() else ""
            raise UserError(
                f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}). "
                f"Fix the file by hand.{hint}"
            ) from None
        if not isinstance(data, dict):
            raise UserError(f"{path} should contain a JSON object.")

        mem = cls(series=data.get("series") or series)
        chars = data.get("characters")
        entries: List[tuple[str, Dict[str, Any]]] = []
        if isinstance(chars, dict):
            entries = [(k, v) for k, v in chars.items() if isinstance(v, dict)]
        elif isinstance(chars, list):  # hand-edited into a list: recover
            entries = [(_text(c.get("name"), 80), c) for c in chars if isinstance(c, dict)]
        for key, char in entries:
            # The "name" field is what users edit to fix a wrong name; the dict
            # key is incidental. Re-key on the field so a correction sticks,
            # and remember the old key as an alias so it still matches.
            name = _text(char.get("name"), 80) or _text(key, 80)
            if not name:
                continue
            char = dict(char)
            char["name"] = name
            aliases = _strings(char.get("aliases"), 80)
            if key and key.casefold() != name.casefold() and key not in aliases:
                aliases.append(key)
            char["aliases"] = aliases
            mem.characters[name] = char
        mem.world_facts = _strings(data.get("world_facts"))
        mem.plot_threads = [t for t in as_list(data.get("plot_threads")) if isinstance(t, dict)]
        gl = data.get("glossary")
        mem.glossary = {str(k): _text(v) for k, v in gl.items()} if isinstance(gl, dict) else {}
        mem.chapters_done = _strings(data.get("chapters_done"))
        learned = data.get("learned")
        mem.learned = {str(k): str(v) for k, v in learned.items()} if isinstance(learned, dict) else {}
        mem.updated_at = data.get("updated_at") or _now()
        return mem

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 0:
            shutil.copy2(path, path.with_name("series_memory.prev.json"))
        self.updated_at = _now()
        atomic_write_json(
            path,
            {
                "series": self.series,
                "updated_at": self.updated_at,
                "chapters_done": self.chapters_done,
                "characters": self.characters,
                "world_facts": self.world_facts,
                "plot_threads": self.plot_threads,
                "glossary": self.glossary,
                "learned": self.learned,
            },
        )

    # ---------- merging ----------

    def _find(self, name: str, aliases: List[str]) -> str | None:
        """Match an incoming character to an existing one, case-insensitively,
        by name or alias in either direction."""
        wanted = {n.casefold() for n in [name, *aliases] if n}
        for key, char in self.characters.items():
            known = {key.casefold(), _text(char.get("name")).casefold()}
            known |= {a.casefold() for a in _strings(char.get("aliases"))}
            if wanted & known:
                return key
        return None

    def _incoming_characters(self, raw: Any) -> List[Dict[str, Any]]:
        if isinstance(raw, dict):
            # {"Raon": {...}} instead of [{"name": "Raon", ...}]
            out = []
            for k, v in raw.items():
                if isinstance(v, dict):
                    out.append({"name": k, **v})
                elif isinstance(v, str):
                    out.append({"name": k, "appearance": v})
            return out
        out = []
        for item in as_list(raw):
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str) and item.strip():
                out.append({"name": item.strip()})
        return out

    def merge(self, update: Dict[str, Any], chapter: str) -> List[str]:
        """Merge a model-proposed update. Returns human-readable change notes."""
        if not isinstance(update, dict):
            update = {}
        notes: List[str] = []

        for entry in self._incoming_characters(update.get("characters")):
            name = _text(entry.get("name"), 80)
            if not name:
                continue
            aliases = [a for a in _strings(entry.get("aliases"), 80) if a.casefold() != name.casefold()]
            key = self._find(name, aliases)

            if key is None:
                self.characters[name] = {
                    "name": name,
                    "aliases": aliases,
                    "appearance": _text(entry.get("appearance")),
                    "role": _text(entry.get("role"), 200),
                    "speech_style": _text(entry.get("speech_style"), 200),
                    "notes": _strings(entry.get("notes")),
                    "first_seen": chapter,
                    "last_seen": chapter,
                }
                notes.append(f"+ character: {name}")
                continue

            existing = self.characters[key]
            existing["last_seen"] = chapter
            known_aliases = _strings(existing.get("aliases"), 80)
            # If the model used a different spelling of the name, keep it as an alias.
            for alias in aliases + ([name] if name.casefold() != key.casefold() else []):
                if alias.casefold() not in {a.casefold() for a in known_aliases + [key]}:
                    known_aliases.append(alias)
                    notes.append(f"  {key}: alias '{alias}'")
            existing["aliases"] = known_aliases
            # Only fill blanks -- never overwrite an established description.
            for field_name, limit in (("appearance", 600), ("role", 200), ("speech_style", 200)):
                val = _text(entry.get(field_name), limit)
                if val and not _text(existing.get(field_name)):
                    existing[field_name] = val
                    notes.append(f"  {key}: {field_name} set")
            existing_notes = _strings(existing.get("notes"))
            for note in _strings(entry.get("notes")):
                if note not in existing_notes:
                    existing_notes.append(note)
            existing["notes"] = existing_notes[-30:]

        for fact in _strings(update.get("world_facts")):
            if fact.casefold() not in {f.casefold() for f in self.world_facts}:
                self.world_facts.append(fact)
                self.learned[f"fact:{fact}"] = chapter
                notes.append(f"+ world fact: {fact[:60]}")

        glossary = update.get("glossary")
        if isinstance(glossary, list):  # [{"term": ..., "meaning": ...}] or ["term: meaning"]
            pairs = {}
            for g in glossary:
                if isinstance(g, dict):
                    term = _text(g.get("term") or g.get("name"), 80)
                    if term:
                        pairs[term] = _text(g.get("meaning") or g.get("definition"))
                elif isinstance(g, str) and ":" in g:
                    term, _, meaning = g.partition(":")
                    pairs[term.strip()] = meaning.strip()
            glossary = pairs
        if isinstance(glossary, dict):
            for term, meaning in glossary.items():
                term = _text(term, 80)
                if term and term.casefold() not in {t.casefold() for t in self.glossary}:
                    self.glossary[term] = _text(meaning)
                    self.learned[f"term:{term}"] = chapter
                    notes.append(f"+ glossary: {term}")

        for thread in as_list(update.get("plot_threads")):
            if isinstance(thread, str):
                thread = {"thread": thread, "status": "open"}
            if not isinstance(thread, dict):
                continue
            label = _text(thread.get("thread") or thread.get("question"), 300)
            if not label:
                continue
            status = "resolved" if _text(thread.get("status")).casefold() in {"resolved", "closed", "done"} else "open"
            found = next(
                (t for t in self.plot_threads if _text(t.get("thread")).casefold() == label.casefold()),
                None,
            )
            if found:
                found["status"] = status
                found["last_seen"] = chapter
            else:
                self.plot_threads.append(
                    {"thread": label, "status": status, "opened": chapter, "last_seen": chapter}
                )
                notes.append(f"+ thread: {label[:60]}")

        if chapter not in self.chapters_done:
            self.chapters_done.append(chapter)
        return notes

    # ---------- prompt rendering ----------

    def as_briefing(self, max_chars: int = 6000, *, upto: str | None = None) -> str:
        """Render the bible as the context block injected into chapter prompts.

        Characters seen most recently come first, so when a long series
        outgrows the budget it's the long-absent extras that get cut, not the
        current cast.

        `upto="7"` limits the briefing to what was known by chapter 7: used for
        recaps, so writing up an early chapter can't leak later reveals.
        Entries recorded before chapter tracking existed are kept.
        """
        limit = chapter_key(upto) if upto is not None else None

        def known(label: str | None) -> bool:
            if limit is None or not label:
                return True
            return _chapter_num(label) <= limit

        characters = [c for c in self.characters.values() if known(_text(c.get("first_seen")))]
        facts = [f for f in self.world_facts if known(self.learned.get(f"fact:{f}"))]
        glossary = {t: m for t, m in self.glossary.items() if known(self.learned.get(f"term:{t}"))}
        threads = []
        for t in self.plot_threads:
            if not known(_text(t.get("opened"))):
                continue
            # A thread resolved after the cut-off was still open at that point.
            resolved_by_then = t.get("status") != "open" and known(_text(t.get("last_seen")))
            if not resolved_by_then:
                threads.append(t)

        if not characters and not facts:
            return "(This is the first chapter. No prior context established yet.)"

        lines: List[str] = []
        if characters:
            lines.append("KNOWN CHARACTERS")
            lines.append(
                "Use these names when you recognize someone. Match on appearance "
                "when a character is on-page but unnamed."
            )
            ordered = sorted(
                characters,
                key=lambda c: _chapter_num(_text(c.get("last_seen"))),
                reverse=True,
            )
            for char in ordered:
                bits = [f"- {_text(char.get('name'), 80)}"]
                aliases = _strings(char.get("aliases"), 80)
                if aliases:
                    bits.append(f"(aka {', '.join(aliases)})")
                if _text(char.get("role")):
                    bits.append(f"-- {_text(char.get('role'), 200)}")
                lines.append(" ".join(bits))
                if _text(char.get("appearance")):
                    lines.append(f"    looks like: {_text(char.get('appearance'))}")
                if _text(char.get("speech_style")):
                    lines.append(f"    speech: {_text(char.get('speech_style'), 200)}")
            lines.append("")

        if threads:
            lines.append("OPEN PLOT THREADS")
            for thread in threads[-15:]:
                lines.append(f"- {_text(thread.get('thread'))} (since {thread.get('opened', '?')})")
            lines.append("")

        if facts:
            lines.append("ESTABLISHED FACTS")
            for fact in facts[-25:]:
                lines.append(f"- {fact}")
            lines.append("")

        if glossary:
            lines.append("TERMINOLOGY")
            for term, meaning in list(glossary.items())[-20:]:
                lines.append(f"- {term}: {meaning}")
            lines.append("")

        out = "\n".join(lines).strip()
        if len(out) > max_chars:
            cut = out.rfind("\n", 0, max_chars)
            out = out[: cut if cut > 0 else max_chars] + "\n... (briefing truncated)"
        return out

    def summary(self) -> str:
        return (
            f"{self.series}: {len(self.characters)} characters, "
            f"{len(self.world_facts)} facts, "
            f"{len([t for t in self.plot_threads if t.get('status') == 'open'])} open threads, "
            f"{len(self.chapters_done)} chapters processed"
        )
