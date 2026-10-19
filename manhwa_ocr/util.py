"""Small shared helpers. No imports from the rest of the package, so anything
can use these without creating import cycles."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, List

# Chapter labels end up in filenames (chapter_<label>.md, .work/ch<label>/), so
# they are restricted to characters that are safe on every filesystem.
CHAPTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
MAX_RANGE = 5000


class UserError(Exception):
    """A problem the user (or driving agent) can fix. Printed without a traceback."""


# ---------------------------------------------------------------- chapters


def valid_chapter(label: str) -> bool:
    return bool(CHAPTER_RE.match(label or "")) and ".." not in label


def normalize_label(label: str) -> str:
    """'01' -> '1', '007.5' -> '7.5'. Non-numeric labels pass through unchanged.

    Without this, chapter_01.md and a link numbered 1 would be two different
    chapters and the same chapter would get transcribed twice.
    """
    label = (label or "").strip()
    m = re.fullmatch(r"0*(\d+)(\.\d+)?", label)
    if m:
        return m.group(1) + (m.group(2) or "")
    return label


def require_chapter(label: str) -> str:
    label = normalize_label(label)
    if not valid_chapter(label):
        raise UserError(
            f"Invalid chapter '{label}'. Use numbers like 7 or 12.5, "
            "or short names of letters, digits, '.', '_' and '-'."
        )
    return label


def chapter_key(label: str):
    """Sort 1, 2, 10, 10.5, 11, then non-numeric labels alphabetically."""
    try:
        return (0, float(label), label)
    except (TypeError, ValueError):
        return (1, 0.0, str(label))


def sort_chapters(labels: Iterable[str]) -> List[str]:
    return sorted(dict.fromkeys(labels), key=chapter_key)


def expand_chapters(spec: str) -> List[str]:
    """'1-7' / '1,3,5' / '1-4,11' / '2' -> chapter labels, validated."""
    out: List[str] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise UserError(f"Range '{part}' runs backwards. Did you mean {hi}-{lo}?")
            if hi - lo + 1 > MAX_RANGE:
                raise UserError(f"Range '{part}' is too large (max {MAX_RANGE} chapters).")
            out.extend(str(n) for n in range(lo, hi + 1))
        else:
            out.append(require_chapter(part))
    if not out:
        raise UserError(f"No chapters in '{spec}'. Examples: 3   1-7   1-4,11")
    return list(dict.fromkeys(out))


def chapter_spec(labels: Iterable[str]) -> str:
    """The reverse of expand_chapters: ['1','2','3','5'] -> '1-3,5'."""
    parts: List[str] = []
    run: List[int] = []

    def flush() -> None:
        if run:
            parts.append(str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}")
            run.clear()

    for label in sort_chapters(labels):
        if label.isdigit():
            n = int(label)
            if run and n != run[-1] + 1:
                flush()
            run.append(n)
        else:
            flush()
            parts.append(label)
    flush()
    return ",".join(parts)


# ---------------------------------------------------------------- atomic writes
#
# Every file the dashboard or a restarted agent might read is written to a temp
# file in the same directory and then swapped in with os.replace, which is
# atomic on Windows and POSIX. A crash or Ctrl+C mid-write can no longer leave a
# half-written file that the next run trusts.


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def atomic_save_image(img, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp.png", dir=path.parent)
    os.close(fd)
    try:
        img.save(tmp, "PNG")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- JSON from models


def extract_json(raw: str) -> Any:
    """Pull JSON out of text that may be wrapped in prose or code fences.

    Models (and agents writing files) routinely add ```json fences or a
    sentence of commentary around the payload, so this is deliberately
    forgiving.
    """
    if not raw or not raw.strip():
        raise ValueError("empty response")

    text = raw.strip().lstrip("﻿")

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the first balanced object or array, whichever starts first.
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    for start in sorted(starts):
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        depth, in_str, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise ValueError(f"Could not parse JSON from: {raw[:200]!r}")


def read_text_any(path: Path) -> str:
    """Read text whatever encoding a Windows tool saved it in.

    PowerShell 5.1's `>` and Out-File write UTF-16LE with a BOM by default, so
    an agent shelling out there produces files plain utf-8 decoding rejects.
    """
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # BOM-less UTF-16 shows up as alternating NUL bytes.
        if data.count(b"\x00") > len(data) // 4:
            for enc in ("utf-16-le", "utf-16-be"):
                try:
                    return data.decode(enc)
                except UnicodeDecodeError:
                    continue
        return data.decode("cp1252", errors="replace")


def read_agent_json(path: Path, what: str) -> Any:
    """Load a JSON file an agent wrote, with an error message it can act on."""
    path = Path(path)
    if not path.exists():
        raise UserError(f"{path} not found. {what}")
    raw = read_text_any(path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        try:
            return extract_json(raw)
        except ValueError:
            raise UserError(
                f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}: "
                f"{exc.msg}). Fix the file and re-run the same command."
            ) from None


def as_list(value: Any) -> list:
    """Coerce sloppy model output: None -> [], "x" -> ["x"], dict -> its values."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (str, int, float)):
        return [value]
    if isinstance(value, dict):
        return list(value.values())
    return []
