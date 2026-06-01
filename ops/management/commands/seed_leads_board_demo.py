"""
Seed demo leads for the Leads Board so you can see every bucket populated.

    # create demo leads for a date (default = today + 5 days)
    python manage.py seed_leads_board_demo
    python manage.py seed_leads_board_demo --date 2026-06-03

    # remove them again
    python manage.py seed_leads_board_demo --clear

Demo leads are tagged by the "@leadsboard.demo" email domain, so --clear only
removes these (and cascades their nudge/task rows). The Lead→GHL create-sync
signal is disconnected during seeding so this NEVER creates real GHL contacts.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models.signals import post_save
from django.utils import timezone

DEMO_DOMAIN = "@leadsboard.demo"


class Command(BaseCommand):
    help = "Seed (or clear) demo leads to preview the Leads Board."

    def add_arguments(self, parser):
        parser.add_argument("--date", default=None, help="Pickup date YYYY-MM-DD (default: today + 5).")
        parser.add_argument("--clear", action="store_true", help="Delete previously-seeded demo leads and exit.")

    def handle(self, *args, **opts):
        from reservations.models import Lead
        from rates.models import Vehicle
        from ghl_integration.models import FollowUpTask
        import reservations.signals as rsignals

        if opts["clear"]:
            count, _ = Lead.objects.filter(email__endswith=DEMO_DOMAIN).delete()
            self.stdout.write(self.style.WARNING(f"Cleared demo leads ({count} rows incl. related)."))
            return

        if opts["date"]:
            try:
                target = datetime.strptime(opts["date"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--date must be YYYY-MM-DD")
        else:
            target = timezone.localdate() + timedelta(days=5)

        now = timezone.now()

        def veh(vtype, cap, lug):
            v = Vehicle.objects.filter(vehicle_type=vtype).first()
            return v or Vehicle.objects.create(vehicle_type=vtype, capacity=cap, luggage_capacity=lug)

        towncar = veh("towncar", 4, 4)
        suv = veh("suv", 6, 6)
        van = veh("van", 12, 12)
        minivan = veh("mini_van", 6, 6)

        S = Lead.StatusChoices
        ago = lambda **kw: now - timedelta(**kw)

        # Each row is built to land in a specific bucket (see ops/leads_board.py).
        specs = [
            # ── Safe to offer (cold but alive: no reply, no outbound in 48h) ──
            dict(bucket="safe", first_name="Marcus", last_name="Bell", vehicle=suv,
                 estimated_price=Decimal("145"), dropoff_location="Walt Disney World",
                 status=S.CONTACTED, last_contact_date=ago(days=6)),
            dict(bucket="safe", first_name="Priya", last_name="Nair", vehicle=towncar,
                 estimated_price=Decimal("110"), dropoff_location="Universal Orlando",
                 status=S.INTERESTED, last_contact_date=ago(days=5)),
            dict(bucket="safe", first_name="Tom", last_name="Hartman", vehicle=van,
                 estimated_price=Decimal("180"), dropoff_location="Port Canaveral",
                 segment="cruise_transfer", status=S.CONTACTED, last_contact_date=ago(days=4)),

            # ── Active — hands off ──
            dict(bucket="active", first_name="Jared", last_name="Cole", vehicle=suv,
                 estimated_price=Decimal("150"), dropoff_location="Walt Disney World",
                 status=S.INTERESTED, has_replied=True, last_reply_at=ago(days=1),
                 needs_human_follow_up=True, last_contact_date=ago(days=2),  # replied recently
                 _outbound="Hey Jared, still need a ride from MCO to Disney on the 6th? Happy to lock it in.",
                 _last_reply="Hey! Still comparing a couple of companies — what's your best round-trip price for 4 of us?"),
            dict(bucket="active", first_name="Sofia", last_name="Mendez", vehicle=towncar,
                 estimated_price=Decimal("120"), dropoff_location="Universal Orlando",
                 status=S.CONTACTED, last_contact_date=ago(hours=5)),  # we texted 5h ago
            dict(bucket="active", first_name="Derek", last_name="Yoon", vehicle=minivan,
                 estimated_price=Decimal("135"), dropoff_location="Walt Disney World",
                 status=S.CONTACTED, sequence_active=True, last_contact_date=ago(days=1)),  # sequence running

            # ── Already nudged (offer made, gone quiet) ──
            dict(bucket="nudged", first_name="Amara", last_name="Okafor", vehicle=van,
                 estimated_price=Decimal("175"), dropoff_location="Port Canaveral",
                 segment="cruise_transfer", status=S.CONTACTED, last_contact_date=ago(days=3)),

            # ── Booked / Lost / Opted out ──
            dict(bucket="booked", first_name="Will", last_name="Carter", vehicle=suv,
                 estimated_price=Decimal("160"), dropoff_location="Walt Disney World",
                 status=S.CONVERTED, converted=True, last_contact_date=ago(days=2)),
            dict(bucket="lost", first_name="Hannah", last_name="Price", vehicle=towncar,
                 estimated_price=Decimal("115"), dropoff_location="Universal Orlando",
                 status=S.LOST, last_contact_date=ago(days=9)),
            dict(bucket="optout", first_name="Greg", last_name="Salas", vehicle=towncar,
                 estimated_price=Decimal("130"), dropoff_location="Walt Disney World",
                 status=S.CONTACTED, sms_opt_out=True, last_contact_date=ago(days=2)),
        ]

        # Disconnect the create-sync signal so seeding can't create GHL contacts.
        # NOTE: sender must be the Lead class (matches @receiver(sender=Lead)),
        # not a string — a string silently fails to disconnect.
        post_save.disconnect(rsignals.sync_lead_to_ghl_on_create, sender=Lead)
        created = 0
        try:
            from ghl_integration.models import LeadActivity

            for i, spec in enumerate(specs, start=1):
                bucket = spec.pop("bucket")
                outbound = spec.pop("_outbound", None)
                last_reply = spec.pop("_last_reply", None)
                lead = Lead.objects.create(
                    email=f"{spec['first_name'].lower()}.{spec['last_name'].lower()}{DEMO_DOMAIN}",
                    phone=f"407-555-12{i:02d}",
                    pickup_location="Orlando Airport (MCO)",
                    pickup_date=target,
                    notes="LEADS_BOARD_DEMO",
                    **spec,
                )
                created += 1
                if bucket == "nudged":
                    FollowUpTask.objects.create(
                        lead=lead,
                        step_number=6,
                        segment="pre_pickup_cruise_urgency",
                        status=FollowUpTask.StatusChoices.SENT,
                        scheduled_at=now - timedelta(days=2),
                        sent_at=now - timedelta(days=2),
                        message_body="(demo) Hi Amara! Your sailing on Saturday is almost here — want me to lock in your $175 transfer? Just reply YES.",
                    )
                if outbound:
                    FollowUpTask.objects.create(
                        lead=lead, step_number=3, segment="general",
                        status=FollowUpTask.StatusChoices.SENT,
                        scheduled_at=now - timedelta(days=2), sent_at=now - timedelta(days=2),
                        message_body=outbound,
                    )
                if last_reply:
                    act = LeadActivity.objects.create(
                        lead=lead,
                        activity_type=LeadActivity.ActivityType.REPLY_RECEIVED,
                        description=f"SMS reply received: {last_reply[:100]}",
                        metadata={"message_body": last_reply, "message_preview": last_reply[:200]},
                    )
                    # created_at is auto_now_add — backdate it to match last_reply_at.
                    LeadActivity.objects.filter(pk=act.pk).update(created_at=now - timedelta(days=1))
        finally:
            post_save.connect(rsignals.sync_lead_to_ghl_on_create, sender=Lead)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {created} demo leads for {target.isoformat()}."
        ))
        self.stdout.write(f"  View:  /dispatching/leads-board/?date={target.isoformat()}")
        self.stdout.write(self.style.WARNING(
            "  Note: fake phone numbers — clicking 'Send offer' will attempt a real GHL send "
            "(it'll just fail on the bogus number). Edit one lead's phone to your own to test end-to-end."
        ))
        self.stdout.write("  Remove with:  python manage.py seed_leads_board_demo --clear")
