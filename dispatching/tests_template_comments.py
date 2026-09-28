"""Every template comment must actually comment.

Run with:  ./manage.py test dispatching.tests_template_comments

Django's ``{# ... #}`` is SINGLE-LINE ONLY. Spread it over two or more lines and
it silently stops being a comment: the text renders into the page. It has escaped
twice — once onto the trip card, once onto the booking pricing screen — because
nothing fails, the words just appear in front of a dispatcher.

Multi-line explanation belongs in ``{% comment %}`` / ``{% endcomment %}``.
"""
import pathlib

from django.conf import settings
from django.test import SimpleTestCase

SKIP = ("node_modules", "staticfiles", ".venv", "site-packages")


class HashCommentsStayOnOneLine(SimpleTestCase):
    def test_no_template_opens_a_hash_comment_it_does_not_close(self):
        offenders = []
        for template_dir in (pathlib.Path(settings.BASE_DIR),):
            for path in template_dir.rglob("*.html"):
                if any(part in str(path) for part in SKIP):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    # A CSS block such as `@media print{#id{...}}` also contains
                    # "{#", so only flag lines where it reads as a template tag:
                    # whitespace or start-of-line immediately before it.
                    stripped = line.lstrip()
                    if not stripped.startswith("{#"):
                        continue
                    if "#}" in stripped:
                        continue
                    rel = path.relative_to(settings.BASE_DIR)
                    offenders.append(f"{rel}:{lineno}: {stripped[:80]}")

        self.assertEqual(
            offenders,
            [],
            "These open a {# comment #} without closing it on the same line, so "
            "the text renders onto the page instead of being a comment. Use "
            "{% comment %} ... {% endcomment %} for anything multi-line:\n  "
            + "\n  ".join(offenders),
        )
