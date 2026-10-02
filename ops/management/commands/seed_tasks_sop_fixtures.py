"""Seed the disposable SOP capture database with one Ops Control task of every type.

Run against business.settings_sop ONLY:

    python manage.py migrate --settings=business.settings_sop
    python manage.py loaddata rates_data.json --settings=business.settings_sop
    python manage.py seed_tasks_sop_fixtures --settings=business.settings_sop

Builds on seed_sop_fixtures when that has run (its dispatcher, jdoe, is reused
as-is) and creates what it needs when it hasn't.

WHY EVERY TASK IS FILED THE REAL WAY
The tasks guide photographs each task page, and each page is built from what
the filer wrote into the task: the conflict's legs and lateness, the unpaid
amount, the flight shift. Hand-written rows would photograph a page no
dispatcher ever sees. So each task is filed by the function the live system
uses: the 30-minute scanners, flag_flight_not_found, _handle_flight_disruption,
flag_afterhours_fee. The overnight-date task is the one exception — its filer
asks AeroAPI first — and it is written with the sweep's own title and wording.

Today's trips are placed a few hours after the moment you run this, so run it
during the day (before about 8 PM Eastern) and capture straight afterwards.

WHAT EXISTS AFTERWARDS (one of each, plus a lane's worth of ownership)
  * Driver Conflict — Kevin Brooks can't get from a hotel run to an MCO arrival.
  * Driver Assignment — a trip this evening with nobody on it.
  * Unpaid Reservations — claimed by jdoe, with a voicemail already logged.
  * Flight Verification: a flight that moved, one that can't be found, one that
    was cancelled, and an overnight pickup whose date nobody has confirmed.
  * Confirmation Texts — tomorrow's batch.
  * Contact Us — a family asking for a quote, claimed by Maria Reyes.
  * After-Hours Fee — a pickup at 11:30 PM.
  * Manual Task — a call-back jdoe set for herself, and one finished earlier.

Every person is invented: @example.com addresses and 555-02xx phones.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from drivers.models import Driver, DriverVehicleAssignment, FleetVehicle
from ops import tasks as scanners
from ops.models import OperationalTask
from ops.services import close_task, create_task, log_communication
from rates.models import Rate
from reservations.models import Customer, Flight, Leg, Reservation
from users.models import ContactUsForm, UserProfile

DISPATCHER = ("jdoe", "Jordan", "Doe", "555-0201")
TEAMMATE = ("mreyes", "Maria", "Reyes", "555-0202")
CHAUFFEURS = [
    ("kbrooks", "Kevin", "Brooks", "555-0211"),
    ("aflores", "Ana", "Flores", "555-0212"),
    ("cwright", "Calvin", "Wright", "555-0213"),
]
GUESTS = {
    "conflict_prior": ("Margaret", "Ellison", "555-0221"),
    "conflict": ("Samuel", "Osei", "555-0222"),
    "no_driver": ("Claire", "Donnelly", "555-0223"),
    "unpaid": ("Victor", "Almeida", "555-0224"),
    "contact": ("Naomi", "Fischer", "555-0225"),
    "flight_moved": ("Arjun", "Mehta", "555-0226"),
    "flight_missing": ("Lucy", "Harrington", "555-0227"),
    "flight_cancelled": ("Diego", "Navarro", "555-0228"),
    "overnight": ("Fiona", "Gallagher", "555-0229"),
    "late_night": ("Henry", "Caldwell", "555-0230"),
    "tomorrow": ("Isabel", "Moreau", "555-0231"),
    "callback": ("Thomas", "Whitaker", "555-0232"),
}

# A partner operator, so the conflict page's farm-out step has someone to offer.
PARTNER = ("coastline", "Coastline", "Car Service", "555-0241")

MCO = "Orlando International Airport (MCO)"


class Command(BaseCommand):
    help = "Seed one Ops Control task of every type into the SOP capture database."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Delete the seeded tasks and trips first and rebuild them.")

    def handle(self, *args, **options):
        name = str(settings.DATABASES["default"]["NAME"])
        if "db_sop_capture" not in name:
            raise CommandError(
                "Refusing to run: this writes fixtures, and the database is not the "
                f"disposable capture one (got {name}). Add --settings=business.settings_sop."
            )
        self.rate = Rate.objects.first()
        if self.rate is None:
            raise CommandError("No rates yet — run loaddata rates_data.json first.")

        with transaction.atomic():
            if options["force"]:
                self._clear()
            self._seed()

        counts = ", ".join(
            f"{label}: {OperationalTask.objects.filter(task_type=value).count()}"
            for value, label in OperationalTask.TaskType.choices
        )
        self.stdout.write(self.style.SUCCESS(f"Seeded tasks. {counts}. Capture as jdoe."))

    # ── teardown ────────────────────────────────────────────────────────────
    def _clear(self):
        OperationalTask.objects.all().delete()
        ContactUsForm.objects.filter(phone_number__startswith="555-02").delete()
        DriverVehicleAssignment.objects.filter(vehicle__vehicle_number__startswith="S2").delete()
        FleetVehicle.objects.filter(vehicle_number__startswith="S2").delete()
        legs = Leg.objects.filter(reservation__customer__phone_number__startswith="555-02")
        Flight.objects.filter(leg__in=legs).delete()
        legs.delete()
        Reservation.objects.filter(customer__phone_number__startswith="555-02").delete()
        Customer.objects.filter(phone_number__startswith="555-02").delete()

    # ── people ──────────────────────────────────────────────────────────────
    def _staff(self, username, first, last, phone):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"first_name": first, "last_name": last,
                      "email": f"{first.lower()}.{last.lower()}@example.com",
                      "is_staff": True, "is_superuser": False, "is_active": True},
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        UserProfile.objects.get_or_create(user=user, defaults={"phone_number": phone})
        return user

    def _chauffeur(self, username, first, last, phone):
        user, _ = User.objects.get_or_create(
            username=username,
            defaults={"first_name": first, "last_name": last,
                      "email": f"{first.lower()}.{last.lower()}@example.com"},
        )
        UserProfile.objects.get_or_create(user=user, defaults={"phone_number": phone})
        driver, _ = Driver.objects.update_or_create(
            profile=user, defaults={"driver_type": "inhouse", "is_active": True})
        return driver

    def _guest(self, key):
        first, last, phone = GUESTS[key]
        guest, _ = Customer.objects.update_or_create(
            phone_number=phone,
            defaults={"first_name": first, "last_name": last,
                      "email": f"{first.lower()}.{last.lower()}@example.com"},
        )
        return guest

    # ── trips ───────────────────────────────────────────────────────────────
    def _trip(self, key, when, pickup, dropoff, driver=None, flight=None, booked_days_ago=0):
        # A booking saved without a price is $0, and a $0 booking owes nothing, so
        # the unpaid scanner would never look at it.
        reservation = Reservation.objects.create(
            trip_type="one-way", customer=self._guest(key), rate=self.rate, status="confirmed",
            base_price=Decimal("165.00"), gratuity_amount=Decimal("20.00"),
            total_price=Decimal("185.00"))
        if booked_days_ago:
            Reservation.objects.filter(pk=reservation.pk).update(
                created_at=timezone.now() - timedelta(days=booked_days_ago))
        leg = Leg.objects.create(
            reservation=reservation, driver=driver, flight_information=flight,
            pickup_date=when.date(), pickup_time=when.time().replace(second=0, microsecond=0),
            pickup_location=pickup, dropoff_location=dropoff, status="confirmed",
        )
        return leg

    def _flight(self, airline, number, origin, lands, status="Scheduled"):
        return Flight.objects.create(
            flight_type="arrival", airline=airline, flight_number=number,
            origin=origin, destination="MCO", status=status,
            departure_date=lands.date() if lands else None,
            scheduled_arrival_local=lands, scheduled_gate_arrival_local=lands,
            terminal="B" if lands else "",
        )

    # ── the seed ────────────────────────────────────────────────────────────
    def _seed(self):
        tz = timezone.get_current_timezone()
        now = timezone.localtime()
        base = now.replace(second=0, microsecond=0)
        base -= timedelta(minutes=base.minute % 5)
        today = now.date()

        def at(day_offset, hour, minute):
            return timezone.make_aware(
                datetime.combine(today + timedelta(days=day_offset), time(hour, minute)), tz)

        jdoe = self._staff(*DISPATCHER)
        maria = self._staff(*TEAMMATE)
        kevin, ana, calvin = (self._chauffeur(*c) for c in CHAUFFEURS)
        self._roster(today, kevin, ana, calvin)
        partner = self._chauffeur(*PARTNER)
        partner.driver_type = "affiliate"
        partner.save(update_fields=["driver_type"])

        # Driver Conflict: Kevin drops at MCO after a hotel run, and the arrival he
        # is booked on next lands before he can be inside. Inside two hours, so the
        # scanner files it on first sight.
        self._trip("conflict_prior", base + timedelta(minutes=20),
                   "Disney's Contemporary Resort", MCO, driver=kevin)
        lands = base + timedelta(minutes=30)
        self._trip("conflict", lands, f"{MCO}, Terminal B", "Universal's Portofino Bay Hotel",
                   driver=kevin, flight=self._flight("DL", "1342", "ATL", lands))
        scanners._scan_driver_overlaps()

        # Driver Assignment: a trip this evening with nobody on it.
        self._trip("no_driver", base + timedelta(hours=2, minutes=45),
                   "Hyatt Regency Grand Cypress", MCO)
        scanners._scan_unassigned_legs()

        # Unpaid Reservations: booked two days ago, trip in two days, nothing paid.
        self._trip("unpaid", at(2, 10, 15), "Disney's Grand Floridian Resort", MCO,
                   driver=ana, booked_days_ago=2)
        scanners._scan_unpaid_reservations()

        # Flight Verification, four ways.
        moved_booked = at(2, 14, 30)
        self._trip("flight_moved", moved_booked, f"{MCO}, Terminal A",
                   "Disney's Yacht Club Resort",
                   flight=self._flight("WN", "2218", "BWI", moved_booked + timedelta(minutes=75)))
        scanners._scan_flight_mismatches()

        missing = self._trip("flight_missing", at(3, 11, 0), f"{MCO}, Terminal C",
                             "Disney's Riviera Resort",
                             flight=self._flight("B6", "9917", "", None, status=""))
        scanners.flag_flight_not_found(missing)

        cancelled_at = at(1, 16, 40)
        cancelled_flight = self._flight("AA", "1577", "ORD", cancelled_at, status="Cancelled")
        cancelled = self._trip("flight_cancelled", cancelled_at, f"{MCO}, Terminal B",
                               "Universal's Loews Royal Pacific Resort", driver=calvin,
                               flight=cancelled_flight)
        scanners._handle_flight_disruption(cancelled, cancelled_flight)

        overnight_at = at(4, 0, 40)
        overnight = self._trip("overnight", overnight_at, f"{MCO}, Terminal B",
                               "Disney's Coronado Springs Resort",
                               flight=self._flight("F9", "1021", "PHL", overnight_at))
        self._overnight_task(overnight)

        # After-Hours Fee: a late departure, the $20 not in the price, and a card on
        # file so the page offers the one-click charge.
        late = self._trip("late_night", at(2, 23, 30), "Universal's Portofino Bay Hotel", MCO,
                          driver=kevin)
        Customer.objects.filter(pk=late.reservation.customer_id).update(
            stripe_customer_id="cus_SOPFIXTURE0230", card_brand="Visa", card_last4="4242",
            card_exp_month=8, card_exp_year=today.year + 3)
        scanners.flag_afterhours_fee(late, late.pickup_time)

        # Confirmation Texts: tomorrow's trips have not had theirs yet.
        self._trip("tomorrow", at(1, 9, 20), "Disney's Polynesian Village Resort", MCO, driver=ana)
        scanners._scan_confirmation_texts()

        # Contact Us: a real-sounding enquiry from a couple of hours ago.
        first, last, phone = GUESTS["contact"]
        form = ContactUsForm.objects.create(
            first_name=first, last_name=last, phone_number=phone,
            email=f"{first.lower()}.{last.lower()}@example.com", contact_method="phone",
            about=("Hi, we are a family of five landing at MCO on the 18th and need a ride "
                   "to our Disney resort with two car seats. Could you send us a quote? "
                   "Thank you, Naomi"),
        )
        ContactUsForm.objects.filter(pk=form.pk).update(created_at=now - timedelta(hours=2))
        scanners._scan_uncontacted_forms()

        # Manual Task: a call-back jdoe set for herself, and one finished earlier.
        callback_leg = self._trip("callback", at(3, 8, 45), "Disney's Beach Club Resort", MCO,
                                  driver=calvin)
        create_task(
            task_type=OperationalTask.TaskType.MANUAL,
            title=(f"Call back {GUESTS['callback'][1]} about the car seat, "
                   f"R{callback_leg.reservation_id}"),
            description="Asked for a forward-facing seat for a 3-year-old. Confirm it's on the "
                        "trip and the driver knows.",
            due_at=at(0, 17, 0) if now.hour < 17 else at(1, 17, 0),
            priority=OperationalTask.Priority.MEDIUM,
            created_by=jdoe, assigned_to=jdoe,
        )
        done = create_task(
            task_type=OperationalTask.TaskType.MANUAL,
            title="Send Ellison the updated pickup time",
            due_at=now - timedelta(hours=1), created_by=jdoe, assigned_to=jdoe,
        )
        close_task(done, resolved_by=jdoe, resolution_notes="Texted the new time; guest replied OK.")

        self._ownership(jdoe, maria, now)

    def _roster(self, today, *drivers):
        """A car each for today. The conflict page only offers drivers who are on
        today's roster, and the roster is the day's vehicle assignments."""
        vehicle_type = self.rate.vehicle
        for n, driver in enumerate(drivers, start=1):
            unit, _ = FleetVehicle.objects.update_or_create(
                vehicle_number=f"S2{n}",
                defaults={"vehicle_type": vehicle_type, "year": 2024, "make": "Chevrolet",
                          "model": "Suburban", "license_plate": f"SOP 02{n}",
                          "vin": f"1GTS2{n}SOPFIXTURE", "is_active": True},
            )
            DriverVehicleAssignment.objects.update_or_create(
                driver=driver, date=today, defaults={"vehicle": unit})

    def _overnight_task(self, leg):
        """The sweep's "no email" branch, word for word (dispatching/overnight_arrival.py)."""
        from dispatching.overnight_arrival import fmt_date, fmt_time
        prev_day = leg.pickup_date - timedelta(days=1)
        create_task(
            task_type=OperationalTask.TaskType.FLIGHT_VERIFICATION,
            title=(f"Call to confirm overnight date: "
                   f"{fmt_date(leg.pickup_date)} {fmt_time(leg.pickup_time)}")[:200],
            description=(
                f"Overnight arrival {fmt_time(leg.pickup_time)} on "
                f"{fmt_date(leg.pickup_date)} ({leg.flight_information.get_flight_ident() or 'no ident'}). "
                f"Guest has no email for the one-tap confirmation — call and ask "
                f"which date they TAKE OFF: {fmt_date(prev_day)} (pickup correct) "
                f"or {fmt_date(leg.pickup_date)} (pickup moves to "
                f"{fmt_date(leg.pickup_date + timedelta(days=1))})."
            ),
            priority=OperationalTask.Priority.MEDIUM,
            due_at=timezone.now(),
            leg=leg,
            reservation=leg.reservation,
            metadata={"source": "overnight_sweep", "scenario": "ambiguous"},
        )

    def _ownership(self, jdoe, maria, now):
        """A queue mid-shift: some work claimed, some parked, the rest waiting."""
        T = OperationalTask.TaskType
        unpaid = OperationalTask.objects.get(task_type=T.PAYMENT_CHASE)
        unpaid.assigned_to, unpaid.assigned_at = jdoe, now - timedelta(minutes=50)
        unpaid.status = OperationalTask.Status.IN_PROGRESS
        unpaid.save(update_fields=["assigned_to", "assigned_at", "status"])
        log_communication(unpaid, channel="call", outcome="voicemail", user=jdoe,
                          contact_value=unpaid.reservation.customer.phone_number,
                          notes="Left a voicemail asking them to call back to confirm.")

        contact = OperationalTask.objects.get(task_type=T.CONTACT_FORM)
        contact.assigned_to, contact.assigned_at = maria, now - timedelta(minutes=20)
        contact.status = OperationalTask.Status.IN_PROGRESS
        contact.save(update_fields=["assigned_to", "assigned_at", "status"])

        overnight = OperationalTask.objects.get(title__startswith="Call to confirm overnight")
        overnight.status = OperationalTask.Status.SNOOZED
        overnight.snoozed_until = now + timedelta(hours=4)
        overnight.save(update_fields=["status", "snoozed_until"])
