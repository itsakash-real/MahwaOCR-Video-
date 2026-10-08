"""Chapter page-URL discovery.

The generic scraper handles most reader sites: pull every <img> out of the HTML,
keep the ones that look like sequential chapter pages, drop the chrome.

If a site is JavaScript-rendered or actively hostile to scripts, don't fight it.
Save the images with your browser and point the tool at the folder:

    manhwa-ocr prepare --folder ./my-pages --chapter 3

Adding a site-specific scraper: subclass Scraper, implement `matches` and
`page_urls`, and append it to SCRAPERS.
"""

from __future__ import annotations

import html as htmllib
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List
from urllib.parse import unquote, urljoin, urlparse

import requests

from .util import UserError

# Site furniture that shows up in <img> tags but isn't a story page. Matched
# against the *filename only*, as whole words: matching the full URL threw away
# every page on WordPress readers (".../uploads/...") and on any series whose
# slug happened to contain "icon" or "thumb".
_JUNK_WORDS = {
    "logo", "banner", "avatar", "icon", "icons", "discord", "patreon", "kofi",
    "ad", "ads", "advert", "button", "thumb", "thumbnail", "sprite", "emoji",
    "favicon", "loading", "loader", "placeholder", "spinner", "header", "footer",
    "social", "share", "gravatar", "pixel",
}

_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I | re.S)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.S)
# Lazy loaders put the real image in one of these and a placeholder in src.
_PREFERRED_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-url", "data-cfsrc", "src")
_IMAGE_EXT = re.compile(r"\.(webp|jpe?g|png|gif|avif)$", re.I)


def _is_junk(url: str) -> bool:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]).lower()
    stem = name.rsplit(".", 1)[0]
    words = set(re.split(r"[^a-z0-9]+", stem))
    return bool(words & _JUNK_WORDS)


def extract_image_urls(page_html: str, base_url: str) -> List[str]:
    """Every plausible image URL in <img> tags, in document order, de-duplicated."""
    out: List[str] = []
    seen = set()
    for tag in _IMG_TAG.findall(page_html):
        attrs = {}
        for m in _ATTR.finditer(tag):
            attrs[m.group(1).lower()] = m.group(2) if m.group(2) is not None else (
                m.group(3) if m.group(3) is not None else m.group(4)
            )
        src = next(
            (attrs[a].strip() for a in _PREFERRED_ATTRS
             if attrs.get(a) and not attrs[a].strip().startswith("data:")),
            "",
        )
        if not src:
            continue
        absolute = urljoin(base_url, htmllib.unescape(src))
        if absolute in seen or not absolute.startswith(("http://", "https://")):
            continue
        seen.add(absolute)
        if not _IMAGE_EXT.search(urlparse(absolute).path):
            continue
        if _is_junk(absolute):
            continue
        out.append(absolute)
    return out


def pick_chapter_pages(candidates: List[str]) -> List[str]:
    """Keep the images that look like the chapter, in page order.

    Story pages normally share one folder on the image host. The biggest such
    group is the chapter; site chrome lives elsewhere. Some readers spread one
    chapter over two hosts or folders, so any other group of comparable size is
    kept too rather than silently dropping half the chapter.
    """
    if not candidates:
        return []
    groups: Dict[str, List[str]] = {}
    for src in candidates:
        p = urlparse(src)
        groups.setdefault(p.netloc + p.path.rsplit("/", 1)[0], []).append(src)
    biggest = max(len(g) for g in groups.values())
    if biggest < 3:
        return candidates
    keep = {k for k, g in groups.items() if len(g) >= max(3, biggest * 0.3)}
    return [
        src for src in candidates
        if (urlparse(src).netloc + urlparse(src).path.rsplit("/", 1)[0]) in keep
    ]


class Scraper(ABC):
    name = "base"

    @classmethod
    @abstractmethod
    def matches(cls, url: str) -> bool: ...

    @abstractmethod
    def page_urls(self, url: str, cfg: Dict[str, Any]) -> List[str]: ...

    @staticmethod
    def fetch_html(url: str, cfg: Dict[str, Any]) -> str:
        headers = dict(cfg.get("headers") or {})
        headers.setdefault("Accept", "text/html,application/xhtml+xml")
        try:
            resp = requests.get(url, headers=headers, timeout=cfg.get("timeout", 60))
            resp.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            why = {
                403: "the site refused the request (it probably blocks scripts)",
                404: "the page doesn't exist (check the link)",
                429: "the site is rate-limiting; wait a bit and retry",
            }.get(status, str(exc))
            raise UserError(f"Couldn't open the chapter page: {why}.") from None
        return resp.text


class GenericScraper(Scraper):
    """Heuristic <img> extraction. Works on most static reader pages."""

    name = "generic"

    @classmethod
    def matches(cls, url: str) -> bool:
        return True  # last resort

    def page_urls(self, url: str, cfg: Dict[str, Any]) -> List[str]:
        pages = pick_chapter_pages(extract_image_urls(self.fetch_html(url, cfg), url))
        if not pages:
            raise UserError(
                "No chapter images found on that page. It is probably JavaScript-rendered.\n"
                "Save the images with your browser and run:  prepare --chapter N --folder <dir>"
            )
        return pages


class AsuraScraper(GenericScraper):
    """Asura-style CDN layout: /chapters/<series>/<n>/<hash>.webp"""

    name = "asura"

    @classmethod
    def matches(cls, url: str) -> bool:
        return "asura" in urlparse(url).netloc.lower()

    def page_urls(self, url: str, cfg: Dict[str, Any]) -> List[str]:
        urls = super().page_urls(url, cfg)
        return [u for u in urls if "/chapters/" in u] or urls


SCRAPERS: List[type[Scraper]] = [AsuraScraper, GenericScraper]


def get_scraper(url: str) -> Scraper:
    for cls in SCRAPERS:
        if cls.matches(url):
            return cls()
    return GenericScraper()


def discover_pages(url: str, cfg: Dict[str, Any]) -> List[str]:
    return get_scraper(url).page_urls(url, cfg)
