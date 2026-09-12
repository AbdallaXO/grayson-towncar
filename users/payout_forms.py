"""Validated payout detail fields, shared by the agent and agency forms.

One free-text box could not tell a PayPal email from a Venmo handle, and a
mistyped handle pays a stranger with no way back. Each rail is validated for the
shape it actually needs, including the ABA checksum on routing numbers.
"""
import re

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .models import PayoutDetails

VENMO_HANDLE = re.compile(r'^[A-Za-z0-9_-]{5,30}$')


def validate_routing_number(value):
    """ABA routing numbers carry a check digit; a typo almost never survives it."""
    digits = ''.join(c for c in (value or '') if c.isdigit())
    if len(digits) != 9:
        raise ValidationError('A routing number is exactly 9 digits.')
    weights = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    if sum(int(d) * w for d, w in zip(digits, weights)) % 10 != 0:
        raise ValidationError('That routing number is not valid. Please check it against a check or your bank app.')
    return digits


class PayoutDetailsMixin:
    """Adds per-rail payout fields to a form and validates the chosen rail.

    The host form must supply a ``payment_method`` field. Only the fields for the
    selected rail are required; the rest are ignored.
    """

    PAYOUT_FIELDS = {
        'paypal': ['paypal_email'],
        'venmo': ['venmo_handle'],
        'bank': ['bank_account_name', 'bank_routing_number', 'bank_account_type', 'bank_account_number'],
    }

    def add_payout_fields(self, instance=None):
        self.fields['paypal_email'] = forms.EmailField(
            label='PayPal email', required=False,
            help_text='The email address on the PayPal account.')
        self.fields['venmo_handle'] = forms.CharField(
            label='Venmo username', required=False, max_length=64,
            help_text='Without the @. Letters, numbers, hyphens and underscores.')
        self.fields['bank_account_name'] = forms.CharField(
            label='Name on the account', required=False, max_length=120)
        self.fields['bank_routing_number'] = forms.CharField(
            label='Routing number', required=False, max_length=9,
            help_text='9 digits.')
        self.fields['bank_account_type'] = forms.ChoiceField(
            label='Account type', required=False,
            choices=[('', 'Select')] + PayoutDetails.ACCOUNT_TYPE_CHOICES)
        self.fields['bank_account_number'] = forms.CharField(
            label='Account number', required=False, max_length=34,
            help_text='Stored encrypted. Only the last four digits are shown afterwards.')
        if instance is not None:
            for name in ['paypal_email', 'venmo_handle', 'bank_account_name',
                         'bank_routing_number', 'bank_account_type']:
                self.initial.setdefault(name, getattr(instance, name, '') or '')
            if instance.bank_account_last4:
                # The stored account shows in the field itself, masked. Typing a
                # new number replaces it; leaving it alone keeps what is saved.
                self.fields['bank_account_number'].widget.attrs['placeholder'] = instance.bank_account_masked
                self.fields['bank_account_number'].help_text = (
                    'Saved and encrypted. Type a new number only if you want to replace it.')

    def clean_venmo_handle(self):
        handle = (self.cleaned_data.get('venmo_handle') or '').strip().lstrip('@')
        if handle and not VENMO_HANDLE.match(handle):
            raise ValidationError('That does not look like a Venmo username. Use the name after the @ in their profile.')
        return handle

    def clean_bank_routing_number(self):
        value = (self.cleaned_data.get('bank_routing_number') or '').strip()
        return validate_routing_number(value) if value else ''

    def clean_bank_account_number(self):
        digits = ''.join(c for c in (self.cleaned_data.get('bank_account_number') or '') if c.isdigit())
        if digits and not (4 <= len(digits) <= 17):
            raise ValidationError('A US account number is between 4 and 17 digits.')
        return digits

    def validate_selected_rail(self, data, instance=None):
        """Require the fields the chosen rail needs, and nothing else."""
        method = data.get('payment_method') or ''
        if not method:
            return data
        for name in self.PAYOUT_FIELDS.get(method, []):
            if name == 'bank_account_number' and instance is not None and instance.bank_account_last4:
                continue  # keeping the stored account number is fine
            if not data.get(name):
                self.add_error(name, 'Required for this payment method.')
        if method == 'paypal' and data.get('paypal_email'):
            try:
                validate_email(data['paypal_email'])
            except ValidationError:
                self.add_error('paypal_email', 'Enter a valid email address.')
        return data

    def apply_payout_fields(self, obj):
        """Copy validated payout details onto the agent or agency."""
        data = self.cleaned_data
        obj.paypal_email = data.get('paypal_email') or ''
        obj.venmo_handle = data.get('venmo_handle') or ''
        obj.bank_account_name = data.get('bank_account_name') or ''
        obj.bank_routing_number = data.get('bank_routing_number') or ''
        obj.bank_account_type = data.get('bank_account_type') or ''
        if data.get('bank_account_number'):
            obj.set_bank_account(data['bank_account_number'])
        return obj
