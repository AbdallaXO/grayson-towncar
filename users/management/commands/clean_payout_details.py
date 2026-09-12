"""Move legacy free-text payout handles into the structured fields.

Only migrates values whose shape is unambiguous -- a valid email for PayPal, a
valid handle for Venmo. Anything else is reported for a human, because guessing
a payout destination wrong sends money to a stranger with no way back. Bank
details are never parsed out of prose for the same reason.

Dry run by default; pass --apply to write.
"""
import re
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from users.models import TravelAgent, Agency

EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$')
HANDLE = re.compile(r'^@?[A-Za-z0-9_-]{5,30}$')

# Handles and emails buried in labels: "Venmo: @name", "PayPal Email: x@y.com",
# "https://venmo.com/u/name". Extracting these is safe; deciding between two
# different rails in one field is not.
EMAIL_ANYWHERE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
HANDLE_ANYWHERE = re.compile(r'@([A-Za-z0-9_-]{5,30})')
VENMO_URL = re.compile(r'venmo\.com/u/([A-Za-z0-9_-]{5,30})', re.I)
PAYPAL_ME = re.compile(r'paypal\.me/([A-Za-z0-9_-]+)', re.I)


def extract_email(raw):
    """The single email in this text, or None if there are none or several."""
    found = {m.lower() for m in EMAIL_ANYWHERE.findall(raw)}
    return found.pop() if len(found) == 1 else None


def without_emails(raw):
    """Text with emails removed, so an address's domain is not read as a handle."""
    return EMAIL_ANYWHERE.sub(' ', raw)


def extract_handle(raw):
    """The single @handle in this text, or None if ambiguous."""
    url = VENMO_URL.search(raw)
    if url:
        return url.group(1)
    found = {m for m in HANDLE_ANYWHERE.findall(without_emails(raw))}
    if len(found) == 1:
        return found.pop()
    if found:
        return None
    # No @ at all: accept a bare token only when the text is just that token.
    stripped = re.sub(r'^(venmo|paypal|handle|account|username|user)\b[:\s-]*', '', raw.strip(), flags=re.I).strip()
    return stripped if HANDLE.match(stripped) and not EMAIL.match(stripped) else None


def mentions_both_rails(raw):
    """True when one field names PayPal and Venmo — a human must choose."""
    low = raw.lower()
    if 'venmo' in low and ('paypal' in low or PAYPAL_ME.search(raw)):
        return True
    # An email plus a genuine @handle beside it (not the email's own domain).
    return bool(EMAIL_ANYWHERE.search(raw) and HANDLE_ANYWHERE.search(without_emails(raw)))


class Command(BaseCommand):
    help = 'Migrate legacy payment_info text into structured payout fields (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the changes.')
        parser.add_argument('--show-review', action='store_true', help='List the records needing a human.')

    def handle(self, *args, **options):
        apply_changes = options['apply']
        stats = Counter()
        review = []

        for model, label in ((TravelAgent, 'agent'), (Agency, 'agency')):
            for obj in model.objects.all():
                method = (obj.payment_method or '').strip()
                raw = (obj.payment_info or '').strip()
                if method not in ('paypal', 'venmo'):
                    continue
                if method == 'paypal' and obj.paypal_email:
                    stats[f'{label}:paypal already structured'] += 1
                    continue
                if method == 'venmo' and obj.venmo_handle:
                    stats[f'{label}:venmo already structured'] += 1
                    continue
                if not raw:
                    stats[f'{label}:{method} blank'] += 1
                    continue

                # A field naming two rails is a choice, not a parse.
                if mentions_both_rails(raw):
                    stats[f'{label}:{method} names two rails'] += 1
                    review.append((label, obj.pk, method, raw))
                    continue

                value = extract_email(raw) if method == 'paypal' else extract_handle(raw)
                if value:
                    stats[f'{label}:{method} migrated'] += 1
                    if apply_changes:
                        if method == 'paypal':
                            obj.paypal_email = value
                            obj.save(update_fields=['paypal_email'])
                        else:
                            obj.venmo_handle = value
                            obj.save(update_fields=['venmo_handle'])
                else:
                    stats[f'{label}:{method} needs review'] += 1
                    review.append((label, obj.pk, method, raw))

        # Rails that were never structured, so the operator knows what remains.
        for model, label in ((TravelAgent, 'agent'), (Agency, 'agency')):
            stats[f'{label}:bank needs re-entry'] += model.objects.filter(
                payment_method='bank', bank_account_last4='').count()

        self.stdout.write(self.style.MIGRATE_HEADING(
            'APPLIED' if apply_changes else 'DRY RUN — nothing written (use --apply)'))
        for key in sorted(stats):
            self.stdout.write(f'  {key:38} {stats[key]}')

        if review:
            self.stdout.write('')
            self.stdout.write(f'{len(review)} record(s) need a human decision.')
            if options['show_review']:
                for label, pk, method, raw in review:
                    self.stdout.write(f'  {label} #{pk} [{method}] {raw!r}')
            else:
                self.stdout.write('  Re-run with --show-review to list them.')
