"""Reversible encryption for payout account numbers.

Bank account numbers are recoverable by design -- the payout run decrypts them
to pay, and staff can reveal one deliberately. What this buys is that a stolen
database file, backup or developer snapshot carries ciphertext instead of
usable accounts.

Set PARTNER_PAYOUT_KEY in production (generate one with
``manage.py generate_payout_key``) and keep a copy somewhere safe: lose the key
and the stored numbers cannot be read back.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)

_UNSET_WARNED = False


def _key():
    """The Fernet key, or one derived from SECRET_KEY when none is configured."""
    global _UNSET_WARNED
    configured = (getattr(settings, 'PARTNER_PAYOUT_KEY', '') or '').strip()
    if configured:
        return configured.encode()
    # Development fallback so local work needs no extra setup. Derived keys die
    # with SECRET_KEY, so production must set an explicit key.
    if not _UNSET_WARNED:
        logger.warning(
            'PARTNER_PAYOUT_KEY is not set; deriving a payout key from SECRET_KEY. '
            'Set an explicit key in production or rotating SECRET_KEY will make '
            'stored bank account numbers unreadable.'
        )
        _UNSET_WARNED = True
    digest = hashlib.sha256(f'partner-payout:{settings.SECRET_KEY}'.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt(value):
    """Encrypt a secret. Blank in, blank out."""
    value = (value or '').strip()
    if not value:
        return ''
    return Fernet(_key()).encrypt(value.encode()).decode()


def decrypt(token):
    """Decrypt a stored secret, or return '' when it cannot be read.

    An unreadable value means the key changed or the row predates encryption;
    callers show the last four digits instead of crashing a payout run.
    """
    token = (token or '').strip()
    if not token:
        return ''
    try:
        return Fernet(_key()).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        logger.error('A stored payout account number could not be decrypted; check PARTNER_PAYOUT_KEY.')
        return ''


def last4(value):
    digits = ''.join(c for c in (value or '') if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else ''
