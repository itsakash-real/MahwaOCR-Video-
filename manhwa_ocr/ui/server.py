"""Dashboard HTTP server. Standard library only.

Security posture, since this serves files off your disk:
  * Binds to 127.0.0.1 only. Never exposed to your network.
  * Rejects requests whose Host header is not localhost, which blocks DNS
    rebinding (a remote page pointing a hostname at 127.0.0.1).
  * Writes require a custom header. Browsers will not send one cross-origin
    without a CORS preflight, which this server never approves, so other
    websites cannot drive it.
  * Documents are looked up by validated id, never by client-supplied path, and
    every resolved path is checked to sit inside the session directory.

The UI only reads results and adds links. The pipeline itself still runs in your
terminal / agent; the dashboard is where you look at what it produced.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict
from urllib.parse import parse_qs, unquote, urlparse

from ..sessions import Session, SessionStore
from ..status import session_detail, session_summary
from ..util import valid_chapter

INDEX_HTML = Path(__file__).with_name("index.html")


def index_bytes() -> bytes:
    """The dashboard page, with the canonical agent prompts (agent_prompts.py) filled in."""
    from ..agent_prompts import all_prompts_json

    blob = json.dumps(all_prompts_json()).replace("</", "<\\/")
    return INDEX_HTML.read_bytes().replace(b"__AGENT_PROMPTS_JSON__", blob.encode("utf-8"))


class LocalServer(ThreadingHTTPServer):
    """Refuses to share its port.

    The stdlib default sets SO_REUSEADDR, which on Windows lets a second
    process bind the same port: a second `ui` would start "successfully" next
    to a stale one, and so could any other local program.
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
WRITE_HEADER = "X-Manhwa-OCR"
MAX_BODY = 256 * 1024
MAX_AUDIO = 300 * 1024 * 1024
AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
               ".flac": "audio/flac", ".aac": "audio/aac", ".opus": "audio/ogg"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def make_handler(store: SessionStore, log: Callable[[str], None]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "manhwa-ocr"

        # ---------------- plumbing ----------------

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            return host in ALLOWED_HOSTS

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _session(self, slug: str) -> Session:
            try:
                return store.get(slug)
            except (KeyError, ValueError):
                raise ApiError(HTTPStatus.NOT_FOUND, f"No session '{slug}'")

        def _active_slug(self) -> str | None:
            act = store.active()
            return act.slug if act else None

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
            self._body_consumed = True
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be JSON")
            if not isinstance(data, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be a JSON object")
            return data

        # ---------------- routing ----------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"Forbidden host", "text/plain")
            url = urlparse(self.path)
            # Session slugs can be non-Latin ("용사는-쉴-수-없다"); the browser
            # percent-encodes them in the path.
            parts = [unquote(p) for p in url.path.split("/") if p]
            try:
                if not parts:
                    return self._send(
                        HTTPStatus.OK, index_bytes(), "text/html; charset=utf-8"
                    )
                if parts == ["api", "sessions"]:
                    active = self._active_slug()
                    return self._json(
                        HTTPStatus.OK,
                        {
                            "root": str(store.root),
                            "active": active,
                            "sessions": [session_summary(s, active) for s in store.list()],
                        },
                    )
                if len(parts) == 3 and parts[:2] == ["api", "session"]:
                    sess = self._session(parts[2])
                    return self._json(HTTPStatus.OK, session_detail(sess, self._active_slug()))
                if len(parts) == 4 and parts[:2] == ["api", "session"] and parts[3] == "doc":
                    sess = self._session(parts[2])
                    q = parse_qs(url.query)
                    return self._json(
                        HTTPStatus.OK,
                        self._doc(sess, (q.get("type") or [""])[0], (q.get("id") or [""])[0]),
                    )
                if len(parts) == 4 and parts[:2] == ["api", "session"] and parts[3] == "media":
                    sess = self._session(parts[2])
                    q = parse_qs(url.query)
                    path, ctype = self._media(sess, (q.get("kind") or [""])[0],
                                              (q.get("chapter") or [""])[0], (q.get("n") or ["1"])[0])
                    return self._send_file(path, ctype)
                raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                pass  # browsers cancel media range requests all the time
            except Exception as exc:  # noqa: BLE001 - never crash the server
                log(f"  ! {self.path}: {exc}")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Server error"})

        # ---------------- media ----------------

        def _media(self, sess: Session, kind: str, chapter: str, n: str):
            """Resolve media by (kind, chapter), never by a client-supplied path."""
            from ..video.media import audio_path, media_dir
            from ..video.render import output_path
            from ..video.storyboard import storyboard_paths

            if not valid_chapter(chapter):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Bad chapter id")
            try:
                idx = max(1, int(n))
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Bad index")
            if kind == "video":
                path, ctype = output_path(sess.path, chapter), "video/mp4"
            elif kind == "preview":
                path, ctype = output_path(sess.path, chapter, preview=True), "video/mp4"
            elif kind == "storyboard":
                boards = storyboard_paths(sess.path, chapter)
                path, ctype = (boards[idx - 1] if idx <= len(boards) else None), "image/png"
            elif kind == "sheet":
                sheets = sorted(media_dir(sess.path, chapter).glob("panels_sheet_*.png"))
                path, ctype = (sheets[idx - 1] if idx <= len(sheets) else None), "image/png"
            elif kind == "audio":
                path = audio_path(sess.path, chapter)
                ctype = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream") if path else ""
            else:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Unknown media kind")
            if path is None or not path.is_file() or not _inside(path, sess.path):
                raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
            return path, ctype

        def _send_file(self, path: Path, ctype: str) -> None:
            """Serve a file with single-range support, which <video> needs to seek."""
            size = path.stat().st_size
            start, end, status = 0, size - 1, HTTPStatus.OK
            rng = self.headers.get("Range")
            if rng:
                m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
                if not m or not (m.group(1) or m.group(2)):
                    return self._range_error(size)
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:  # "bytes=-500": the last 500 bytes
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    return self._range_error(size)
                end = min(end, size - 1)
                status = HTTPStatus.PARTIAL_CONTENT
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _range_error(self, size: int) -> None:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _upload_audio(self, sess: Session, query: str) -> None:
            """Stream an uploaded narration file to audio/chapter_N.<ext>."""
            import os
            import tempfile

            from ..video.media import AUDIO_EXTS, audio_dir, audio_path

            q = parse_qs(query)
            chapter = (q.get("chapter") or [""])[0]
            ext = "." + (q.get("ext") or [""])[0].lower().lstrip(".")
            if not valid_chapter(chapter):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Bad chapter id")
            if ext not in AUDIO_EXTS:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"Audio must be one of: {', '.join(AUDIO_EXTS)}")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not (ctype.startswith("audio/") or ctype in {"application/octet-stream", "video/mp4"}):
                raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "That doesn't look like an audio file")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Empty upload")
            if length > MAX_AUDIO:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Audio files are limited to 300 MB")

            folder = audio_dir(sess.path)
            folder.mkdir(parents=True, exist_ok=True)
            self._body_consumed = True  # from here on we read it ourselves
            fd, tmp = tempfile.mkstemp(prefix=".upload.", suffix=ext, dir=folder)
            try:
                with os.fdopen(fd, "wb") as out:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            raise ApiError(HTTPStatus.BAD_REQUEST, "Upload was cut off")
                        out.write(chunk)
                        remaining -= len(chunk)
                # One audio file per chapter: replace any earlier one, whatever its type.
                old = audio_path(sess.path, chapter)
                dest = folder / f"chapter_{chapter}{ext}"
                os.replace(tmp, dest)
                if old is not None and old.resolve() != dest.resolve():
                    old.unlink(missing_ok=True)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._json(HTTPStatus.OK, {"saved": dest.name, "bytes": length})

        def _drain(self, limit: int = 1 << 20) -> None:
            """Read and discard an unread request body before replying.

            On Windows, closing a socket that still has unread request data
            sends a reset, so the client sees "connection aborted" instead of
            our error message. Bodies over the limit aren't worth reading;
            the connection is closed instead.
            """
            if getattr(self, "_body_consumed", False):
                return
            self._body_consumed = True
            try:
                remaining = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if remaining > limit:
                self.close_connection = True
                return
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self._body_consumed = False
            if not self._host_ok():
                self._drain()
                return self._send(HTTPStatus.FORBIDDEN, b"Forbidden host", "text/plain")
            if self.headers.get(WRITE_HEADER) != "1":
                self._drain()
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Missing write header"})
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.split("/") if p]
            try:
                # Binary upload: must be handled before the body is parsed as JSON.
                if len(parts) == 4 and parts[:2] == ["api", "session"] and parts[3] == "audio":
                    return self._upload_audio(self._session(parts[2]), url.query)
                body = self._body()
                if parts == ["api", "sessions"]:
                    return self._create(body)
                if len(parts) == 4 and parts[:2] == ["api", "session"]:
                    sess = self._session(parts[2])
                    if parts[3] == "links":
                        return self._add_links(sess, body)
                    if parts[3] == "activate":
                        store.activate(sess.slug)
                        return self._json(HTTPStatus.OK, {"active": sess.slug})
                raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
            except ApiError as exc:
                self._drain()
                self._json(exc.status, {"error": exc.message})
            except Exception as exc:  # noqa: BLE001
                log(f"  ! POST {self.path}: {exc}")
                self._drain()
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Server error"})

        # ---------------- handlers ----------------

        def _doc(self, sess: Session, kind: str, ident: str) -> Dict[str, Any]:
            if kind == "transcript":
                if not valid_chapter(ident):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Bad chapter id")
                path = sess.path / f"chapter_{ident}.md"
                title = f"Chapter {ident} — transcript"
            elif kind == "recap":
                if not valid_chapter(ident):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Bad chapter id")
                path = sess.recap_path(ident)
                title = f"Chapter {ident} — recap"
            elif kind == "tts":
                if not valid_chapter(ident):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Bad chapter id")
                from ..video.cues import tts_path

                path = tts_path(sess.path, ident)
                title = f"Chapter {ident} — narration to record"
            elif kind == "compiled":
                # Only names that actually exist are servable.
                by_name = {p.stem: p for p in sess.compilations()}
                if ident not in by_name:
                    raise ApiError(HTTPStatus.NOT_FOUND, "No such script")
                path = by_name[ident]
                title = ident.replace("compiled_", "Script: chapters ").replace("_", ", ")
            elif kind == "links":
                path = sess.links_path
                title = "chapters.txt"
            else:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Unknown document type")

            if not _inside(path, sess.path) or not path.is_file():
                raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
            return {
                "title": title,
                "filename": path.name,
                "path": str(path),
                "text": path.read_text(encoding="utf-8"),
            }

        def _create(self, body: Dict[str, Any]) -> None:
            name = str(body.get("name") or "").strip()
            if not name or len(name) > 120:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Give the series a name")
            try:
                sess = store.create(name)
            except FileExistsError:
                raise ApiError(HTTPStatus.CONFLICT, f"'{name}' already exists")
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, str(exc))
            sess.ensure_links_file()
            added = sess.add_links(_clean_urls(body.get("urls")))
            self._json(HTTPStatus.CREATED, {"slug": sess.slug, "added": added})

        def _add_links(self, sess: Session, body: Dict[str, Any]) -> None:
            urls = _clean_urls(body.get("urls"))
            if not urls:
                raise ApiError(HTTPStatus.BAD_REQUEST, "No http(s) links found")
            added = sess.add_links(urls)
            self._json(
                HTTPStatus.OK,
                {"added": added, "duplicates": len(urls) - added, "total": len(sess.read_links())},
            )

    return Handler


def _clean_urls(raw: Any) -> list[str]:
    """Accept a list or a pasted blob; keep only plausible http(s) URLs."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:2000]:
        u = str(item).strip()
        if re.fullmatch(r"https?://[^\s<>\"']{3,2000}", u):
            out.append(u)
    return out


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    sessions_root: str | None = None,
    open_browser: bool = True,
    log: Callable[[str], None] = print,
) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        log("The dashboard only binds to localhost.")
        return 1

    store = SessionStore(sessions_root)
    try:
        httpd = LocalServer((host, port), make_handler(store, log))
    except OSError as exc:
        log(f"Could not start on port {port}: {exc}")
        log(f"Try another:  python -m manhwa_ocr ui --port {port + 1}")
        return 1

    url = f"http://127.0.0.1:{port}/"
    log(f"Dashboard: {url}")
    log(f"Sessions:  {store.root}")
    log("Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\nStopped.")
    finally:
        httpd.server_close()
    return 0
