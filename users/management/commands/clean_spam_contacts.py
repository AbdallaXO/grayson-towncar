"""Delete spam contact form submissions (Cyrillic text, URLs in names, known spam patterns)."""

import re
from django.core.management.base import BaseCommand
from users.models import ContactUsForm


CYRILLIC_RE = re.compile(r'[\u0400-\u04FF]')
URL_RE = re.compile(r'https?://', re.IGNORECASE)
SPAM_KEYWORDS = [
    'tinyurl.com', 'bit.ly', 'руб', 'перевод', 'сюрприз',
    'подарок', 'новости', 'ссылк', 'joriuckror',
]


def is_spam(entry):
    combined = f"{entry.first_name} {entry.last_name} {entry.about}".lower()
    if CYRILLIC_RE.search(combined):
        return True
    if URL_RE.search(f"{entry.first_name} {entry.last_name}"):
        return True
    if any(kw in combined for kw in SPAM_KEYWORDS):
        return True
    return False


class Command(BaseCommand):
    help = "Delete spam contact form submissions"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would be deleted without actually deleting",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        entries = ContactUsForm.objects.all()
        spam = [e for e in entries if is_spam(e)]

        if not spam:
            self.stdout.write(self.style.SUCCESS("No spam found."))
            return

        for e in spam:
            name = f"{e.first_name} {e.last_name}"[:50]
            self.stdout.write(f"  #{e.id} - {name} - {e.email} - {e.created_at}")

        if dry_run:
            self.stdout.write(self.style.WARNING(f"\nDry run: {len(spam)} spam entries found (not deleted)."))
        else:
            ids = [e.id for e in spam]
            ContactUsForm.objects.filter(id__in=ids).delete()
            self.stdout.write(self.style.SUCCESS(f"\nDeleted {len(spam)} spam entries."))
