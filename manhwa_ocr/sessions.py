"""Session management -- one isolated workspace per series.

Each session owns its own directory. Nothing is shared between sessions, so the
character bible for one series can never leak into another:

    <root>/
      active                      <- name of the current session
      the-hero-cannot-rest/
        session.json              <- metadata
        chapters.txt              <- the chapter URL list you paste into
        series_memory.json        <- this series' bible
        chapter_1.md ...
        ch1/ ch2/ ...             <- pages, tiles, intermediates
        .cache/
      some-other-series/
        ...

Switching is a pointer change, so going back to an earlier series leaves
everything exactly as it was.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .util import (
    atomic_write_json,
    atomic_write_text,
    chapter_key,
    normalize_label,
    read_text_any,
    valid_chapter,
)

DEFAULT_ROOT = Path.home() / ".manhwa-ocr" / "sessions"
ARCHIVE_DIRNAME = "_archived"
POINTER_NAME = "active"

# Two distinct concerns, deliberately separate:
#   RESERVED  - slugs a user may not claim, because they would be confusable
#               with store bookkeeping. These are post-slugify forms, so
#               "_archived" appears here as "archived".
#   SKIP_DIRS - directory names that are store internals, not sessions.
RESERVED = {"active", "archived"}
SKIP_DIRS = {ARCHIVE_DIRNAME, POINTER_NAME}

# Windows refuses these as directory names, with or without an extension.
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(10)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    """Folder-safe session name. Keeps non-Latin scripts (Korean, Hindi, ...)."""
    # Keep letters, digits and combining marks from any script. Plain \w drops
    # combining marks, which mangles Devanagari: "नायक" came out as "न-यक".
    chars = []
    for ch in (name or "").casefold():
        cat = unicodedata.category(ch)
        chars.append(ch if cat[0] in "LMN" or ch == "_" else "-")
    slug = "".join(chars)
    slug = re.sub(r"[-_]{2,}", "-", slug).strip("-_.")
    slug = slug[:64].strip("-_.")
    if not slug:
        raise ValueError(f"Cannot derive a folder name from {name!r}. Use some letters or digits.")
    if slug in RESERVED:
        raise ValueError(f"'{slug}' is reserved by the tool. Pick a different name.")
    if slug.split(".")[0] in _WINDOWS_RESERVED:
        raise ValueError(f"'{slug}' is a reserved name on Windows. Pick a different name.")
    return slug


@dataclass
class Session:
    slug: str
    path: Path
    name: str = ""
    created_at: str = ""
    last_used: str = ""
    url_template: str = ""
    notes: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def meta_path(self) -> Path:
        return self.path / "session.json"

    @property
    def memory_path(self) -> Path:
        return self.path / "series_memory.json"

    @classmethod
    def load(cls, path: Path) -> "Session":
        meta: Dict[str, Any] = {}
        mp = path / "session.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        known = {"name", "created_at", "last_used", "url_template", "notes"}
        return cls(
            slug=path.name,
            path=path,
            name=meta.get("name", path.name),
            created_at=meta.get("created_at", ""),
            last_used=meta.get("last_used", ""),
            url_template=meta.get("url_template", ""),
            notes=meta.get("notes", ""),
            extra={k: v for k, v in meta.items() if k not in known},
        )

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": self.name or self.slug,
            "created_at": self.created_at or _now(),
            "last_used": self.last_used or _now(),
            "url_template": self.url_template,
            "notes": self.notes,
            **self.extra,
        }
        atomic_write_json(self.meta_path, payload)

    def touch(self) -> None:
        self.last_used = _now()
        self.save()

    # ---------- chapter link list ----------

    @property
    def links_path(self) -> Path:
        return self.path / "chapters.txt"

    def read_links(self) -> List[tuple[str, str]]:
        """Parse chapters.txt into [(chapter_label, url), ...].

        One URL per line. Blank lines and `#` comments ignored. Chapters are
        numbered by position unless a line gives an explicit label:

            https://site.com/ch/one-slug        -> chapter 1
            https://site.com/ch/two-slug        -> chapter 2
            12 = https://site.com/ch/twelve     -> chapter 12
        """
        return self.parse_links()[0]

    def parse_links(self) -> tuple[List[tuple[str, str]], List[str]]:
        """Return (links, problems). Problems are human-readable line warnings."""
        if not self.links_path.exists():
            return [], []
        out: List[tuple[str, str]] = []
        problems: List[str] = []
        seen_labels = set()
        next_n = 1
        text = read_text_any(self.links_path)
        for lineno, raw in enumerate(text.splitlines(), start=1):
            # Allow trailing notes:  https://site/ch2   # the big fight
            line = re.sub(r"\s+#.*$", "", raw).strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^(\S+)\s*[=|]\s*(https?://\S+)$", line)
            if m:
                label, url = normalize_label(m.group(1)), m.group(2)
                if label.isdigit():
                    # Bare lines after an explicit number continue from it, so
                    # "47 = ..." followed by bare URLs gives 48, 49...
                    next_n = int(label) + 1
            elif re.match(r"^https?://\S+$", line):
                label, url = str(next_n), line
                next_n += 1
            else:
                if re.search(r"https?:|www\.|://|\.(com|net|org|io|xyz)\b", line, re.I):
                    # Looks like a broken link: it still takes its chapter
                    # number, so every later chapter keeps the right label.
                    problems.append(
                        f"line {lineno}: chapter {next_n} link is malformed (needs http:// or https://): {line[:60]}"
                    )
                    next_n += 1
                else:
                    problems.append(f"line {lineno}: not a link, ignored: {line[:60]}")
                continue
            if not valid_chapter(label):
                problems.append(f"line {lineno}: chapter label '{label}' has unsafe characters, ignored")
                continue
            if label in seen_labels:
                problems.append(f"line {lineno}: chapter {label} listed twice, keeping the first")
                continue
            seen_labels.add(label)
            out.append((label, url))
        return out, problems

    _HEADER = (
        "# Chapter links, one per line:   N = https://...\n"
        "# Bare URLs also work and are numbered after the last numbered line.\n"
        "# Blank lines and # comments are ignored.\n\n"
    )

    @staticmethod
    def _norm_url(u: str) -> str:
        return u.strip().rstrip("/")

    def add_links(self, urls: List[str]) -> int:
        """Append URLs with explicit chapter numbers. Returns how many added.

        Writing `N = url` rather than a bare URL pins each chapter's number, so
        later hand-edits to the file (inserting, reordering, deleting a line)
        can never relabel a chapter that has already been transcribed.
        """
        current = self.read_links()
        existing = {self._norm_url(u) for _, u in current}
        new: List[str] = []
        for u in urls:
            u = (u or "").strip()
            key = self._norm_url(u)
            if re.fullmatch(r"https?://\S+", u) and key not in existing:
                existing.add(key)
                new.append(u)
        if not new:
            return 0

        numeric = [int(c) for c, _ in current if c.isdigit()]
        start = (max(numeric) + 1) if numeric else 1

        old = read_text_any(self.links_path) if self.links_path.exists() else self._HEADER
        # A hand-edited file may not end in a newline; appending straight onto
        # it would fuse two links into one unparseable line.
        if old and not old.endswith("\n"):
            old += "\n"
        body = "".join(f"{start + i} = {u}\n" for i, u in enumerate(new))
        atomic_write_text(self.links_path, old + body)
        return len(new)

    def ensure_links_file(self) -> Path:
        if not self.links_path.exists():
            atomic_write_text(self.links_path, self._HEADER)
        return self.links_path

    # ---------- recaps ----------

    @property
    def recaps_dir(self) -> Path:
        return self.path / "recaps"

    def recap_path(self, chapter: str) -> Path:
        return self.recaps_dir / f"chapter_{chapter}.md"

    def recaps(self) -> List[str]:
        if not self.recaps_dir.exists():
            return []
        return _labels_in(self.recaps_dir)

    def compilations(self) -> List[Path]:
        if not self.recaps_dir.exists():
            return []
        return sorted(self.recaps_dir.glob("compiled_*.md"))

    def pending(self) -> List[tuple[str, str]]:
        """Links whose chapter has not been finalized yet."""
        done = set(self.chapters())
        return [(c, u) for c, u in self.read_links() if c not in done]

    def chapters(self) -> List[str]:
        """Chapters with a transcript. An empty file does not count as done."""
        return _labels_in(self.path)

    def summary(self) -> str:
        chaps = self.chapters()
        bits = [f"{len(chaps)} chapter{'s' if len(chaps) != 1 else ''}"]
        if chaps:
            bits.append(f"({', '.join(chaps[:8])}{'...' if len(chaps) > 8 else ''})")
        return " ".join(bits)


def _labels_in(folder: Path) -> List[str]:
    """chapter_<label>.md files in a folder, valid labels only, in chapter order.

    Zero-byte files are skipped so a crashed write can't mark a chapter done.
    chapter_N.brief.md / chapter_N.report.md are companions of a recap, not
    recaps themselves.
    """
    if not folder.exists():
        return []
    out = []
    for p in folder.glob("chapter_*.md"):
        label = p.stem[len("chapter_"):]
        if "." not in label and valid_chapter(label) and p.is_file() and p.stat().st_size > 0:
            out.append(label)
    return sorted(out, key=chapter_key)


class SessionStore:
    def __init__(self, root: str | Path | None = None):
        # --sessions-root, then $MANHWA_SESSIONS_ROOT, then `output_folder:` in
        # manhwa-ocr.yaml, then ~/.manhwa-ocr/sessions.
        if not root:
            root = os.environ.get("MANHWA_SESSIONS_ROOT")
        if not root:
            from .config import output_folder

            root = output_folder()
        self.root = Path(root or DEFAULT_ROOT).expanduser().resolve()

    # ---------- discovery ----------

    @property
    def _pointer(self) -> Path:
        return self.root / POINTER_NAME

    def list(self) -> List[Session]:
        if not self.root.exists():
            return []
        out = [
            Session.load(p)
            for p in sorted(self.root.iterdir())
            if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith(".")  # .agent, .git: not series
        ]
        return sorted(out, key=lambda s: s.last_used or "", reverse=True)

    def exists(self, name: str) -> bool:
        return (self.root / slugify(name)).is_dir()

    def get(self, name: str) -> Session:
        slug = slugify(name)
        path = self.root / slug
        if not path.is_dir():
            available = ", ".join(s.slug for s in self.list()) or "(none yet)"
            raise KeyError(f"No session {slug!r}. Available: {available}")
        return Session.load(path)

    # ---------- lifecycle ----------

    def create(
        self, name: str, *, url_template: str = "", activate: bool = True
    ) -> Session:
        slug = slugify(name)
        path = self.root / slug
        if path.exists():
            raise FileExistsError(f"Session {slug!r} already exists")
        path.mkdir(parents=True)
        sess = Session(
            slug=slug,
            path=path,
            name=name,
            created_at=_now(),
            last_used=_now(),
            url_template=url_template,
        )
        sess.save()
        if activate:
            self.activate(slug)
        return sess

    def activate(self, name: str) -> Session:
        sess = self.get(name)
        self.root.mkdir(parents=True, exist_ok=True)
        self._pointer.write_text(sess.slug, encoding="utf-8")
        sess.touch()
        return sess

    def active(self) -> Session | None:
        """Resolve the current session: $MANHWA_SESSION, then the pointer file."""
        env = os.environ.get("MANHWA_SESSION")
        if env:
            try:
                return self.get(env)
            except (KeyError, ValueError):
                return None
        if not self._pointer.exists():
            return None
        slug = self._pointer.read_text(encoding="utf-8").strip()
        if not slug:
            return None
        path = self.root / slug
        return Session.load(path) if path.is_dir() else None

    def rename(self, old: str, new: str) -> Session:
        sess = self.get(old)
        new_slug = slugify(new)
        if new_slug == sess.slug:
            sess.name = new
            sess.save()
            return sess
        target = self.root / new_slug
        if target.exists():
            raise FileExistsError(f"Session {new_slug!r} already exists")
        current = self.active()
        was_active = current is not None and current.slug == sess.slug
        sess.path.rename(target)
        moved = Session.load(target)
        moved.name = new
        moved.save()
        if was_active:
            self.activate(new_slug)
        return moved

    def archive(self, name: str) -> Path:
        """Move a session aside rather than deleting it.

        Transcripts cost real API calls to produce, so removal is never
        destructive -- the directory is moved under _archived/ and the path is
        returned so the user can restore or remove it deliberately.
        """
        sess = self.get(name)
        archive_root = self.root / ARCHIVE_DIRNAME
        archive_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = archive_root / f"{sess.slug}-{stamp}"
        shutil.move(str(sess.path), str(dest))
        active = self.active()
        if active is None and self._pointer.exists():
            self._pointer.unlink()
        return dest
