#!/usr/bin/env python
"""Render Django pages to standalone HTML, for design review outside the app.

Claude Design, an Artifact, or a plain browser tab can all render real markup —
none of them can run Django. This renders a page through the test client and
then inlines everything it needs (local CSS and JS, CDN stylesheets, fonts,
images) so the result opens correctly with no server and no static files.

Tracking is stripped on the way out: Google Tag Manager, gtag, the Bing UET
tag, and the Turnstile widget. A design copy should not fire analytics when
someone opens it, and a captcha cannot work off-domain anyway.

    python scripts/flatten_page.py partner --out design-export
    python scripts/flatten_page.py /users/ /rates-booking/ --out design-export

Each argument is either a URL name (reversed) or a literal path.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import posixpath
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "business.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.staticfiles import finders  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import NoReverseMatch, reverse  # noqa: E402

# The test client speaks as "testserver"; the real ALLOWED_HOSTS list has no
# reason to know about it.
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]

PROD = "https://www.graysontowncar.com"

# Hosts whose scripts exist only to watch people. None of them belong in a
# design copy that gets opened by a handful of reviewers.
TRACKER_HOSTS = (
    "googletagmanager.com",
    "google-analytics.com",
    "bat.bing.net",
    "clarity.ms",
    "connect.facebook.net",
    "snap.licdn.com",
    "challenges.cloudflare.com",
)
TRACKER_MARKERS = ("gtm.start", "dataLayer", "gtag(", "uetq", "fbq(", "turnstile")

# Google Fonts is the one remote stylesheet worth leaving as a link: it is on
# every renderer's allowlist, including the Artifact CSP.
KEEP_REMOTE = ("fonts.googleapis.com", "fonts.gstatic.com")


class Budget:
    """Tracks how many bytes of inlined assets the page has spent so far."""

    def __init__(self, per_asset: int, total: int) -> None:
        self.per_asset = per_asset
        self.total = total
        self.spent = 0
        self.inlined: list[tuple[str, int]] = []
        self.skipped: list[tuple[str, str]] = []

    def allow(self, name: str, size: int) -> bool:
        if size > self.per_asset:
            self.skipped.append((name, f"{size // 1024} KB over per-asset cap"))
            return False
        if self.spent + size > self.total:
            self.skipped.append((name, "page budget exhausted"))
            return False
        self.spent += size
        self.inlined.append((name, size))
        return True


def static_path(url: str) -> str | None:
    """Static-relative path for a URL under STATIC_URL, else None."""
    path = urlparse(url).path
    if not path.startswith(settings.STATIC_URL):
        return None
    return path[len(settings.STATIC_URL):]


def read_static(rel: str) -> bytes | None:
    found = finders.find(rel)
    if found:
        return Path(found).read_bytes()
    collected = Path(settings.STATIC_ROOT) / rel
    if collected.exists():
        return collected.read_bytes()
    return None


def fetch(url: str) -> bytes | None:
    try:
        req = Request(url, headers={"User-Agent": "grayson-flatten/1.0"})
        with urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception:
        return None


def resolve(ref: str, base: tuple[str, str]) -> tuple[str, str] | None:
    """Resolve a reference found inside a document against its base.

    Returns ("static", rel) or ("remote", url), or None for things that should
    be left exactly as they are (data URIs, anchors, mailto, tel).
    """
    ref = ref.strip().strip("'\"")
    if not ref or ref.startswith(("data:", "#", "mailto:", "tel:", "javascript:")):
        return None
    if ref.startswith("//"):
        return ("remote", "https:" + ref)
    if ref.startswith(("http://", "https://")):
        rel = static_path(ref)
        return ("static", rel) if rel else ("remote", ref)

    kind, origin = base
    if kind == "static":
        if ref.startswith("/"):
            rel = static_path(ref)
            return ("static", rel) if rel else ("remote", urljoin(PROD, ref))
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(origin), ref))
        return ("static", joined)

    return ("remote", urljoin(origin, ref))


def load(target: tuple[str, str]) -> bytes | None:
    kind, ref = target
    return read_static(ref) if kind == "static" else fetch(ref)


def absolute(target: tuple[str, str]) -> str:
    kind, ref = target
    return f"{PROD}{settings.STATIC_URL}{ref}" if kind == "static" else ref


# Data URIs are decoded by their declared type, not sniffed the way an HTTP
# response is, so a file whose extension disagrees with its bytes has to be
# labelled by what it actually is or the browser refuses to draw it.
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"wOF2", "font/woff2"),
    (b"wOFF", "font/woff"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)


def sniff(payload: bytes, ref: str) -> str:
    for signature, mime in MAGIC:
        if payload.startswith(signature):
            return mime
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    head = payload[:200].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in payload[:400].lower()):
        return "image/svg+xml"
    return mimetypes.guess_type(urlparse(ref).path)[0] or "application/octet-stream"


def data_uri(target: tuple[str, str], payload: bytes) -> str:
    _, ref = target
    mime = sniff(payload, ref)
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def embed(ref: str, base: tuple[str, str], budget: Budget) -> str:
    """An href/src value the flattened page can use: data URI, or absolute URL."""
    target = resolve(ref, base)
    if target is None:
        return ref
    if any(host in target[1] for host in KEEP_REMOTE):
        return target[1]
    payload = load(target)
    if payload is None:
        budget.skipped.append((target[1], "could not be read"))
        return absolute(target)
    if not budget.allow(target[1], len(payload)):
        return absolute(target)
    return data_uri(target, payload)


CSS_URL = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.I)
CSS_IMPORT = re.compile(r"@import\s+(?:url\()?\s*['\"]([^'\"]+)['\"]\s*\)?\s*;", re.I)


# Every browser that will ever open a design copy supports woff2. Inlining the
# .ttf/.eot fallbacks alongside it doubles or triples the page weight for
# nothing — Font Awesome alone came to 3.7 MB that way.
LEGACY_FONT = (".ttf", ".eot", ".otf", ".woff")


def inline_css(text: str, base: tuple[str, str], budget: Budget, depth: int = 0) -> str:
    """Rewrite url() references and pull in @import-ed sheets."""

    def swap(match: re.Match) -> str:
        ref = match.group(2)
        path = urlparse(ref.split("#")[0].split("?")[0]).path.lower()
        if path.endswith(LEGACY_FONT):
            target = resolve(ref, base)
            return f"url('{absolute(target)}')" if target else match.group(0)
        return f"url('{embed(ref, base, budget)}')"

    if depth < 3:
        def pull(match: re.Match) -> str:
            target = resolve(match.group(1), base)
            if target is None:
                return match.group(0)
            payload = load(target)
            if payload is None:
                return match.group(0)
            return inline_css(payload.decode("utf-8", "replace"), target, budget, depth + 1)

        text = CSS_IMPORT.sub(pull, text)

    return prune_legacy(CSS_URL.sub(swap, text))


# A src list that still names the .ttf fallback trips renderers that audit URLs
# rather than requests — the artifact viewer reports it as a blocked font even
# though no browser would ever ask for it once the woff2 above it loaded.
LEGACY_ENTRY = re.compile(
    r",?\s*url\(\s*['\"]?https?://[^'\")]+\.(?:ttf|eot|otf|woff)(?:[?#][^'\")]*)?['\"]?\s*\)"
    r"(?:\s*format\([^)]*\))?",
    re.I,
)


def prune_legacy(css: str) -> str:
    # Scoping this to the src: declaration is not an option — an inlined woff2
    # arrives as a data URI, whose own "…woff2;base64," semicolon ends any
    # declaration the regex tries to bound.
    css = LEGACY_ENTRY.sub("", css)
    return re.sub(r"(src\s*:)\s*,\s*", r"\1", css, flags=re.I)


TAG = re.compile(r"<(link|script|img|source)\b([^>]*?)(/?)>", re.I | re.S)
SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
ATTR = re.compile(r"""(\b[\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")


def attrs(raw: str) -> dict[str, str]:
    found = {}
    for match in ATTR.finditer(raw):
        found[match.group(1).lower()] = match.group(2) or match.group(3) or match.group(4) or ""
    return found


def strip_trackers(html: str) -> tuple[str, int]:
    removed = 0

    def drop(match: re.Match) -> str:
        nonlocal removed
        src = attrs(match.group(1)).get("src", "")
        body = match.group(2)
        if any(host in src for host in TRACKER_HOSTS) or any(m in body for m in TRACKER_MARKERS):
            removed += 1
            return ""
        return match.group(0)

    return SCRIPT_BLOCK.sub(drop, html), removed


def swap_srcset(value: str, base: tuple[str, str], budget: Budget) -> str:
    parts = []
    for candidate in value.split(","):
        bits = candidate.strip().split()
        if not bits:
            continue
        bits[0] = embed(bits[0], base, budget)
        parts.append(" ".join(bits))
    return ", ".join(parts)


def flatten(html: str, budget: Budget) -> str:
    html, dropped = strip_trackers(html)
    base: tuple[str, str] = ("static", "")

    def handle(match: re.Match) -> str:
        tag = match.group(1).lower()
        raw = match.group(2)
        close = match.group(3)
        found = attrs(raw)

        if tag == "link":
            rel = found.get("rel", "").lower()
            href = found.get("href", "")
            if "stylesheet" in rel:
                if any(host in href for host in KEEP_REMOTE):
                    return match.group(0)
                target = resolve(href, base)
                if target is None:
                    return match.group(0)
                payload = load(target)
                if payload is None:
                    budget.skipped.append((href, "stylesheet could not be read"))
                    return match.group(0)
                css = inline_css(payload.decode("utf-8", "replace"), target, budget)
                budget.allow(target[1], len(css.encode()))
                return f"<style>\n/* {target[1]} */\n{css}\n</style>"
            if "preload" in rel:
                # The real element gets inlined; a preload of the same bytes
                # would only double the page weight.
                return ""
            if href and ("icon" in rel or "apple-touch" in rel):
                return f'<link{ATTR_SUB(raw, "href", embed(href, base, budget))}{close}>'
            return match.group(0)

        if tag == "script":
            src = found.get("src", "")
            target = resolve(src, base) if src else None
            if target and target[0] == "static":
                payload = load(target)
                if payload is not None:
                    budget.allow(target[1], len(payload))
                    return f"<script>\n/* {target[1]} */\n{payload.decode('utf-8', 'replace')}\n</script><!--"
            return match.group(0)

        if tag in ("img", "source"):
            out = raw
            if found.get("src"):
                out = ATTR_SUB(out, "src", embed(found["src"], base, budget))
            if found.get("srcset"):
                out = ATTR_SUB(out, "srcset", swap_srcset(found["srcset"], base, budget))
            return f"<{tag}{out}{close}>"

        return match.group(0)

    html = TAG.sub(handle, html)
    # Inlined <script src> tags leave their original </script> behind; the
    # opening "<!--" above swallows it.
    html = html.replace("<!--</script>", "")
    html = re.sub(r"<!--\s*</script\s*>", "", html)

    # Inline style="...url(...)..." attributes.
    def style_attr(match: re.Match) -> str:
        return f'style="{inline_css(match.group(1), base, budget)}"'

    html = re.sub(r'style="([^"]*url\([^"]*)"', style_attr, html, flags=re.I)

    if dropped:
        html = html.replace("</head>", f"<!-- {dropped} tracking scripts removed -->\n</head>", 1)
    return html


def ATTR_SUB(raw: str, name: str, value: str) -> str:
    pattern = re.compile(rf"""(\b{name}\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
    return pattern.sub(lambda m: m.group(1) + '"' + value.replace('"', "&quot;") + '"', raw, count=1)


HEAD = re.compile(r"<head[^>]*>(.*?)</head\s*>", re.I | re.S)
BODY = re.compile(r"<body([^>]*)>(.*?)</body\s*>", re.I | re.S)
TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
STYLE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.I | re.S)
SHEET = re.compile(r"<link\b[^>]*?rel\s*=\s*[\"']?(?:stylesheet|preconnect)[\"']?[^>]*?/?>", re.I | re.S)
CLASS = re.compile(r"""class\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)


def as_artifact(html: str) -> str:
    """Reshape a full document into the fragment an Artifact expects.

    The publisher supplies its own doctype, <head> and <body>, so those tags
    have to go — but the styles inside the head, and the class the layout hangs
    off the body, both have to survive.
    """
    head_match = HEAD.search(html)
    body_match = BODY.search(html)
    if not head_match or not body_match:
        return html

    head = head_match.group(1)
    title = TITLE.search(head)
    pieces = []
    # Title first: the publisher only scans the opening 8 KB for it, and the
    # inlined stylesheets are far bigger than that.
    if title:
        pieces.append(f"<title>{title.group(1).strip()}</title>")
    pieces.extend(m.group(0) for m in SHEET.finditer(head))
    pieces.extend(m.group(0) for m in STYLE.finditer(head))

    body_class = CLASS.search(body_match.group(1))
    if body_class:
        value = body_class.group(1) or body_class.group(2) or body_class.group(3)
        pieces.append(f"<script>document.body.className = {json.dumps(value)};</script>")

    return "\n".join(pieces) + "\n" + body_match.group(2)


def with_card(html: str, group: str, name: str) -> str:
    """Prefix the Design System pane's card marker, which must be line one."""
    return f'<!-- @dsCard group="{group}" name="{name}" -->\n' + html


def page_path(arg: str) -> str:
    if arg.startswith("/"):
        return arg
    try:
        return reverse(arg)
    except NoReverseMatch:
        return "/" + arg.strip("/") + "/"


def slug(path: str) -> str:
    cleaned = path.strip("/").replace("/", "-") or "home"
    return re.sub(r"[^a-z0-9-]+", "-", cleaned.lower())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pages", nargs="+", help="URL names or paths")
    parser.add_argument("--out", default="design-export", help="output directory")
    parser.add_argument("--max-asset-kb", type=int, default=3072)
    parser.add_argument("--budget-mb", type=int, default=12)
    parser.add_argument(
        "--artifact",
        action="store_true",
        help="emit the head-and-body fragment an Artifact publish expects",
    )
    parser.add_argument(
        "--card",
        metavar="GROUP",
        help="prefix the Claude Design card marker, filed under this group",
    )
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    client = Client()

    failures = 0
    for arg in args.pages:
        path = page_path(arg)
        budget = Budget(args.max_asset_kb * 1024, args.budget_mb * 1024 * 1024)
        try:
            response = client.get(path, follow=True)
        except Exception as exc:
            print(f"  {path}  render failed: {exc}")
            failures += 1
            continue
        if response.status_code != 200:
            print(f"  {path}  HTTP {response.status_code}")
            failures += 1
            continue

        html = flatten(response.content.decode("utf-8", "replace"), budget)
        if args.artifact:
            html = as_artifact(html)
        if args.card:
            title = TITLE.search(html)
            label = title.group(1).split("—")[0].strip() if title else slug(path)
            html = with_card(html, args.card, label)
        destination = out / f"{slug(path)}.html"
        destination.write_text(html, encoding="utf-8")

        size = destination.stat().st_size
        print(f"  {path}  ->  {destination}  ({size / 1024:.0f} KB, {len(budget.inlined)} assets inlined)")
        for name, why in budget.skipped:
            print(f"       left as a link: {name}  ({why})")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
