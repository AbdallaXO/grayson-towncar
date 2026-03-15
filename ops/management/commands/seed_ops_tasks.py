"""
Seed realistic operational task data for testing the ops task queue UI.

Usage:
    python manage.py seed_ops_tasks
    python manage.py seed_ops_tasks --clear   # wipe seeded data first

All created objects are tagged with [OPS_SEED] in notes/private_notes
and "_seed": True in task metadata so they can be safely removed.
"""

from datetime import timedelta, time, datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db.models.signals import post_save, pre_save
from django.utils import timezone

from drivers.models import Driver
from ops.models import OperationalTask, CommunicationAttempt, StaffActivity
from ops.signals import (
    _ops_store_res_old_status,
    _ops_reservation_task_handler,
    _ops_store_leg_old_driver,
    _ops_leg_task_handler,
    _ops_contact_form_handler,
    _ops_payment_task_handler,
)
from payment.models import Payment
from rates.models import Rate, Vehicle
from reservations.models import Customer, Flight, Lead, Leg, Reservation
from users.models import ContactUsForm

SEED_TAG = "[OPS_SEED]"


class Command(BaseCommand):
    help = "Seed realistic operational task data for testing the ops task queue UI."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Remove previously seeded ops test data before re-seeding.",
        )

    def handle(self, *args, **options):
        if options["clear"]:
            self._clear()

        self._disconnect_signals()

        try:
            rate, vehicle = self._ensure_prerequisites()
            sarah, mike = self._create_staff_users()
            driver_a, driver_b = self._create_drivers()
            customers = self._create_customers()
            leads = self._create_leads(vehicle)
            reservations, legs = self._create_reservations(
                customers, rate, vehicle, driver_a, driver_b
            )
            flights = self._create_flights(legs)
            self._create_payments(reservations)
            contact_forms = self._create_contact_forms()
            tasks = self._create_tasks(reservations, legs, flights, contact_forms, sarah, mike)
            self._create_comm_attempts(tasks, sarah, mike)
            self._create_staff_activities(tasks, sarah, mike)
        finally:
            self._reconnect_signals()

        self.stdout.write(
            self.style.SUCCESS(
                "\nDone! Visit /dispatching/task-queue/ to see the task queue."
            )
        )

    # ------------------------------------------------------------------
    # Clear
    # ------------------------------------------------------------------

    def _clear(self):
        t_count = OperationalTask.objects.filter(metadata__has_key="_seed").count()
        OperationalTask.objects.filter(metadata__has_key="_seed").delete()

        # Delete payments first (PROTECT FK), then reservations (cascades legs/flights)
        seed_res = Reservation.objects.filter(private_notes__contains=SEED_TAG)
        r_count = seed_res.count()
        Payment.objects.filter(reservation__in=seed_res).delete()
        seed_res.delete()

        l_count = Lead.objects.filter(notes__contains=SEED_TAG).count()
        Lead.objects.filter(notes__contains=SEED_TAG).delete()

        cf_count = ContactUsForm.objects.filter(about__contains=SEED_TAG).count()
        ContactUsForm.objects.filter(about__contains=SEED_TAG).delete()

        Customer.objects.filter(
            email__endswith="@ops-seed.test"
        ).filter(reservation__isnull=True).delete()

        Driver.objects.filter(
            profile__username__startswith="ops_seed_driver_"
        ).delete()
        User.objects.filter(username__startswith="ops_seed_").delete()

        StaffActivity.objects.filter(metadata__has_key="_seed").delete()

        self.stdout.write(
            self.style.WARNING(
                f"Cleared: {t_count} tasks, {r_count} reservations, {l_count} leads, {cf_count} contact forms"
            )
        )

    # ------------------------------------------------------------------
    # Signal management
    # ------------------------------------------------------------------

    def _disconnect_signals(self):
        pre_save.disconnect(_ops_store_res_old_status, sender=Reservation)
        post_save.disconnect(_ops_reservation_task_handler, sender=Reservation)
        pre_save.disconnect(_ops_store_leg_old_driver, sender=Leg)
        post_save.disconnect(_ops_leg_task_handler, sender=Leg)
        post_save.disconnect(_ops_contact_form_handler, sender=ContactUsForm)
        post_save.disconnect(_ops_payment_task_handler, sender=Payment)
        self.stdout.write("  Disconnected ops signals")

    def _reconnect_signals(self):
        pre_save.connect(_ops_store_res_old_status, sender=Reservation)
        post_save.connect(_ops_reservation_task_handler, sender=Reservation)
        pre_save.connect(_ops_store_leg_old_driver, sender=Leg)
        post_save.connect(_ops_leg_task_handler, sender=Leg)
        post_save.connect(_ops_contact_form_handler, sender=ContactUsForm)
        post_save.connect(_ops_payment_task_handler, sender=Payment)
        self.stdout.write("  Reconnected ops signals")

    # ------------------------------------------------------------------
    # Prerequisites
    # ------------------------------------------------------------------

    def _ensure_prerequisites(self):
        if not Rate.objects.exists():
            self.stderr.write(
                self.style.ERROR(
                    "No rates found. Run `python manage.py loadrates` first."
                )
            )
            raise SystemExit(1)
        rate = Rate.objects.select_related("vehicle", "route").first()
        vehicle = Vehicle.objects.first()
        self.stdout.write(f"  Using rate: {rate} / vehicle: {vehicle}")
        return rate, vehicle

    def _create_staff_users(self):
        sarah, _ = User.objects.get_or_create(
            username="ops_seed_sarah",
            defaults={
                "first_name": "Sarah",
                "last_name": "Mitchell",
                "is_staff": True,
                "is_active": True,
            },
        )
        mike, _ = User.objects.get_or_create(
            username="ops_seed_mike",
            defaults={
                "first_name": "Mike",
                "last_name": "Torres",
                "is_staff": True,
                "is_active": True,
            },
        )
        self.stdout.write(f"  Staff: {sarah.get_full_name()}, {mike.get_full_name()}")
        return sarah, mike

    def _create_drivers(self):
        user_a, _ = User.objects.get_or_create(
            username="ops_seed_driver_tom",
            defaults={"first_name": "Tom", "last_name": "Rivera", "is_active": True},
        )
        driver_a, _ = Driver.objects.get_or_create(
            profile=user_a,
            defaults={
                "driver_type": "inhouse",
                "phone_number": "407-555-9901",
                "schedule": "Mon-Fri: 4AM-3PM",
            },
        )
        user_b, _ = User.objects.get_or_create(
            username="ops_seed_driver_ray",
            defaults={"first_name": "Ray", "last_name": "Santos", "is_active": True},
        )
        driver_b, _ = Driver.objects.get_or_create(
            profile=user_b,
            defaults={
                "driver_type": "inhouse",
                "phone_number": "321-555-9902",
                "schedule": "Mon-Sat: 5AM-4PM",
            },
        )
        self.stdout.write(f"  Drivers: {driver_a}, {driver_b}")
        return driver_a, driver_b

    # ------------------------------------------------------------------
    # Domain data
    # ------------------------------------------------------------------

    def _create_customers(self):
        CUSTOMER_DATA = [
            ("Jennifer", "Whitfield", "j.whitfield@ops-seed.test", "407-555-0201", "32801"),
            ("Marcus", "Chen", "m.chen@ops-seed.test", "321-555-0302", "34747"),
            ("Amanda", "Rodriguez", "a.rodriguez@ops-seed.test", "407-555-0403", "32819"),
            ("David", "Thompson", "d.thompson@ops-seed.test", "863-555-0504", "33896"),
            ("Sarah", "O'Brien", "s.obrien@ops-seed.test", "407-555-0605", "32836"),
            ("Robert", "Nakamura", "r.nakamura@ops-seed.test", "321-555-0706", "32920"),
            ("Lisa", "Patel", "l.patel@ops-seed.test", "407-555-0807", "34786"),
            ("James", "Morrison", "j.morrison@ops-seed.test", "352-555-0908", "34714"),
        ]
        customers = []
        for first, last, email, phone, zipcode in CUSTOMER_DATA:
            c, _ = Customer.objects.get_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "phone_number": phone,
                    "zipcode": zipcode,
                },
            )
            customers.append(c)
        self.stdout.write(f"  Created/found {len(customers)} customers")
        return customers

    def _create_leads(self, vehicle):
        today = timezone.localdate()
        now = timezone.now()

        LEAD_DATA = [
            {
                "first_name": "Lisa",
                "last_name": "Patel",
                "email": "l.patel.lead@ops-seed.test",
                "phone": "407-555-0807",
                "pickup_location": "Orlando International Airport (MCO)",
                "dropoff_location": "Disney's Grand Floridian Resort",
                "pickup_date": today + timedelta(days=2),
                "estimated_price": Decimal("185.00"),
                "status": "new",
                "segment": "airport_transfer",
                "utm_source": "google",
                "notes": SEED_TAG,
            },
            {
                "first_name": "James",
                "last_name": "Morrison",
                "email": "j.morrison.lead@ops-seed.test",
                "phone": "352-555-0908",
                "pickup_location": "Orlando International Airport (MCO)",
                "dropoff_location": "Universal's Royal Pacific Resort",
                "pickup_date": today + timedelta(days=8),
                "estimated_price": Decimal("165.00"),
                "status": "new",
                "segment": "theme_park",
                "utm_source": "facebook",
                "notes": SEED_TAG,
            },
            {
                "first_name": "Robert",
                "last_name": "Nakamura",
                "email": "r.nakamura.lead@ops-seed.test",
                "phone": "321-555-0706",
                "pickup_location": "Port Canaveral Cruise Terminal",
                "dropoff_location": "Orlando International Airport (MCO)",
                "pickup_date": today + timedelta(days=5),
                "estimated_price": Decimal("220.00"),
                "status": "contacted",
                "segment": "cruise_transfer",
                "utm_source": "google",
                "contact_attempts": 1,
                "last_contact_date": now - timedelta(hours=2),
                "notes": SEED_TAG,
            },
            {
                "first_name": "Diana",
                "last_name": "Fletcher",
                "email": "d.fletcher.lead@ops-seed.test",
                "phone": "407-555-1122",
                "pickup_location": "Hilton Orlando Bonnet Creek",
                "dropoff_location": "Orlando International Airport (MCO)",
                "pickup_date": today + timedelta(days=10),
                "estimated_price": Decimal("155.00"),
                "status": "interested",
                "segment": "airport_transfer",
                "utm_source": "direct",
                "contact_attempts": 2,
                "last_contact_date": now - timedelta(hours=6),
                "notes": SEED_TAG,
            },
            {
                "first_name": "Carlos",
                "last_name": "Mendez",
                "email": "c.mendez.lead@ops-seed.test",
                "phone": "321-555-3344",
                "pickup_location": "Orlando International Airport (MCO)",
                "dropoff_location": "Marriott World Center",
                "pickup_date": today - timedelta(days=3),
                "estimated_price": Decimal("175.00"),
                "status": "converted",
                "segment": "general",
                "utm_source": "google",
                "converted": True,
                "converted_at": now - timedelta(days=2),
                "contact_attempts": 3,
                "notes": SEED_TAG,
            },
        ]

        leads = []
        for data in LEAD_DATA:
            lead, _ = Lead.objects.get_or_create(
                email=data["email"],
                defaults={**data, "vehicle": vehicle},
            )
            leads.append(lead)
        self.stdout.write(f"  Created/found {len(leads)} leads")
        return leads

    def _create_contact_forms(self):
        now = timezone.now()
        forms_data = [
            {
                "first_name": "Patricia",
                "last_name": "Nguyen",
                "email": "patricia.nguyen@example.com",
                "phone_number": "407-555-0801",
                "contact_method": "phone",
                "about": f"We need transportation for a wedding party of 12 from the Ritz-Carlton to "
                         f"Bella Collina on April 5th. Looking for pricing on a van or multiple cars. {SEED_TAG}",
                "status": "pending",
            },
            {
                "first_name": "David",
                "last_name": "Kim",
                "email": "david.kim@example.com",
                "phone_number": "321-555-0802",
                "contact_method": "email",
                "about": f"Corporate event next month — need 3 towncar shuttles between Hyatt Regency "
                         f"and the convention center for 2 days. Can you send a quote? {SEED_TAG}",
                "status": "pending",
            },
            {
                "first_name": "Maria",
                "last_name": "Santos",
                "email": "maria.santos@example.com",
                "phone_number": "863-555-0803",
                "contact_method": "text",
                "about": f"Airport pickup for family of 4 arriving on Southwest flight, need car seat "
                         f"for toddler. Flying into MCO March 20. {SEED_TAG}",
                "status": "contacted",
                "contacted_at": now - timedelta(hours=6),
            },
        ]

        forms = []
        for data in forms_data:
            form, _ = ContactUsForm.objects.get_or_create(
                email=data["email"],
                about__contains=SEED_TAG,
                defaults=data,
            )
            forms.append(form)
        self.stdout.write(f"  Created/found {len(forms)} contact forms")
        return forms

    def _create_reservations(self, customers, rate, vehicle, driver_a, driver_b):
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        day_after = today + timedelta(days=2)
        three_days = today + timedelta(days=3)
        five_days = today + timedelta(days=5)
        yesterday = today - timedelta(days=1)

        # Create 8 reservations with varying states
        RES_DATA = [
            # 0: Jennifer - unpaid, today pickup, no driver (driver_assign task)
            {
                "customer": customers[0],
                "trip_type": "one_way",
                "base_price": Decimal("285.00"),
                "total_price": Decimal("285.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(5, 30),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Disney's Grand Floridian Resort",
                     "driver": None},
                ],
            },
            # 1: Marcus - card saved, day-after pickup, has driver
            {
                "customer": customers[1],
                "trip_type": "one_way",
                "base_price": Decimal("195.00"),
                "total_price": Decimal("195.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": day_after, "pickup_time": time(14, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Hilton Orlando Bonnet Creek",
                     "driver": driver_a},
                ],
            },
            # 2: Amanda - has flight with mismatch, tomorrow pickup, has driver
            {
                "customer": customers[2],
                "trip_type": "one_way",
                "base_price": Decimal("175.00"),
                "total_price": Decimal("175.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": tomorrow, "pickup_time": time(14, 30),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Universal's Royal Pacific Resort",
                     "driver": driver_a,
                     "_flight": "DL1842_mismatch"},
                ],
            },
            # 3: David - has flight with mismatch (escalated), tomorrow pickup
            {
                "customer": customers[3],
                "trip_type": "one_way",
                "base_price": Decimal("210.00"),
                "total_price": Decimal("210.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": tomorrow, "pickup_time": time(11, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Marriott World Center",
                     "driver": driver_b,
                     "_flight": "WN2156_mismatch"},
                ],
            },
            # 4: Sarah - tomorrow pickup, driver assigned, no SMS sent
            {
                "customer": customers[4],
                "trip_type": "one_way",
                "base_price": Decimal("165.00"),
                "total_price": Decimal("165.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": tomorrow, "pickup_time": time(8, 15),
                     "pickup_location": "Disney's Animal Kingdom Lodge",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": driver_b},
                ],
            },
            # 5: Robert - 3 days out, no driver (coverage gap)
            {
                "customer": customers[5],
                "trip_type": "round_trip",
                "base_price": Decimal("340.00"),
                "total_price": Decimal("340.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": three_days, "pickup_time": time(11, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Port Canaveral Cruise Terminal",
                     "driver": None},
                    {"pickup_date": three_days + timedelta(days=7), "pickup_time": time(9, 0),
                     "pickup_location": "Port Canaveral Cruise Terminal",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": None},
                ],
            },
            # 6: Lisa - day-after, no driver
            {
                "customer": customers[6],
                "trip_type": "one_way",
                "base_price": Decimal("195.00"),
                "total_price": Decimal("195.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": day_after, "pickup_time": time(16, 0),
                     "pickup_location": "Sanford International Airport (SFB)",
                     "dropoff_location": "Wyndham Bonnet Creek",
                     "driver": None},
                ],
            },
            # 7: James - completed reservation (for completed tasks)
            {
                "customer": customers[7],
                "trip_type": "one_way",
                "base_price": Decimal("175.00"),
                "total_price": Decimal("175.00"),
                "status": "completed",
                "legs": [
                    {"pickup_date": yesterday, "pickup_time": time(10, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Hyatt Regency Grand Cypress",
                     "driver": driver_a,
                     "confirmation_sms_sent_at": timezone.now() - timedelta(days=2)},
                ],
            },
        ]

        reservations = []
        legs_map = {}  # res_index -> list of legs

        for i, rdata in enumerate(RES_DATA):
            leg_datas = rdata.pop("legs")
            tag = f"{SEED_TAG} Res #{i}"

            # Check for existing seed reservation for this customer
            res = Reservation.objects.filter(
                customer=rdata["customer"],
                private_notes__contains=SEED_TAG,
            ).first()

            if not res:
                res = Reservation.objects.create(
                    **rdata, rate=rate, vehicle=vehicle, private_notes=tag,
                    passenger_count=2, luggage_count=2,
                )

            reservations.append(res)
            legs_map[i] = []

            for ldata in leg_datas:
                flight_key = ldata.pop("_flight", None)
                sms_sent = ldata.pop("confirmation_sms_sent_at", None)

                leg = Leg.objects.filter(
                    reservation=res,
                    pickup_date=ldata["pickup_date"],
                    pickup_time=ldata["pickup_time"],
                ).first()

                if not leg:
                    leg = Leg.objects.create(
                        reservation=res,
                        private_notes=tag,
                        **ldata,
                    )

                if sms_sent:
                    Leg.objects.filter(pk=leg.pk).update(
                        confirmation_sms_sent_at=sms_sent
                    )

                leg._flight_key = flight_key
                legs_map[i].append(leg)

        self.stdout.write(
            f"  Created/found {len(reservations)} reservations with legs"
        )
        return reservations, legs_map

    def _create_flights(self, legs_map):
        """Create flight records with intentional time mismatches."""
        now = timezone.now()
        flights = {}

        # Res #2 leg 0 — DL1842 arriving 1hr later than booked pickup
        leg_2_0 = legs_map[2][0]
        if getattr(leg_2_0, "_flight_key", None) and not leg_2_0.flight_information_id:
            flight_dt = datetime.combine(
                leg_2_0.pickup_date, leg_2_0.pickup_time
            ) + timedelta(hours=1)
            flight_dt = timezone.make_aware(
                flight_dt, timezone.get_current_timezone()
            )
            flight = Flight.objects.create(
                flight_type="arrival",
                airline="DL",
                airline_display_name="Delta Air Lines",
                flight_number="1842",
                origin="ATL - Hartsfield-Jackson Atlanta Intl",
                destination="MCO - Orlando Intl",
                status="Scheduled",
                scheduled_arrival_local=flight_dt,
                estimated_arrival_local=flight_dt,
                terminal="B",
                gate="76",
            )
            Leg.objects.filter(pk=leg_2_0.pk).update(flight_information=flight)
            leg_2_0.flight_information = flight
            flights["DL1842"] = flight
            self.stdout.write(f"  Flight DL1842 (1hr late mismatch)")

        # Res #3 leg 0 — WN2156 arriving 45min earlier than booked pickup
        leg_3_0 = legs_map[3][0]
        if getattr(leg_3_0, "_flight_key", None) and not leg_3_0.flight_information_id:
            flight_dt = datetime.combine(
                leg_3_0.pickup_date, leg_3_0.pickup_time
            ) - timedelta(minutes=45)
            flight_dt = timezone.make_aware(
                flight_dt, timezone.get_current_timezone()
            )
            flight = Flight.objects.create(
                flight_type="arrival",
                airline="WN",
                airline_display_name="Southwest Airlines",
                flight_number="2156",
                origin="BWI - Baltimore/Washington Intl",
                destination="MCO - Orlando Intl",
                status="Scheduled",
                scheduled_arrival_local=flight_dt,
                estimated_arrival_local=flight_dt - timedelta(minutes=10),
                terminal="A",
                gate="122",
            )
            Leg.objects.filter(pk=leg_3_0.pk).update(flight_information=flight)
            leg_3_0.flight_information = flight
            flights["WN2156"] = flight
            self.stdout.write(f"  Flight WN2156 (45min early mismatch)")

        return flights

    def _create_payments(self, reservations):
        """Create payment records — some paid, some not."""
        now = timezone.now()

        # Res #1 (Marcus) - card saved but not charged
        Payment.objects.get_or_create(
            reservation=reservations[1],
            status="card_saved",
            defaults={
                "customer": reservations[1].customer,
                "amount": Decimal("195.00"),
                "payment_type": "pay_later",
                "description": "Card on file",
            },
        )

        # Res #7 (James, completed) - fully paid
        Payment.objects.get_or_create(
            reservation=reservations[7],
            status="paid",
            defaults={
                "customer": reservations[7].customer,
                "amount": Decimal("175.00"),
                "payment_type": "pay_now",
                "description": "Full payment",
            },
        )

        self.stdout.write("  Created payments")

    # ------------------------------------------------------------------
    # Operational Tasks
    # ------------------------------------------------------------------

    def _create_tasks(self, reservations, legs_map, flights, contact_forms, sarah, mike):
        now = timezone.now()
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)
        tomorrow_9am = timezone.make_aware(
            datetime.combine(tomorrow, time(9, 0)),
            timezone.get_current_timezone(),
        )
        today_3pm = timezone.make_aware(
            datetime.combine(today, time(15, 0)),
            timezone.get_current_timezone(),
        )

        T = OperationalTask.TaskType
        S = OperationalTask.Status
        P = OperationalTask.Priority

        def _meta(**kw):
            return {"_seed": True, **kw}

        tasks = {}

        # ── payment_chase (3) ──

        tasks["payment_urgent"] = OperationalTask.objects.create(
            task_type=T.PAYMENT_CHASE, status=S.PENDING, priority=P.CRITICAL,
            title=f"Unpaid $285: Jennifer Whitfield — trip {tomorrow_9am:%b %d}",
            description="No payment on file. Pickup tomorrow.",
            reservation=reservations[0], due_at=now,
            escalate_at=tomorrow_9am,
            metadata=_meta(amount_owed="285.00", total_price="285.00",
                           days_until_pickup=1),
        )

        tasks["payment_in_progress"] = OperationalTask.objects.create(
            task_type=T.PAYMENT_CHASE, status=S.IN_PROGRESS, priority=P.HIGH,
            title=f"Unpaid $195: Marcus Chen — trip {(today + timedelta(days=2)):%b %d}",
            description="Card saved on file but not charged.",
            reservation=reservations[1], due_at=now,
            assigned_to=mike, attempts=1,
            last_attempt_at=now - timedelta(hours=3),
            metadata=_meta(amount_owed="195.00", total_price="195.00",
                           days_until_pickup=2),
        )

        tasks["payment_completed"] = OperationalTask.objects.create(
            task_type=T.PAYMENT_CHASE, status=S.COMPLETED, priority=P.MEDIUM,
            title=f"Unpaid $175: James Morrison — trip {yesterday:%b %d}",
            description="Guest paid via checkout link.",
            reservation=reservations[7], due_at=now - timedelta(days=3),
            resolved_at=now - timedelta(days=2), resolved_by=sarah,
            resolution_notes="Auto-closed: payment received ($175.00)",
            metadata=_meta(amount_owed="0"),
        )

        # ── flight_verify (2) ──

        tasks["flight_pending"] = OperationalTask.objects.create(
            task_type=T.FLIGHT_VERIFICATION, status=S.PENDING, priority=P.HIGH,
            title=f"Flight mismatch: Amanda Rodriguez — DL1842 arriving 1hr late",
            description="Booked pickup 2:30 PM, flight lands ~3:30 PM. Verify with guest.",
            leg=legs_map[2][0], reservation=reservations[2],
            due_at=now, escalate_at=now + timedelta(hours=4),
            metadata=_meta(mismatch_minutes=60, mismatch_direction="late",
                           flight_ident="DL1842"),
        )

        tasks["flight_escalated"] = OperationalTask.objects.create(
            task_type=T.FLIGHT_VERIFICATION, status=S.ESCALATED, priority=P.CRITICAL,
            title=f"Flight mismatch: David Thompson — WN2156 arriving 45min early",
            description="Booked pickup 11:00 AM, flight lands ~10:15 AM. Guest may wait.",
            leg=legs_map[3][0], reservation=reservations[3],
            due_at=now - timedelta(hours=4), escalate_at=now - timedelta(hours=1),
            attempts=1, last_attempt_at=now - timedelta(hours=2),
            metadata=_meta(mismatch_minutes=45, mismatch_direction="early",
                           flight_ident="WN2156"),
        )

        # ── contact_form (3) ──

        tasks["contact_pending_wedding"] = OperationalTask.objects.create(
            task_type=T.CONTACT_FORM, status=S.PENDING, priority=P.HIGH,
            title=f"Contact form: Patricia Nguyen",
            description="Wedding party of 12, Ritz-Carlton to Bella Collina, April 5th.",
            contact_form=contact_forms[0],
            due_at=now - timedelta(hours=2), escalate_at=now + timedelta(hours=2),
            metadata=_meta(email="patricia.nguyen@example.com", phone="407-555-0801",
                           contact_method="phone"),
        )

        tasks["contact_pending_corporate"] = OperationalTask.objects.create(
            task_type=T.CONTACT_FORM, status=S.PENDING, priority=P.HIGH,
            title=f"Contact form: David Kim",
            description="Corporate event — 3 towncar shuttles, 2 days, Hyatt to convention center.",
            contact_form=contact_forms[1],
            due_at=now - timedelta(hours=1), escalate_at=now + timedelta(hours=3),
            metadata=_meta(email="david.kim@example.com", phone="321-555-0802",
                           contact_method="email"),
        )

        tasks["contact_completed"] = OperationalTask.objects.create(
            task_type=T.CONTACT_FORM, status=S.COMPLETED, priority=P.HIGH,
            title=f"Contact form: Maria Santos",
            description="Airport pickup for family of 4, needs car seat, MCO March 20.",
            contact_form=contact_forms[2],
            due_at=now - timedelta(hours=8),
            resolved_at=now - timedelta(hours=6), resolved_by=sarah,
            resolution_notes="Called and quoted $85 for MCO pickup with car seat. Sent booking link.",
            metadata=_meta(email="maria.santos@example.com", phone="863-555-0803",
                           contact_method="text"),
        )

        # ── driver_assign (2) — today only ──

        tasks["driver_critical"] = OperationalTask.objects.create(
            task_type=T.DRIVER_ASSIGNMENT, status=S.PENDING, priority=P.CRITICAL,
            title=f"No driver: Jennifer Whitfield — {today:%b %d} 5:30 AM",
            description="MCO → Disney Grand Floridian. Early morning airport arrival.",
            leg=legs_map[0][0], reservation=reservations[0],
            due_at=now, escalate_at=now,
            metadata=_meta(pickup_date=str(today), pickup_time="05:30"),
        )

        tasks["driver_completed"] = OperationalTask.objects.create(
            task_type=T.DRIVER_ASSIGNMENT, status=S.COMPLETED, priority=P.CRITICAL,
            title=f"No driver: James Morrison — {yesterday:%b %d} 10:00 AM",
            description="MCO → Hyatt Regency Grand Cypress",
            leg=legs_map[7][0], reservation=reservations[7],
            due_at=now - timedelta(days=1),
            resolved_at=now - timedelta(days=1), resolved_by=mike,
            resolution_notes="Auto-closed: driver Tom Rivera assigned",
            metadata=_meta(),
        )

        # ── manual (2) ──

        tasks["manual_low"] = OperationalTask.objects.create(
            task_type=T.MANUAL, status=S.PENDING, priority=P.LOW,
            title="Review weekly dispatch capacity for spring break surge",
            description="Check driver availability for Mar 15-22. "
                        "May need to onboard 1-2 contract drivers.",
            due_at=now + timedelta(days=2),
            metadata=_meta(),
        )

        tasks["manual_in_progress"] = OperationalTask.objects.create(
            task_type=T.MANUAL, status=S.IN_PROGRESS, priority=P.MEDIUM,
            title="Call Disney concierge about VIP pickup policy change",
            description="New policy requires advance vehicle registration. "
                        "Get details and update driver briefing docs.",
            due_at=now, assigned_to=mike,
            metadata=_meta(),
        )

        self.stdout.write(f"  Created {len(tasks)} operational tasks")
        return tasks

    # ------------------------------------------------------------------
    # Communication Attempts
    # ------------------------------------------------------------------

    def _create_comm_attempts(self, tasks, sarah, mike):
        now = timezone.now()
        attempts = []

        # Payment in-progress: 1 attempt
        attempts.append(CommunicationAttempt(
            task=tasks["payment_in_progress"],
            channel="call", outcome="answered", staff_user=mike,
            contact_value="321-555-0302",
            notes="Guest said will pay tonight, card on file",
            duration_seconds=180,
        ))

        # Flight escalated: 1 failed attempt
        attempts.append(CommunicationAttempt(
            task=tasks["flight_escalated"],
            channel="call", outcome="no_answer", staff_user=sarah,
            contact_value="863-555-0504",
            notes="No answer, tried twice. Will try again in 1 hour.",
            duration_seconds=0,
        ))

        # Manual in-progress: 1 attempt
        attempts.append(CommunicationAttempt(
            task=tasks["manual_in_progress"],
            channel="call", outcome="voicemail", staff_user=mike,
            contact_value="407-555-0000",
            notes="Disney concierge office voicemail. Left callback number.",
            duration_seconds=30,
        ))

        CommunicationAttempt.objects.bulk_create(attempts)
        self.stdout.write(f"  Created {len(attempts)} communication attempts")

    # ------------------------------------------------------------------
    # Staff Activities
    # ------------------------------------------------------------------

    def _create_staff_activities(self, tasks, sarah, mike):
        now = timezone.now()
        activities = [
            StaffActivity(
                user=mike, action_type="task_claimed",
                task=tasks["payment_in_progress"],
                metadata={"_seed": True, "task_type": "payment_chase"},
            ),
            StaffActivity(
                user=mike, action_type="comm_logged",
                task=tasks["payment_in_progress"],
                metadata={"_seed": True, "channel": "call", "outcome": "answered"},
            ),
            StaffActivity(
                user=sarah, action_type="page_view",
                path="/dispatching/task-queue/",
                metadata={"_seed": True},
            ),
            StaffActivity(
                user=mike, action_type="page_view",
                path="/dispatching/task-queue/",
                metadata={"_seed": True},
            ),
        ]
        StaffActivity.objects.bulk_create(activities)
        self.stdout.write(f"  Created {len(activities)} staff activities")
