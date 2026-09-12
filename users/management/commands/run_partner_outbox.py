import time
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from users.partner_outbox import deliver_one, schedule_reminders


class Command(BaseCommand):
    help='Dedicated partner email worker. Use --once to drain currently due rows.'
    def add_arguments(self,parser):
        parser.add_argument('--once',action='store_true')
        parser.add_argument('--poll-seconds',type=int,default=5)
    def handle(self,*args,**options):
        if not getattr(settings,'PARTNER_EMAIL_DELIVERY_ENABLED',False):
            raise CommandError('Set PARTNER_EMAIL_DELIVERY_ENABLED=1 on the dedicated worker to send mail.')
        last_reminders=0
        while True:
            close_old_connections()
            if time.monotonic()-last_reminders>=60:
                schedule_reminders();last_reminders=time.monotonic()
            while deliver_one(): pass
            if options['once']: return
            time.sleep(max(1,options['poll_seconds']))
