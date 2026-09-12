"""Print a new PARTNER_PAYOUT_KEY. Store it before using it anywhere."""
from cryptography.fernet import Fernet
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Generate a PARTNER_PAYOUT_KEY for encrypting payout account numbers.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(Fernet.generate_key().decode()))
        self.stdout.write('')
        self.stdout.write('Set this as PARTNER_PAYOUT_KEY and keep a copy in your password manager.')
        self.stdout.write('Changing or losing it makes existing bank account numbers unreadable.')
