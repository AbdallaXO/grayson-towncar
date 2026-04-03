"""
Seed realistic staff metrics data for a full day of work.
Creates activity across all models that feed into Staff Metrics:
- StaffActivity (page views, task actions)
- OperationalTask (tasks created, claimed, completed, snoozed)
- CommunicationAttempt (calls, emails, SMS)
- EmailLog (confirmation, payment reminder, driver statement emails)
- AuditLog (driver assignments, status changes, payment actions)
- Reservation/Leg history changes (via django-simple-history)

Usage:
    python manage.py seed_staff_metrics
    python manage.py seed_staff_metrics --clear   # clear seed data first
"""
import random
from datetime import timedelta, time as dt_time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()


class Command(BaseCommand):
    help = "Seed a realistic day of staff activity for Staff Metrics testing"

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true", help="Clear existing seed data first")

    def handle(self, *args, **options):
        from ops.models import (
            OperationalTask, CommunicationAttempt, StaffActivity, EmailLog,
        )
        from reservations.models import AuditLog, Reservation, Leg
        from drivers.models import Driver

        if options["clear"]:
            self.stdout.write("Clearing seed data...")
            # Only clear data with seed marker in metadata
            StaffActivity.objects.filter(metadata__seed=True).delete()
            EmailLog.objects.filter(metadata__seed=True).delete()
            CommunicationAttempt.objects.filter(metadata__seed=True).delete()
            OperationalTask.objects.filter(metadata__seed=True).delete()
            AuditLog.objects.filter(notes__contains="[SEED]").delete()
            self.stdout.write(self.style.SUCCESS("Seed data cleared."))

        # Get staff users
        staff_users = list(User.objects.filter(is_staff=True, is_active=True))
        if len(staff_users) < 2:
            self.stdout.write(self.style.ERROR("Need at least 2 staff users. Aborting."))
            return

        drivers = list(Driver.objects.all()[:8])
        reservations = list(
            Reservation.objects.order_by("-created_at")
            .select_related("customer")[:20]
        )
        legs = list(Leg.objects.order_by("-id")[:20])

        if not reservations:
            self.stdout.write(self.style.ERROR("No reservations found. Aborting."))
            return

        now = timezone.now()
        today_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)

        # ─── Define each staff member's simulated day ───
        # Staff[0] = "abdi" (owner, heavy user)
        # Staff[1] = "Sarah" (dispatcher, moderate)
        # Staff[2] = "Mike" (dispatcher, lighter)

        profiles = []
        for i, user in enumerate(staff_users):
            if i == 0:
                profiles.append({
                    "user": user,
                    "start_offset": 0,       # starts at 8:00 AM
                    "end_offset": 660,        # works until ~7:00 PM
                    "page_views": 25,
                    "tasks_to_create": 3,
                    "tasks_to_claim": 5,
                    "tasks_to_complete": 7,
                    "tasks_to_snooze": 1,
                    "comms_calls": 4,
                    "comms_emails": 3,
                    "comms_sms": 2,
                    "emails_confirmation": 5,
                    "emails_payment_reminder": 3,
                    "emails_driver_statement": 1,
                    "driver_assigns": 8,
                    "status_changes": 6,
                    "payment_actions": 2,
                    "field_changes": 10,
                })
            elif i == 1:
                profiles.append({
                    "user": user,
                    "start_offset": 30,       # starts at 8:30 AM
                    "end_offset": 540,         # works until ~5:00 PM
                    "page_views": 18,
                    "tasks_to_create": 2,
                    "tasks_to_claim": 3,
                    "tasks_to_complete": 4,
                    "tasks_to_snooze": 2,
                    "comms_calls": 6,
                    "comms_emails": 2,
                    "comms_sms": 3,
                    "emails_confirmation": 3,
                    "emails_payment_reminder": 2,
                    "emails_driver_statement": 0,
                    "driver_assigns": 5,
                    "status_changes": 4,
                    "payment_actions": 1,
                    "field_changes": 6,
                })
            else:
                profiles.append({
                    "user": user,
                    "start_offset": 60,        # starts at 9:00 AM
                    "end_offset": 480,          # works until ~4:00 PM
                    "page_views": 10,
                    "tasks_to_create": 1,
                    "tasks_to_claim": 2,
                    "tasks_to_complete": 2,
                    "tasks_to_snooze": 1,
                    "comms_calls": 2,
                    "comms_emails": 1,
                    "comms_sms": 1,
                    "emails_confirmation": 2,
                    "emails_payment_reminder": 1,
                    "emails_driver_statement": 1,
                    "driver_assigns": 3,
                    "status_changes": 2,
                    "payment_actions": 0,
                    "field_changes": 4,
                })

        dispatching_pages = [
            "/dispatching/",
            "/dispatching/dashboard/",
            "/dispatching/reservations/",
            "/dispatching/legs/",
            "/dispatching/planner/",
            "/dispatching/board/",
            "/dispatching/confirmations/",
            "/dispatching/drivers/",
        ]

        task_types = ["payment_chase", "flight_verify", "driver_conflict", "driver_assign", "contact_form", "manual"]
        call_outcomes = ["answered", "voicemail", "no_answer", "busy"]
        email_outcomes = ["sent", "delivered"]
        change_fields = [
            ("pickup_time", "10:00:00", "09:30:00"),
            ("pickup_time", "14:00:00", "13:45:00"),
            ("pickup_date", "2026-04-05", "2026-04-06"),
            ("pickup_location", "MCO Airport", "Orlando International Airport Terminal B"),
            ("dropoff_location", "Disney's Grand Floridian", "Disney's Contemporary Resort"),
            ("total_price", "175.00", "200.00"),
            ("base_price", "150.00", "175.00"),
            ("gratuity_amount", "25.00", "30.00"),
            ("private_notes", "Guest has 2 car seats", "Guest has 2 car seats. Needs SUV."),
            ("passenger_count", "2", "4"),
            ("status", "pending", "confirmed"),
            ("status", "confirmed", "completed"),
        ]

        seed_meta = {"seed": True}
        total_created = {
            "StaffActivity": 0,
            "OperationalTask": 0,
            "CommunicationAttempt": 0,
            "EmailLog": 0,
            "AuditLog": 0,
        }

        for profile in profiles:
            user = profile["user"]
            start = today_8am + timedelta(minutes=profile["start_offset"])
            end = today_8am + timedelta(minutes=profile["end_offset"])
            work_minutes = profile["end_offset"] - profile["start_offset"]

            def rand_time():
                """Random timestamp during this staff member's work day."""
                offset = random.randint(0, work_minutes)
                return start + timedelta(minutes=offset, seconds=random.randint(0, 59))

            self.stdout.write(f"\n--- Seeding data for {user.first_name or user.username} ---")

            # ── Page Views ──
            for _ in range(profile["page_views"]):
                ts = rand_time()
                StaffActivity.objects.create(
                    user=user,
                    action_type=StaffActivity.ActionType.PAGE_VIEW,
                    path=random.choice(dispatching_pages),
                    created_at=ts,
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    metadata=seed_meta,
                )
                total_created["StaffActivity"] += 1

            # ── Operational Tasks (create, claim, complete, snooze) ──
            created_tasks = []

            # Create tasks
            for _ in range(profile["tasks_to_create"]):
                ts = rand_time()
                res = random.choice(reservations) if reservations else None
                leg = random.choice(legs) if legs else None
                task_type = random.choice(task_types)
                titles = {
                    "payment_chase": f"Unpaid ${random.randint(50, 300):.2f}: {res.customer if res else 'Customer'} — trip {random.choice(['Mar', 'Apr'])} {random.randint(1, 30)}",
                    "flight_verify": f"Flight mismatch: {random.choice(['Delta', 'American', 'United', 'JetBlue'])} Airlines {random.randint(100, 9999)} Coming {random.randint(1, 60)} ...",
                    "driver_conflict": f"Driver Conflict — {random.choice(drivers) if drivers else 'Driver'}",
                    "driver_assign": f"No driver: {res.customer if res else 'Customer'} — ...",
                    "contact_form": f"New contact: {random.choice(['John', 'Maria', 'James', 'Lisa'])} {random.choice(['Smith', 'Johnson', 'Williams', 'Brown'])}",
                    "manual": f"Follow up: {random.choice(['Check availability', 'Verify address', 'Confirm pricing', 'Update notes'])}",
                }
                task = OperationalTask.objects.create(
                    task_type=task_type,
                    title=titles[task_type],
                    description=f"Auto-generated seed task for metrics testing",
                    status="pending",
                    priority=random.choice([1, 2, 2, 3, 3, 3, 4]),
                    reservation=res,
                    leg=leg,
                    created_by=user,
                    due_at=ts + timedelta(hours=random.randint(1, 48)),
                    metadata=seed_meta,
                )
                task.created_at = ts
                task.save(update_fields=["created_at"])
                created_tasks.append(task)
                total_created["OperationalTask"] += 1

                # Log task_created activity
                StaffActivity.objects.create(
                    user=user,
                    action_type=StaffActivity.ActionType.TASK_CREATED,
                    task=task,
                    created_at=ts + timedelta(seconds=5),
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    metadata=seed_meta,
                )
                total_created["StaffActivity"] += 1

            # Claim some existing open tasks
            open_tasks = list(
                OperationalTask.objects.filter(
                    status="pending",
                    assigned_to__isnull=True,
                ).exclude(metadata__seed=True)[:profile["tasks_to_claim"]]
            )
            # Also include seed tasks from OTHER users
            other_seed = list(
                OperationalTask.objects.filter(
                    status="pending",
                    metadata__seed=True,
                ).exclude(created_by=user)[:max(0, profile["tasks_to_claim"] - len(open_tasks))]
            )
            claimable = (open_tasks + other_seed)[:profile["tasks_to_claim"]]

            for task in claimable:
                ts = rand_time()
                task.assigned_to = user
                task.status = "in_progress"
                task.save(update_fields=["assigned_to", "status"])

                StaffActivity.objects.create(
                    user=user,
                    action_type=StaffActivity.ActionType.TASK_CLAIMED,
                    task=task,
                    created_at=ts,
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    metadata=seed_meta,
                )
                total_created["StaffActivity"] += 1

            # Complete tasks
            completable = list(
                OperationalTask.objects.filter(
                    status__in=["pending", "in_progress"],
                    assigned_to=user,
                )[:profile["tasks_to_complete"]]
            )
            # Also complete some unassigned ones
            if len(completable) < profile["tasks_to_complete"]:
                extras = list(
                    OperationalTask.objects.filter(
                        status__in=["pending", "in_progress"],
                    ).exclude(assigned_to=user)[:profile["tasks_to_complete"] - len(completable)]
                )
                completable.extend(extras)

            for task in completable[:profile["tasks_to_complete"]]:
                ts = rand_time()
                task.status = "completed"
                task.resolved_by = user
                task.resolved_at = ts
                task.attempts = random.randint(1, 4)
                task.save(update_fields=["status", "resolved_by", "resolved_at", "attempts"])

                StaffActivity.objects.create(
                    user=user,
                    action_type=StaffActivity.ActionType.TASK_COMPLETED,
                    task=task,
                    created_at=ts,
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    metadata=seed_meta,
                )
                total_created["StaffActivity"] += 1

            # Snooze tasks
            snoozable = list(
                OperationalTask.objects.filter(
                    status__in=["pending", "in_progress"],
                    assigned_to=user,
                )[:profile["tasks_to_snooze"]]
            )
            for task in snoozable:
                ts = rand_time()
                task.status = "snoozed"
                task.snoozed_until = ts + timedelta(hours=random.randint(2, 24))
                task.save(update_fields=["status", "snoozed_until"])

                StaffActivity.objects.create(
                    user=user,
                    action_type=StaffActivity.ActionType.TASK_SNOOZED,
                    task=task,
                    created_at=ts,
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    metadata=seed_meta,
                )
                total_created["StaffActivity"] += 1

            # ── Communication Attempts ──
            open_tasks_for_comms = list(
                OperationalTask.objects.filter(
                    status__in=["pending", "in_progress", "completed"],
                )[:20]
            )

            for _ in range(profile["comms_calls"]):
                ts = rand_time()
                task = random.choice(open_tasks_for_comms) if open_tasks_for_comms else None
                CommunicationAttempt.objects.create(
                    task=task,
                    channel="call",
                    outcome=random.choice(call_outcomes),
                    staff_user=user,
                    contact_value=f"407-555-{random.randint(1000, 9999)}",
                    duration_seconds=random.randint(15, 300) if random.random() > 0.3 else 0,
                    notes=random.choice([
                        "Left voicemail about pickup time",
                        "Confirmed reservation details",
                        "No answer, will try again",
                        "Discussed pricing and availability",
                        "Customer requested time change",
                    ]),
                    created_at=ts,
                    metadata=seed_meta,
                )
                total_created["CommunicationAttempt"] += 1

                StaffActivity.objects.create(
                    user=user,
                    action_type=StaffActivity.ActionType.COMM_LOGGED,
                    task=task,
                    created_at=ts + timedelta(seconds=10),
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    metadata=seed_meta,
                )
                total_created["StaffActivity"] += 1

            for _ in range(profile["comms_emails"]):
                ts = rand_time()
                task = random.choice(open_tasks_for_comms) if open_tasks_for_comms else None
                CommunicationAttempt.objects.create(
                    task=task,
                    channel="email",
                    outcome=random.choice(email_outcomes),
                    staff_user=user,
                    contact_value=f"{random.choice(['john', 'jane', 'bob', 'mary'])}.{random.choice(['smith', 'doe', 'jones'])}@gmail.com",
                    notes=random.choice([
                        "Sent payment reminder",
                        "Sent confirmation email",
                        "Follow-up on booking inquiry",
                    ]),
                    created_at=ts,
                    metadata=seed_meta,
                )
                total_created["CommunicationAttempt"] += 1

                StaffActivity.objects.create(
                    user=user,
                    action_type=StaffActivity.ActionType.COMM_LOGGED,
                    task=task,
                    created_at=ts + timedelta(seconds=10),
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    metadata=seed_meta,
                )
                total_created["StaffActivity"] += 1

            for _ in range(profile["comms_sms"]):
                ts = rand_time()
                task = random.choice(open_tasks_for_comms) if open_tasks_for_comms else None
                CommunicationAttempt.objects.create(
                    task=task,
                    channel="sms",
                    outcome="sent",
                    staff_user=user,
                    contact_value=f"407-555-{random.randint(1000, 9999)}",
                    notes="Sent pickup reminder via SMS",
                    created_at=ts,
                    metadata=seed_meta,
                )
                total_created["CommunicationAttempt"] += 1

            # ── EmailLog ──
            for _ in range(profile["emails_confirmation"]):
                ts = rand_time()
                res = random.choice(reservations) if reservations else None
                EmailLog.objects.create(
                    email_type="confirmation",
                    sent_by=user,
                    recipient_email=f"{random.choice(['guest', 'traveler', 'customer'])}{random.randint(1, 99)}@gmail.com",
                    subject="Thank you for booking with Grayson Towncar!",
                    reservation=res,
                    sent_at=ts,
                    metadata=seed_meta,
                )
                total_created["EmailLog"] += 1

            for _ in range(profile["emails_payment_reminder"]):
                ts = rand_time()
                res = random.choice(reservations) if reservations else None
                EmailLog.objects.create(
                    email_type="payment_reminder",
                    sent_by=user,
                    recipient_email=f"customer{random.randint(1, 50)}@yahoo.com",
                    subject=f"Action Required: Finalize Your Grayson Towncar Reservation #{res.id if res else 0}",
                    reservation=res,
                    sent_at=ts,
                    metadata=seed_meta,
                )
                total_created["EmailLog"] += 1

            for _ in range(profile["emails_driver_statement"]):
                ts = rand_time()
                driver = random.choice(drivers) if drivers else None
                EmailLog.objects.create(
                    email_type="driver_statement",
                    sent_by=user,
                    recipient_email=f"driver{random.randint(1, 20)}@gmail.com",
                    subject=f"Grayson Towncar - Payment Statement Apr 01, 2026 - Apr 03, 2026",
                    sent_at=ts,
                    metadata={**seed_meta, "driver_id": driver.id if driver else None},
                )
                total_created["EmailLog"] += 1

            # ── AuditLog entries ──
            for _ in range(profile["driver_assigns"]):
                ts = rand_time()
                leg = random.choice(legs) if legs else None
                driver = random.choice(drivers) if drivers else None
                AuditLog.objects.create(
                    model_name="Leg",
                    object_id=leg.id if leg else random.randint(100, 999),
                    action="driver_assigned",
                    field_name="driver",
                    old_value="",
                    new_value=str(driver) if driver else "Driver",
                    user=user,
                    username=user.username,
                    timestamp=ts,
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    notes="[SEED] Driver assignment for metrics testing",
                )
                total_created["AuditLog"] += 1

            for _ in range(profile["status_changes"]):
                ts = rand_time()
                res = random.choice(reservations) if reservations else None
                old_status = random.choice(["pending", "confirmed"])
                new_status = "confirmed" if old_status == "pending" else "completed"
                AuditLog.objects.create(
                    model_name=random.choice(["Leg", "Reservation"]),
                    object_id=res.id if res else random.randint(100, 999),
                    action="status_changed",
                    field_name="status",
                    old_value=old_status,
                    new_value=new_status,
                    user=user,
                    username=user.username,
                    timestamp=ts,
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    notes="[SEED] Status change for metrics testing",
                )
                total_created["AuditLog"] += 1

            for _ in range(profile["payment_actions"]):
                ts = rand_time()
                res = random.choice(reservations) if reservations else None
                AuditLog.objects.create(
                    model_name="Reservation",
                    object_id=res.id if res else random.randint(100, 999),
                    action="payment_processed",
                    field_name="payment_status",
                    old_value="unpaid",
                    new_value="paid",
                    user=user,
                    username=user.username,
                    timestamp=ts,
                    ip_address="192.168.1." + str(random.randint(10, 50)),
                    notes="[SEED] Payment processed for metrics testing",
                )
                total_created["AuditLog"] += 1

            self.stdout.write(f"  {user.first_name or user.username}: done")

        # ── Create correction/override scenarios ──
        # Staff[1] changes something, then Staff[0] "corrects" it 30 min later
        if len(staff_users) >= 2 and legs:
            self.stdout.write("\n--- Seeding correction/override scenarios ---")
            correction_leg = legs[0]
            ts1 = today_8am + timedelta(hours=2)
            ts2 = ts1 + timedelta(minutes=30)

            # Staff[1] sets pickup time
            AuditLog.objects.create(
                model_name="Leg",
                object_id=correction_leg.id,
                action="updated",
                field_name="pickup_time",
                old_value="10:00:00",
                new_value="09:30:00",
                user=staff_users[1],
                username=staff_users[1].username,
                timestamp=ts1,
                notes="[SEED] Original change",
            )
            # Staff[0] overrides it
            AuditLog.objects.create(
                model_name="Leg",
                object_id=correction_leg.id,
                action="updated",
                field_name="pickup_time",
                old_value="09:30:00",
                new_value="10:15:00",
                user=staff_users[0],
                username=staff_users[0].username,
                timestamp=ts2,
                notes="[SEED] Correction by manager",
            )
            total_created["AuditLog"] += 2

            # Another correction: Staff[2] assigns wrong driver, Staff[0] fixes
            if len(staff_users) >= 3 and len(legs) > 1:
                corr_leg2 = legs[1]
                ts3 = today_8am + timedelta(hours=4)
                ts4 = ts3 + timedelta(minutes=15)

                AuditLog.objects.create(
                    model_name="Leg",
                    object_id=corr_leg2.id,
                    action="driver_assigned",
                    field_name="driver",
                    old_value="",
                    new_value=str(drivers[0]) if drivers else "Driver A",
                    user=staff_users[2],
                    username=staff_users[2].username,
                    timestamp=ts3,
                    notes="[SEED] Original assignment",
                )
                AuditLog.objects.create(
                    model_name="Leg",
                    object_id=corr_leg2.id,
                    action="driver_assigned",
                    field_name="driver",
                    old_value=str(drivers[0]) if drivers else "Driver A",
                    new_value=str(drivers[1]) if len(drivers) > 1 else "Driver B",
                    user=staff_users[0],
                    username=staff_users[0].username,
                    timestamp=ts4,
                    notes="[SEED] Correction - reassigned to correct driver",
                )
                total_created["AuditLog"] += 2

        # ── Summary ──
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(self.style.SUCCESS("Seed data created successfully!"))
        for model, count in total_created.items():
            self.stdout.write(f"  {model}: {count} records")
        self.stdout.write(f"\nView at: /dispatching/staff-metrics/")
        self.stdout.write(f"Clear with: python manage.py seed_staff_metrics --clear")
