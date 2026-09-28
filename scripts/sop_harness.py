"""Reusable harness for capturing SOP screenshots from the real application.

One flow script per SOP imports this, walks the workflow, and calls shoot() at
each step. Rerunning the flow regenerates every screenshot — which is the whole
point: a screenshot library nobody can regenerate is a library that rots.

WHAT THIS GUARANTEES

  * No production data. The harness refuses to start unless Django is pointed at
    the disposable capture database (business.settings_sop).
  * No credentials. The dispatcher session is minted directly in the session
    store from their auth hash; no password exists and none is ever typed.
  * Annotations anchored to the DOM, not to pixels. A highlight is a CSS
    selector; the harness asks the live page where that element is and draws
    there. A layout change moves the box instead of silently mislabelling it —
    the failure mode that makes recorded-click tools go quietly wrong.
  * A manifest per SOP recording the git blob hash of every template and view
    the flow depends on, so drift can be detected later without re-running.

USAGE

    from scripts.sop_harness import SopCapture

    with SopCapture(slug="reservation-basics", depends_on=[...]) as cap:
        cap.goto("/dispatching/")
        cap.shoot("01-dashboard", "The dispatch board",
                  highlight=[("#new-reservation-btn", "1")])
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Brand (content/static/css): gold on navy.
GOLD = (201, 162, 39)          # #C9A227
GOLD_BRIGHT = (212, 175, 55)   # #D4AF37
NAVY = (10, 20, 40)            # #0A1428
WHITE = (255, 255, 255)

VIEWPORT = {"width": 1440, "height": 900}   # matches the existing SOP-004 images
PORT = 8111
BASE_URL = f"http://127.0.0.1:{PORT}"


@dataclass
class Shot:
    """One captured step, as it will appear in the generated SOP."""
    name: str
    caption: str
    url: str
    path: str
    highlights: list = field(default_factory=list)


class SopCapture:
    def __init__(self, slug: str, depends_on: list[str], out_root: Path | None = None,
                 as_user: str = "jdoe"):
        self.slug = slug
        self.depends_on = depends_on
        # Which fixture account the capture is taken as. Defaults to the
        # dispatcher, because most SOPs are dispatcher SOPs — but the fleet
        # screens branch on the role, and capturing those as a dispatcher would
        # photograph a top bar the fleet manager never sees.
        self.as_user = as_user
        self.out_dir = (out_root or REPO_ROOT / "SOPS" / "images") / slug
        self.shots: list[Shot] = []
        self.failures: list[tuple[str, str]] = []
        self._server: subprocess.Popen | None = None
        self._pw = None
        self._browser = None
        self.page = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    def __enter__(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._start_server()
        session_key = self._mint_session(self.as_user)
        self._start_browser(session_key)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._write_manifest()
        finally:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
            self._stop_server()
        return False

    # ── server ──────────────────────────────────────────────────────────────
    def _start_server(self):
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "business.settings_sop"
        env.pop("DATABASE_URL", None)
        env.pop("RAILWAY_ENVIRONMENT", None)
        env.pop("PGHOST", None)

        self._server = subprocess.Popen(
            [sys.executable, "manage.py", "runserver", str(PORT),
             "--noreload", "--settings=business.settings_sop"],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

        deadline = time.time() + 60
        while time.time() < deadline:
            if self._server.poll() is not None:
                out = self._server.stdout.read().decode("utf-8", "replace")
                raise RuntimeError(f"Capture server died on startup:\n{out[-3000:]}")
            try:
                urllib.request.urlopen(BASE_URL, timeout=2)
                return
            except urllib.error.HTTPError:
                return          # any HTTP response means it is serving
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"Capture server did not come up on {BASE_URL} within 60s")

    def _stop_server(self):
        if self._server and self._server.poll() is None:
            self._server.terminate()
            try:
                self._server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._server.kill()

    # ── auth, without a password ────────────────────────────────────────────
    def _mint_session(self, username: str) -> str:
        """Create a logged-in session directly in the session store.

        This is force_login() by hand. It writes the same three keys Django's
        own login() writes, so the app cannot tell the difference — but no
        password is involved at any point, and none of these fixture users has
        a usable one.
        """
        code = (
            "import django, os;"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE','business.settings_sop');"
            "django.setup();"
            "from django.conf import settings;"
            "from django.contrib.auth import SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY;"
            "from django.contrib.auth.models import User;"
            "from django.contrib.sessions.backends.db import SessionStore;"
            "assert 'db_sop_capture' in str(settings.DATABASES['default']['NAME']), 'wrong database';"
            f"u=User.objects.get(username={username!r});"
            "assert u.is_staff and not u.is_superuser, 'capture user must not be a superuser';"
            "s=SessionStore();"
            "s[SESSION_KEY]=str(u.pk);"
            "s[BACKEND_SESSION_KEY]='django.contrib.auth.backends.ModelBackend';"
            "s[HASH_SESSION_KEY]=u.get_session_auth_hash();"
            "s.create();"
            "print(s.session_key)"
        )
        env = dict(os.environ)
        env["DJANGO_SETTINGS_MODULE"] = "business.settings_sop"
        res = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Could not mint a dispatcher session:\n{res.stderr[-2000:]}")
        return res.stdout.strip().splitlines()[-1]

    # ── browser ─────────────────────────────────────────────────────────────
    def _start_browser(self, session_key: str):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        context = self._browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        context.add_cookies([{
            "name": "sessionid", "value": session_key,
            "domain": "127.0.0.1", "path": "/",
        }])
        self.page = context.new_page()

    # ── the API flows use ───────────────────────────────────────────────────
    def goto(self, path: str, wait: str = "networkidle"):
        self.page.goto(f"{BASE_URL}{path}", wait_until=wait)
        return self

    def shoot(self, name: str, caption: str, highlight: list | None = None,
              full_page: bool = False):
        """Capture one step. `highlight` is [(css_selector, label), ...]."""
        path = self.out_dir / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=full_page)

        resolved = []
        if highlight:
            resolved = self._resolve_highlights(highlight)
            if resolved:
                self._annotate(path, resolved)

        self.shots.append(Shot(
            name=name, caption=caption, url=self.page.url,
            path=str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
            highlights=[h["label"] for h in resolved],
        ))
        print(f"  shot {name}: {caption}")
        return self

    def _resolve_highlights(self, highlight):
        """Ask the live page where each element is, in screenshot pixels."""
        out = []
        for selector, label in highlight:
            try:
                el = self.page.locator(selector).first
                box = el.bounding_box(timeout=3000)
            except Exception:
                print(f"    ! highlight selector not found, skipping: {selector}")
                continue
            if not box:
                print(f"    ! highlight not visible, skipping: {selector}")
                continue
            out.append({"box": box, "label": label, "selector": selector})
        return out

    def _annotate(self, path: Path, resolved):
        from PIL import Image, ImageDraw, ImageFont

        scale = 2  # device_scale_factor
        img = Image.open(path).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = self._font(34)

        for item in resolved:
            b = item["box"]
            pad = 6
            x0 = (b["x"] - pad) * scale
            y0 = (b["y"] - pad) * scale
            x1 = (b["x"] + b["width"] + pad) * scale
            y1 = (b["y"] + b["height"] + pad) * scale

            draw.rounded_rectangle([x0, y0, x1, y1], radius=10 * scale,
                                   outline=GOLD, width=5)

            # Badge sits OUTSIDE the box, on its left edge and vertically
            # centred. On the corner it lands on whatever label sits above the
            # field and hides the very word the step is telling you to find.
            label = str(item["label"])
            r = 22 * scale
            cx = x0 - r - 6 * scale
            cy = (y0 + y1) / 2
            if cx - r < 0:                      # field is hard against the left margin
                cx = x1 + r + 6 * scale         # put it on the right instead
            cy = max(r + 2, min(cy, img.height - r - 2))

            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD, outline=WHITE, width=3)
            tb = draw.textbbox((0, 0), label, font=font)
            draw.text((cx - (tb[2] - tb[0]) / 2, cy - (tb[3] - tb[1]) / 2 - 4),
                      label, fill=NAVY, font=font)

        img.save(path)

    @staticmethod
    def _font(size: int):
        from PIL import ImageFont
        for candidate in ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf"):
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def part(self, name: str):
        """Group steps so one broken selector doesn't lose the whole run.

        A failure inside a part is recorded and the run continues, so a single
        pass surfaces every problem instead of one per attempt — but the
        manifest is withheld, because a manifest is a claim that the capture is
        complete and it must never be written for a partial one.
        """
        return _Part(self, name)

    # ── manifest, for drift detection ───────────────────────────────────────
    def _write_manifest(self):
        if self.failures:
            print("\n  MANIFEST WITHHELD — capture incomplete:")
            for step, err in self.failures:
                print(f"    {step}: {err}")
            return
        manifest = {
            "slug": self.slug,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "commit": self._git("rev-parse", "HEAD"),
            "viewport": VIEWPORT,
            "depends_on": {p: self._blob_hash(p) for p in self.depends_on},
            "shots": [
                {"name": s.name, "caption": s.caption, "url": s.url,
                 "path": s.path, "highlights": s.highlights}
                for s in self.shots
            ],
        }
        out = self.out_dir / "manifest.json"
        out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\n  manifest: {out.relative_to(REPO_ROOT)}")

    @staticmethod
    def _git(*args) -> str:
        try:
            return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                                  text=True, check=True).stdout.strip()
        except Exception:
            return "unknown"

    def _blob_hash(self, rel_path: str) -> str:
        """Git's content hash for a file — changes iff the file's bytes change."""
        return self._git("hash-object", rel_path)


class _Part:
    """Context manager returned by SopCapture.part()."""

    def __init__(self, cap: SopCapture, name: str):
        self.cap = cap
        self.name = name

    def __enter__(self):
        print(f"\n  -- {self.name} --")
        return self.cap

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            return False
        msg = str(exc).splitlines()[0][:160]
        print(f"  !! {self.name} FAILED: {msg}")
        self.cap.failures.append((self.name, msg))
        return True          # swallow, so later parts still run
