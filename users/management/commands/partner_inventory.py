"""Read-only reconciliation inventory; no identities or historical rows are changed."""
import json
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum
from django.db.models.functions import Lower, Trim
from users.models import TravelAgent, Agency, PartnerBooking, PartnerIdentity, AgencyApplication, PartnerOutbox, AffiliationClaim
from reservations.models import Reservation

class Command(BaseCommand):
    help='Print a read-only partner migration/reconciliation inventory (aggregate counts only).'
    def handle(self,*args,**options):
        report={
            'agents':TravelAgent.objects.count(),
            'agencies':Agency.objects.count(),
            'agent_bookings':Reservation.objects.exclude(travel_agent=None).count(),
            'paid_commission':str(Reservation.objects.filter(commission_paid=True).aggregate(total=Sum('commission_amount'))['total'] or 0),
            'unlinked_named_agents':TravelAgent.objects.filter(agency=None).exclude(agency_name__isnull=True).exclude(agency_name='').count(),
            'agency_payment_inconsistencies':TravelAgent.objects.filter(Q(payment_method='agency') & (Q(agency=None)|Q(agency_handles_payment=False))).count(),
            'route_flag_without_agency':TravelAgent.objects.filter(agency=None,agency_handles_payment=True).count(),
            'headless_agencies':Agency.objects.filter(heads=None).count(),
            'duplicate_email_groups':User.objects.exclude(email='').annotate(key=Lower(Trim('email'))).values('key').annotate(n=Count('pk')).filter(n__gt=1).count(),
            'duplicate_username_groups':User.objects.annotate(key=Lower('username')).values('key').annotate(n=Count('pk')).filter(n__gt=1).count(),
        }
        # Also usable before the onboarding migrations exist.
        from django.db import connection
        tables=connection.introspection.table_names()
        if PartnerBooking._meta.db_table in tables:
            report.update(recorded_bookings=PartnerBooking.objects.count(),
                unrecorded_bookings=Reservation.objects.exclude(travel_agent=None).filter(partner_context=None).count(),
                held_bookings=PartnerBooking.objects.exclude(hold='').filter(reservation__commission_paid=False).count(),
                applications_pending=AgencyApplication.objects.filter(state__in=['pending','information']).count(),
                agency_waiting=AffiliationClaim.objects.filter(state__in=['candidate','payment_pending']).count(),
                notification_failures=PartnerOutbox.objects.filter(state='failed').count())
        self.stdout.write(json.dumps(report,indent=2))
