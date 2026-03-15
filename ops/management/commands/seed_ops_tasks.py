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
            drivers = self._get_or_create_drivers()
            customers = self._create_customers()
            leads = self._create_leads(vehicle)
            reservations, legs = self._create_reservations(
                customers, rate, vehicle, drivers
            )
            flights = self._create_flights(legs)
            self._create_payments(reservations)
            contact_forms = self._create_contact_forms()
            tasks = self._create_tasks(
                reservations, legs, flights, contact_forms, drivers, sarah, mike
            )
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
        # Save ALL receivers so we can restore them
        self._saved_post_save = list(post_save.receivers)
        self._saved_pre_save = list(pre_save.receivers)
        self._saved_post_save_uid = post_save.sender_receivers_cache.copy()

        # Nuclear option: disconnect ALL signals to prevent emails,
        # GHL syncs, commission updates, ops task creation, etc.
        post_save.receivers = []
        pre_save.receivers = []
        post_save.sender_receivers_cache.clear()
        pre_save.sender_receivers_cache.clear()

        self.stdout.write("  Disconnected ALL signals (will restore after)")

    def _reconnect_signals(self):
        post_save.receivers = self._saved_post_save
        pre_save.receivers = self._saved_pre_save
        post_save.sender_receivers_cache.clear()
        pre_save.sender_receivers_cache.clear()

        self.stdout.write("  Restored ALL signals")

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
        self.stdout.write(f"  Using rate #{rate.pk} / vehicle #{vehicle.pk}")
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

    def _get_or_create_drivers(self):
        """
        Get existing inhouse drivers or create seed drivers.
        Prefers real drivers from seed_inhouse_test_data for realism.
        """
        # Try to use existing inhouse drivers
        existing = list(
            Driver.objects.filter(
                driver_type="inhouse",
                profile__is_active=True,
            ).select_related("profile").order_by("profile__first_name")
        )

        if len(existing) >= 3:
            self.stdout.write(f"  Using {len(existing)} existing in-house drivers")
            return existing

        # Fallback: create seed drivers
        SEED_DRIVERS = [
            ("ops_seed_driver_neuma", "Neuma", "", "407-624-7385"),
            ("ops_seed_driver_david", "David", "Encarancion", "848-203-7511"),
            ("ops_seed_driver_rayyan", "Rayyan", "Vorajee", "407-633-9901"),
            ("ops_seed_driver_alex", "Alex", "", ""),
            ("ops_seed_driver_shipo", "Shipo", "", "321-202-9865"),
            ("ops_seed_driver_hasan", "Hasan", "", "407-242-3391"),
        ]
        drivers = []
        for username, first, last, phone in SEED_DRIVERS:
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={"first_name": first, "last_name": last, "is_active": True},
            )
            driver, _ = Driver.objects.get_or_create(
                profile=user,
                defaults={
                    "driver_type": "inhouse",
                    "phone_number": phone,
                    "schedule": "Mon-Sun: 4AM-EOD",
                },
            )
            drivers.append(driver)
        self.stdout.write(f"  Created {len(drivers)} seed drivers")
        return drivers

    # ------------------------------------------------------------------
    # Domain data
    # ------------------------------------------------------------------

    def _create_customers(self):
        CUSTOMER_DATA = [
            ("Cristy", "Cole", "c.cole@ops-seed.test", "407-555-0201", "32801"),
            ("Brian", "Caine", "b.caine@ops-seed.test", "321-555-0302", "34747"),
            ("Kari", "Bayer", "k.bayer@ops-seed.test", "407-555-0403", "32819"),
            ("David", "Jennings", "d.jennings@ops-seed.test", "863-555-0504", "33896"),
            ("Troy", "Moon", "t.moon@ops-seed.test", "407-555-0605", "32836"),
            ("Heather", "Hunt", "h.hunt@ops-seed.test", "321-555-0706", "32920"),
            ("Lauren", "Kellner", "l.kellner@ops-seed.test", "407-555-0807", "34786"),
            ("Kristin", "Clay", "k.clay@ops-seed.test", "352-555-0908", "34714"),
            ("Tim", "Francis", "t.francis@ops-seed.test", "407-555-1001", "32801"),
            ("Jordan", "Petty", "j.petty@ops-seed.test", "321-555-1102", "34747"),
            ("Jeffrey", "Best", "j.best@ops-seed.test", "407-555-1203", "32819"),
            ("Meredith", "Moore", "m.moore@ops-seed.test", "863-555-1304", "33896"),
            ("Amy", "Hill", "a.hill@ops-seed.test", "407-555-1405", "32836"),
            ("Jeff", "Phillips", "j.phillips@ops-seed.test", "321-555-1506", "32920"),
            ("Amy", "Rice", "a.rice@ops-seed.test", "407-555-1607", "34786"),
            ("Kimberly", "Rowan", "k.rowan@ops-seed.test", "352-555-1708", "34714"),
            ("Mark", "Pells", "m.pells@ops-seed.test", "407-555-1809", "32801"),
            ("Mariya", "Oncioiu", "m.oncioiu@ops-seed.test", "321-555-1910", "34747"),
            ("Matthew", "Carty", "m.carty@ops-seed.test", "407-555-2011", "32819"),
            ("Donald", "Gillon", "d.gillon@ops-seed.test", "863-555-2112", "33896"),
            ("Roberta", "Delaney", "r.delaney@ops-seed.test", "407-555-2213", "32836"),
            ("Cassie", "Rector", "c.rector@ops-seed.test", "321-555-2314", "32920"),
            ("Lance", "Morring", "l.morring@ops-seed.test", "407-555-2415", "34786"),
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
                "first_name": "Christine",
                "last_name": "Crain",
                "email": "christine.crain@example.com",
                "phone_number": "407-555-0801",
                "contact_method": "phone",
                "about": f"We need transportation for a wedding party of 12 from the Ritz-Carlton to "
                         f"Bella Collina on April 5th. Looking for pricing on a van or multiple cars. {SEED_TAG}",
                "status": "pending",
            },
            {
                "first_name": "Jennifer",
                "last_name": "Jones",
                "email": "jennifer.jones@example.com",
                "phone_number": "321-555-0802",
                "contact_method": "email",
                "about": f"Corporate event next month — need 3 towncar shuttles between Hyatt Regency "
                         f"and the convention center for 2 days. Can you send a quote? {SEED_TAG}",
                "status": "pending",
            },
            {
                "first_name": "Simon",
                "last_name": "Nix",
                "email": "simon.nix@example.com",
                "phone_number": "863-555-0803",
                "contact_method": "text",
                "about": f"Airport pickup for family of 4 arriving on Southwest flight, need car seat "
                         f"for toddler. Flying into MCO March 20. {SEED_TAG}",
                "status": "pending",
            },
            {
                "first_name": "Joriuckror",
                "last_name": "Joriuckror",
                "email": "joriuckror@example.com",
                "phone_number": "",
                "contact_method": "email",
                "about": f"Inquiry about group transportation rates for conference attendees. {SEED_TAG}",
                "status": "pending",
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

    def _create_reservations(self, customers, rate, vehicle, drivers):
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        day_after = today + timedelta(days=2)
        three_days = today + timedelta(days=3)
        yesterday = today - timedelta(days=1)

        # Get drivers by index (safely)
        def drv(idx):
            return drivers[idx] if idx < len(drivers) else drivers[0]

        # Reservations: designed to create realistic conflicts and gaps
        # driver indices map to the drivers list from _get_or_create_drivers
        RES_DATA = [
            # ─── Driver conflict legs: same driver, overlapping times ───

            # 0: Cristy Cole — arrival, driver[0], 2:24 PM (conflicts with #1)
            {
                "customer": customers[0],
                "trip_type": "one_way",
                "base_price": Decimal("185.00"),
                "total_price": Decimal("185.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(14, 24),
                     "pickup_location": "Sanford Fl International Airport ...",
                     "dropoff_location": "Disney's Art of Animation Resort,...",
                     "driver": drv(0),
                     "_flight": "G4_3723"},
                ],
            },
            # 1: Kari Bayer — departure, driver[0], 4:00 PM (conflicted by #0)
            {
                "customer": customers[2],
                "trip_type": "one_way",
                "base_price": Decimal("165.00"),
                "total_price": Decimal("165.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(16, 0),
                     "pickup_location": "Disney's Riviera Resort, Esplanade Avenue,...",
                     "dropoff_location": "Orlando International Airport (MCO),...",
                     "driver": drv(0),
                     "_flight": "WN_3443"},
                ],
            },
            # 2: Brian Caine — driver[1], 1:30 PM (conflicts with #3)
            {
                "customer": customers[1],
                "trip_type": "one_way",
                "base_price": Decimal("195.00"),
                "total_price": Decimal("195.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(13, 30),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Universal's Cabana Bay Beach Resort",
                     "driver": drv(1)},
                ],
            },
            # 3: extra leg for driver[1], 2:45 PM (conflicted by #2)
            {
                "customer": customers[3],
                "trip_type": "one_way",
                "base_price": Decimal("175.00"),
                "total_price": Decimal("175.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(14, 45),
                     "pickup_location": "Hyatt Regency Orlando",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": drv(1)},
                ],
            },
            # 4: driver[2] conflict leg A, 3:00 PM
            {
                "customer": customers[4],
                "trip_type": "one_way",
                "base_price": Decimal("210.00"),
                "total_price": Decimal("210.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(15, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Disney's Polynesian Village Resort",
                     "driver": drv(2),
                     "_flight": "UA_1410"},
                ],
            },
            # 5: driver[2] conflict leg B, 4:15 PM
            {
                "customer": customers[5],
                "trip_type": "one_way",
                "base_price": Decimal("185.00"),
                "total_price": Decimal("185.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(16, 15),
                     "pickup_location": "Disney's Contemporary Resort",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": drv(2)},
                ],
            },

            # ─── No-driver legs (driver_assign tasks) ───

            # 6-14: Various legs without drivers at different times today
            {
                "customer": customers[3],
                "trip_type": "one_way",
                "base_price": Decimal("195.00"),
                "total_price": Decimal("195.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(11, 5),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Marriott World Center",
                     "driver": None},
                ],
            },
            {
                "customer": customers[4],
                "trip_type": "one_way",
                "base_price": Decimal("210.00"),
                "total_price": Decimal("210.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(18, 16),
                     "pickup_location": "Disney's Animal Kingdom Lodge",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": None},
                ],
            },
            {
                "customer": customers[5],
                "trip_type": "one_way",
                "base_price": Decimal("155.00"),
                "total_price": Decimal("155.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(15, 15),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Hilton Orlando Bonnet Creek",
                     "driver": None},
                ],
            },
            {
                "customer": customers[6],
                "trip_type": "one_way",
                "base_price": Decimal("185.00"),
                "total_price": Decimal("185.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(17, 5),
                     "pickup_location": "Universal's Royal Pacific Resort",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": None},
                ],
            },
            {
                "customer": customers[7],
                "trip_type": "one_way",
                "base_price": Decimal("165.00"),
                "total_price": Decimal("165.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(18, 45),
                     "pickup_location": "Hilton Orlando Buena Vista Palace",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": None},
                ],
            },
            {
                "customer": customers[8],
                "trip_type": "one_way",
                "base_price": Decimal("175.00"),
                "total_price": Decimal("175.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(19, 0),
                     "pickup_location": "Disney's Riviera Resort",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": None},
                ],
            },
            {
                "customer": customers[9],
                "trip_type": "one_way",
                "base_price": Decimal("195.00"),
                "total_price": Decimal("195.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(15, 5),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Wyndham Bonnet Creek",
                     "driver": None},
                ],
            },
            {
                "customer": customers[10],
                "trip_type": "one_way",
                "base_price": Decimal("340.00"),
                "total_price": Decimal("340.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(18, 47),
                     "pickup_location": "Disney's Beach Club Resort",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": None},
                ],
            },
            {
                "customer": customers[11],
                "trip_type": "one_way",
                "base_price": Decimal("155.00"),
                "total_price": Decimal("155.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(15, 26),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Disney's Pop Century Resort",
                     "driver": None},
                ],
            },

            # ─── Flight mismatch legs (with drivers) ───

            # 15: Matthew Carty — JetBlue 2875, 7hr 51min late
            {
                "customer": customers[18],
                "trip_type": "one_way",
                "base_price": Decimal("185.00"),
                "total_price": Decimal("185.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(10, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Disney's Grand Floridian Resort",
                     "driver": drv(3) if len(drivers) > 3 else None,
                     "_flight": "B6_2875_late"},
                ],
            },
            # 16: Jeff Phillips — American 1709, 12hr early
            {
                "customer": customers[13],
                "trip_type": "one_way",
                "base_price": Decimal("295.00"),
                "total_price": Decimal("295.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": tomorrow, "pickup_time": time(22, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Universal's Aventura Hotel",
                     "driver": drv(4) if len(drivers) > 4 else None,
                     "_flight": "AA_1709_early"},
                ],
            },
            # 17: Donald Gillon — JetBlue 2875 (different day), 7hr late
            {
                "customer": customers[19],
                "trip_type": "one_way",
                "base_price": Decimal("185.00"),
                "total_price": Decimal("185.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(9, 30),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Marriott World Center",
                     "driver": drv(5) if len(drivers) > 5 else None,
                     "_flight": "B6_2875_late2"},
                ],
            },
            # 18: Roberta Delaney — JetBlue 0351, 21hr early
            {
                "customer": customers[20],
                "trip_type": "one_way",
                "base_price": Decimal("195.00"),
                "total_price": Decimal("195.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": day_after, "pickup_time": time(20, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Hilton Orlando Bonnet Creek",
                     "driver": None,
                     "_flight": "B6_0351_early"},
                ],
            },
            # 19: Cassie Rector — Breeze 217, 1hr late
            {
                "customer": customers[21],
                "trip_type": "one_way",
                "base_price": Decimal("175.00"),
                "total_price": Decimal("175.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(15, 30),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Disney's Art of Animation Resort",
                     "driver": drv(3) if len(drivers) > 3 else None,
                     "_flight": "MX_217_late"},
                ],
            },

            # ─── Unpaid reservations ───

            # 20-26: Various unpaid amounts
            {
                "customer": customers[12],
                "trip_type": "round_trip",
                "base_price": Decimal("343.75"),
                "total_price": Decimal("343.75"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": day_after, "pickup_time": time(14, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Disney's Contemporary Resort",
                     "driver": None},
                ],
            },
            {
                "customer": customers[13],
                "trip_type": "one_way",
                "base_price": Decimal("295.00"),
                "total_price": Decimal("295.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": tomorrow, "pickup_time": time(10, 0),
                     "pickup_location": "Hilton Orlando",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": drv(0)},
                ],
            },
            {
                "customer": customers[14],
                "trip_type": "one_way",
                "base_price": Decimal("105.00"),
                "total_price": Decimal("105.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": tomorrow, "pickup_time": time(8, 0),
                     "pickup_location": "Marriott Village at Lake Buena Vista",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": drv(1)},
                ],
            },
            {
                "customer": customers[15],
                "trip_type": "round_trip",
                "base_price": Decimal("215.00"),
                "total_price": Decimal("215.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": day_after, "pickup_time": time(11, 30),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Disney's Yacht Club Resort",
                     "driver": None},
                ],
            },
            {
                "customer": customers[15],
                "trip_type": "one_way",
                "base_price": Decimal("195.00"),
                "total_price": Decimal("195.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": day_after, "pickup_time": time(16, 0),
                     "pickup_location": "Disney's Yacht Club Resort",
                     "dropoff_location": "Orlando International Airport (MCO)",
                     "driver": None},
                ],
            },
            {
                "customer": customers[16],
                "trip_type": "one_way",
                "base_price": Decimal("175.00"),
                "total_price": Decimal("175.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": day_after, "pickup_time": time(9, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Hyatt Regency Grand Cypress",
                     "driver": None},
                ],
            },
            {
                "customer": customers[17],
                "trip_type": "one_way",
                "base_price": Decimal("195.00"),
                "total_price": Decimal("195.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": tomorrow, "pickup_time": time(13, 0),
                     "pickup_location": "Orlando International Airport (MCO)",
                     "dropoff_location": "Wyndham Grand Orlando Resort",
                     "driver": None},
                ],
            },

            # ─── Driver conflict with flight (Lance Morring / driver[0] earlier leg) ───
            # 27: Lance Morring — arrival with Allegiant 3723 flight, same driver as #0
            {
                "customer": customers[22],
                "trip_type": "one_way",
                "base_price": Decimal("175.00"),
                "total_price": Decimal("175.00"),
                "status": "confirmed",
                "legs": [
                    {"pickup_date": today, "pickup_time": time(14, 24),
                     "pickup_location": "Sanford Fl International Airport",
                     "dropoff_location": "Disney's Art of Animation Resort",
                     "driver": drv(0),
                     "_flight": "G4_3723"},
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
                private_notes__contains=tag,
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
        flights = {}

        FLIGHT_CONFIGS = [
            # (res_idx, leg_idx, flight_key_prefix, config)

            # ─── Driver conflict flights ───

            # Allegiant 3723 — Sanford arrival (on time, used for conflict)
            (0, 0, "G4_3723", {
                "airline": "G4", "airline_display_name": "Allegiant Air",
                "flight_number": "3723", "origin": "PIE - St. Pete-Clearwater Intl",
                "destination": "SFB - Orlando Sanford Intl",
                "status": "En Route", "terminal": "", "gate": "",
                "shift_minutes": 0,
            }),
            # Southwest 3443 — MCO departure (on time, used for conflict)
            (1, 0, "WN_3443", {
                "airline": "WN", "airline_display_name": "Southwest Airlines",
                "flight_number": "3443", "origin": "MCO - Orlando Intl",
                "destination": "BNA - Nashville Intl",
                "status": "Scheduled", "terminal": "B", "gate": "129",
                "shift_minutes": 0,
                "is_departure": True,
            }),
            # United 1410 — 1hr late (from production: scheduled 6:16, arriving 7:16)
            (4, 0, "UA_1410", {
                "airline": "UA", "airline_display_name": "United Airlines",
                "flight_number": "1410", "origin": "EWR - Newark Liberty Intl",
                "destination": "MCO - Orlando Intl",
                "status": "Scheduled / Delayed", "terminal": "B", "gate": "83",
                "shift_minutes": 63,
            }),

            # ─── Flight mismatch: real production patterns ───

            # Delta 0328 — 33 min late (production: sched 5:19, arriving 5:52)
            (15, 0, "B6_2875_late", {
                "airline": "DL", "airline_display_name": "Delta Airlines",
                "flight_number": "0328", "origin": "ATL - Hartsfield-Jackson Atlanta Intl",
                "destination": "MCO - Orlando Intl",
                "status": "En Route / Delayed", "terminal": "B", "gate": "76",
                "shift_minutes": 33,
            }),
            # American 1709 — 6hr early (large schedule shift)
            (16, 0, "AA_1709_early", {
                "airline": "AA", "airline_display_name": "American Airlines",
                "flight_number": "1709", "origin": "DFW - Dallas/Fort Worth Intl",
                "destination": "MCO - Orlando Intl",
                "status": "Scheduled", "terminal": "B", "gate": "56",
                "shift_minutes": -366,
            }),
            # United 496 — cancelled (production: scheduled 5:52, cancelled)
            (17, 0, "B6_2875_late2", {
                "airline": "UA", "airline_display_name": "United Airlines",
                "flight_number": "496", "origin": "IAD - Washington Dulles Intl",
                "destination": "MCO - Orlando Intl",
                "status": "Cancelled", "terminal": "B", "gate": "",
                "shift_minutes": 0,
            }),
            # JetBlue 0351 — massive early (schedule change)
            (18, 0, "B6_0351_early", {
                "airline": "B6", "airline_display_name": "JetBlue Airways",
                "flight_number": "0351", "origin": "BOS - Boston Logan Intl",
                "destination": "MCO - Orlando Intl",
                "status": "Scheduled", "terminal": "A", "gate": "3",
                "shift_minutes": -1295,
            }),
            # American 2887 — 52 min late (production: sched 3:55, arriving 4:47)
            (19, 0, "MX_217_late", {
                "airline": "AA", "airline_display_name": "American Airlines",
                "flight_number": "2887", "origin": "CLT - Charlotte Douglas Intl",
                "destination": "MCO - Orlando Intl",
                "status": "Scheduled / Delayed", "terminal": "B", "gate": "42",
                "shift_minutes": 52,
            }),

            # Lance Morring — same Allegiant 3723
            (27, 0, "G4_3723", {
                "airline": "G4", "airline_display_name": "Allegiant Air",
                "flight_number": "3723", "origin": "PIE - St. Pete-Clearwater Intl",
                "destination": "SFB - Orlando Sanford Intl",
                "status": "En Route", "terminal": "", "gate": "",
                "shift_minutes": 0,
            }),
        ]

        for res_idx, leg_idx, key, config in FLIGHT_CONFIGS:
            if res_idx not in legs_map or leg_idx >= len(legs_map[res_idx]):
                continue
            leg = legs_map[res_idx][leg_idx]
            if not getattr(leg, "_flight_key", None):
                continue
            if leg.flight_information_id:
                continue  # Already has flight

            is_departure = config.get("is_departure", False)
            shift = config.get("shift_minutes", 0)

            scheduled_dt = datetime.combine(
                leg.pickup_date, leg.pickup_time
            )
            if not is_departure:
                scheduled_dt = scheduled_dt - timedelta(minutes=15)

            scheduled_dt = timezone.make_aware(
                scheduled_dt, timezone.get_current_timezone()
            )
            estimated_dt = scheduled_dt + timedelta(minutes=shift)

            flight_kwargs = {
                "flight_type": "departure" if is_departure else "arrival",
                "airline": config["airline"],
                "airline_display_name": config["airline_display_name"],
                "flight_number": config["flight_number"],
                "origin": config["origin"],
                "destination": config["destination"],
                "status": config["status"],
                "terminal": config["terminal"],
                "gate": config["gate"],
            }

            # Flight model only has arrival fields — departures just get
            # flight_type="departure" with no time fields
            if not is_departure:
                flight_kwargs.update({
                    "scheduled_arrival_local": scheduled_dt,
                    "estimated_arrival_local": estimated_dt,
                    "scheduled_gate_arrival_local": scheduled_dt + timedelta(minutes=5),
                    "estimated_gate_arrival_local": estimated_dt + timedelta(minutes=5),
                })

            flight = Flight.objects.create(**flight_kwargs)
            Leg.objects.filter(pk=leg.pk).update(flight_information=flight)
            leg.flight_information = flight
            flights[f"{key}_{res_idx}"] = flight

            shift_label = f"{abs(shift)}min {'late' if shift > 0 else 'early'}" if shift else "on time"
            self.stdout.write(
                f"  Flight {config['airline']}{config['flight_number']} ({shift_label})"
            )

        return flights

    def _create_payments(self, reservations):
        """Create payment records — some paid, some not."""
        # Unpaid reservations: indices 20-26
        UNPAID = [
            (20, Decimal("343.75")),
            (21, Decimal("295.00")),
            (22, Decimal("105.00")),
            (23, Decimal("215.00")),
            (24, Decimal("195.00")),
            (25, Decimal("175.00")),
            (26, Decimal("195.00")),
        ]
        for idx, amount in UNPAID:
            if idx < len(reservations):
                Payment.objects.get_or_create(
                    reservation=reservations[idx],
                    status="failed",
                    defaults={
                        "customer": reservations[idx].customer,
                        "amount": amount,
                        "payment_type": "pay_now",
                        "description": "Initial Payment — declined",
                    },
                )
        self.stdout.write(f"  Created {len(UNPAID)} unpaid payment records")

    # ------------------------------------------------------------------
    # Operational Tasks
    # ------------------------------------------------------------------

    def _create_tasks(self, reservations, legs_map, flights, contact_forms, drivers, sarah, mike):
        now = timezone.now()
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        day_after = today + timedelta(days=2)

        T = OperationalTask.TaskType
        S = OperationalTask.Status
        P = OperationalTask.Priority

        def _meta(**kw):
            return {"_seed": True, **kw}

        def drv(idx):
            return drivers[idx] if idx < len(drivers) else drivers[0]

        tasks = {}

        # ── driver_conflict (3) — matching production screenshot ──

        # Conflict 1: driver[0] (neuma) — legs 0 and 1 overlap
        if legs_map.get(0) and legs_map.get(1):
            leg_trigger = legs_map[1][0]  # 4:00 PM (the one driver will be late to)
            leg_conflict = legs_map[0][0]  # 2:24 PM (finishes late)
            driver = drv(0)
            tasks["conflict_neuma"] = OperationalTask.objects.create(
                task_type=T.DRIVER_CONFLICT, status=S.PENDING, priority=P.CRITICAL,
                title=f"Driver Conflict — {driver}",
                description="2:24 PM and 4:00 PM legs conflict — driver will be 19 min late. Reassign or adjust times.",
                leg=leg_trigger, reservation=leg_trigger.reservation,
                due_at=now, escalate_at=now,
                metadata=_meta(
                    driver_id=driver.id, driver_name=str(driver),
                    flight_ident="Southwest Airlines 3443",
                    mismatch_direction="overlap",
                    mismatch_minutes=19, mismatch_label="19 min late",
                    conflict_minutes=19,
                    conflicting_leg_id=leg_conflict.id,
                    pickup_date=str(today),
                    pickup_time=str(leg_trigger.pickup_time),
                ),
            )

        # Conflict 2: driver[1] — legs 2 and 3 overlap
        if legs_map.get(2) and legs_map.get(3):
            leg_trigger = legs_map[3][0]  # 2:45 PM
            leg_conflict = legs_map[2][0]  # 1:30 PM
            driver = drv(1)
            tasks["conflict_david"] = OperationalTask.objects.create(
                task_type=T.DRIVER_CONFLICT, status=S.PENDING, priority=P.CRITICAL,
                title=f"Driver Conflict — {driver}",
                description="1:30 PM and 2:45 PM legs conflict — driver will be 25 min late. Reassign or adjust times.",
                leg=leg_trigger, reservation=leg_trigger.reservation,
                due_at=now, escalate_at=now,
                metadata=_meta(
                    driver_id=driver.id, driver_name=str(driver),
                    flight_ident="",
                    mismatch_direction="overlap",
                    mismatch_minutes=25, mismatch_label="25 min late",
                    conflict_minutes=25,
                    conflicting_leg_id=leg_conflict.id,
                    pickup_date=str(today),
                    pickup_time=str(leg_trigger.pickup_time),
                ),
            )

        # Conflict 3: driver[2] — legs 4 and 5 overlap
        if legs_map.get(4) and legs_map.get(5):
            leg_trigger = legs_map[5][0]  # 4:15 PM
            leg_conflict = legs_map[4][0]  # 3:00 PM
            driver = drv(2)
            tasks["conflict_rayyan"] = OperationalTask.objects.create(
                task_type=T.DRIVER_CONFLICT, status=S.PENDING, priority=P.CRITICAL,
                title=f"Driver Conflict — {driver}",
                description="3:00 PM and 4:15 PM legs conflict — driver will be 15 min late. Reassign or adjust times.",
                leg=leg_trigger, reservation=leg_trigger.reservation,
                due_at=now, escalate_at=now,
                metadata=_meta(
                    driver_id=driver.id, driver_name=str(driver),
                    flight_ident="United Airlines 1410",
                    mismatch_direction="overlap",
                    mismatch_minutes=15, mismatch_label="15 min late",
                    conflict_minutes=15,
                    conflicting_leg_id=leg_conflict.id,
                    pickup_date=str(today),
                    pickup_time=str(leg_trigger.pickup_time),
                ),
            )

        # ── driver_assign (9) — no driver legs from today ──

        NO_DRIVER_INDICES = [6, 7, 8, 9, 10, 11, 12, 13, 14]
        for idx in NO_DRIVER_INDICES:
            if idx not in legs_map or not legs_map[idx]:
                continue
            leg = legs_map[idx][0]
            customer = leg.reservation.customer if leg.reservation else None
            cname = customer.get_full_name() if customer else "Unknown"
            ptime = leg.pickup_time.strftime("%I:%M %p").lstrip("0")
            tasks[f"driver_{idx}"] = OperationalTask.objects.create(
                task_type=T.DRIVER_ASSIGNMENT, status=S.PENDING, priority=P.CRITICAL,
                title=f"No driver: {cname} — {today:%b %d} {ptime}",
                description=f"{leg.pickup_location} → {leg.dropoff_location}",
                leg=leg, reservation=leg.reservation,
                due_at=now, escalate_at=now,
                metadata=_meta(pickup_date=str(today), pickup_time=str(leg.pickup_time)),
            )

        # ── flight_verify (5) — flight mismatches ──

        FLIGHT_VERIFY = [
            (15, "Matthew Carty", "Delta Airlines 0328", 33, "late", "moderate"),
            (16, "Jeff Phillips", "American Airlines 1709", 366, "early", "major"),
            (17, "Donald Gillon", "United Airlines 496", 0, "cancelled", "major"),
            (18, "Roberta Delaney", "JetBlue Airways 0351", 1295, "early", "major"),
            (19, "Cassie Rector", "American Airlines 2887", 52, "late", "moderate"),
        ]

        for res_idx, cname, flight_ident, mins, direction, severity in FLIGHT_VERIFY:
            if res_idx not in legs_map or not legs_map[res_idx]:
                continue
            leg = legs_map[res_idx][0]

            if direction == "cancelled":
                label = "Cancelled"
            else:
                hrs = mins // 60
                rmins = mins % 60
                if hrs > 0:
                    label = f"Coming {hrs} hr {rmins} min {direction}"
                else:
                    label = f"Coming {rmins} min {direction}"

            tasks[f"flight_{res_idx}"] = OperationalTask.objects.create(
                task_type=T.FLIGHT_VERIFICATION, status=S.PENDING, priority=P.HIGH,
                title=f"Flight mismatch: {cname} — {flight_ident} {label}",
                description=f"Flight is {label.lower()}. Verify pickup time.",
                leg=leg, reservation=leg.reservation,
                due_at=now, escalate_at=now + timedelta(hours=4),
                metadata=_meta(
                    mismatch_minutes=mins, mismatch_direction=direction,
                    mismatch_label=label, severity_tier=severity,
                    flight_ident=flight_ident,
                    pickup_date=str(leg.pickup_date),
                    pickup_time=str(leg.pickup_time),
                ),
            )

        # ── payment_chase (7) — unpaid reservations ──

        PAYMENT_TASKS = [
            (20, "Amy Hill", Decimal("343.75"), day_after, P.HIGH),
            (21, "Jeff Phillips", Decimal("295.00"), tomorrow, P.HIGH),
            (22, "Amy Rice", Decimal("105.00"), tomorrow, P.HIGH),
            (23, "Kimberly Rowan", Decimal("215.00"), day_after, P.HIGH),
            (24, "Kimberly Rowan", Decimal("195.00"), day_after, P.HIGH),
            (25, "Mark Pells", Decimal("175.00"), day_after, P.HIGH),
            (26, "Mariya Oncioiu", Decimal("195.00"), tomorrow, P.HIGH),
        ]

        for res_idx, cname, amount, trip_date, priority in PAYMENT_TASKS:
            if res_idx >= len(reservations):
                continue
            res = reservations[res_idx]
            tasks[f"payment_{res_idx}"] = OperationalTask.objects.create(
                task_type=T.PAYMENT_CHASE, status=S.PENDING, priority=priority,
                title=f"Unpaid ${amount}: {cname} — trip {trip_date:%b %d}",
                description=f"No payment on file.",
                reservation=res, due_at=now,
                escalate_at=now + timedelta(hours=24),
                metadata=_meta(
                    amount_owed=str(amount), total_price=str(amount),
                    days_until_pickup=(trip_date - today).days,
                ),
            )

        # ── contact_form (4) ──

        for i, form in enumerate(contact_forms):
            tasks[f"contact_{i}"] = OperationalTask.objects.create(
                task_type=T.CONTACT_FORM, status=S.PENDING, priority=P.HIGH,
                title=f"Contact form: {form.first_name} {form.last_name}",
                description=form.about[:100] if form.about else "",
                contact_form=form,
                due_at=now, escalate_at=now + timedelta(hours=4),
                metadata=_meta(
                    email=form.email, phone=form.phone_number or "",
                    contact_method=form.contact_method or "",
                ),
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
        attempts = []

        if "conflict_neuma" in tasks:
            attempts.append(CommunicationAttempt(
                task=tasks["conflict_neuma"],
                channel="call", outcome="no_answer", staff_user=sarah,
                contact_value="407-624-7385",
                notes="No answer. Will reassign if no response in 30 min.",
                duration_seconds=0,
            ))

        if "manual_in_progress" in tasks:
            attempts.append(CommunicationAttempt(
                task=tasks["manual_in_progress"],
                channel="call", outcome="voicemail", staff_user=mike,
                contact_value="407-555-0000",
                notes="Disney concierge office voicemail. Left callback number.",
                duration_seconds=30,
            ))

        if attempts:
            CommunicationAttempt.objects.bulk_create(attempts)
        self.stdout.write(f"  Created {len(attempts)} communication attempts")

    # ------------------------------------------------------------------
    # Staff Activities
    # ------------------------------------------------------------------

    def _create_staff_activities(self, tasks, sarah, mike):
        activities = [
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
