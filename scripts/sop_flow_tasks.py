"""Capture Ops Control and every task page for the Tasks guide (SOP-005).

    python scripts/sop_flow_tasks.py

Taken as **jdoe**, a plain dispatcher: is_staff, not a superuser. That matters
on the Driver Conflict page, whose Recovery Advisor card is founders-only — a
capture taken as a founder would photograph a card the dispatcher never sees.

Prerequisites, once (the seeder files one task of every type, the real way):

    python manage.py migrate --settings=business.settings_sop
    python manage.py loaddata rates_data.json --settings=business.settings_sop
    python manage.py seed_tasks_sop_fixtures --settings=business.settings_sop

Today's trips are seeded a few hours ahead of the clock, so seed and capture in
one sitting. The last part claims the After-Hours Fee task to open its Complete
dialog; reseed with --force before capturing again.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "business.settings_sop")

import django  # noqa: E402

django.setup()

from ops.models import OperationalTask  # noqa: E402
from scripts.sop_harness import SopCapture  # noqa: E402

DEPENDS_ON = [
    "dispatching/templates/dispatching/dispatcher_navbar.html",
    "dispatching/templates/dispatching/task_queue.html",
    "dispatching/templates/dispatching/includes/_task_row.html",
    "dispatching/templates/dispatching/includes/_turn_bundle.html",
    "dispatching/templates/dispatching/task_detail.html",
    "dispatching/templates/dispatching/conflict_task_detail.html",
    "dispatching/templates/dispatching/payment_task_detail.html",
    "ops/views.py",
    "ops/tasks.py",
    "ops/playbooks.py",
]

QUEUE = "/dispatching/task-queue/"
T = OperationalTask.TaskType


def task_id(task_type, title_prefix=""):
    qs = OperationalTask.objects.filter(task_type=task_type,
                                        status__in=list(OperationalTask.OPEN_STATUSES))
    if title_prefix:
        qs = qs.filter(title__startswith=title_prefix)
    task = qs.order_by("id").first()
    if task is None:
        raise SystemExit(f"No open {task_type} task {title_prefix!r} — run seed_tasks_sop_fixtures.")
    return task.id


def card(heading):
    """A standard task page's card, found by its heading."""
    return f".ops-card:has(h6:has-text('{heading}'))"


def action(label):
    return f".conflict-act-btn:has-text('{label}')"


def main():
    ids = {
        "conflict": task_id(T.DRIVER_CONFLICT),
        "no_driver": task_id(T.DRIVER_ASSIGNMENT),
        "unpaid": task_id(T.PAYMENT_CHASE),
        "moved": task_id(T.FLIGHT_VERIFICATION, "Flight mismatch"),
        "missing": task_id(T.FLIGHT_VERIFICATION, "⚠️ Flight not found"),
        "cancelled": task_id(T.FLIGHT_VERIFICATION, "Flight cancelled"),
        "overnight": task_id(T.FLIGHT_VERIFICATION, "Call to confirm overnight"),
        "confirm": task_id(T.CONFIRMATION_TEXTS),
        "contact": task_id(T.CONTACT_FORM),
        "fee": task_id(T.AFTERHOURS_FEE),
        "manual": task_id(T.MANUAL, "Call back"),
    }

    # Read before the browser starts: the ORM refuses to run inside Playwright's loop.
    manual_title = OperationalTask.objects.get(id=ids["manual"]).title

    def page(key):
        return f"{QUEUE}{ids[key]}/"

    with SopCapture(slug="tasks-guide", depends_on=DEPENDS_ON, as_user="jdoe") as cap:

        with cap.part("Ops Control") as cap:
            cap.goto(QUEUE)
            cap.shoot("01-ops-control", "Ops Control: where every task lands", highlight=[
                ("a.nav-link[href$='/task-queue/']", "1", "below"),
                (".nu-actions", "2", "left"),
                (".ops-lanes", "3", "inside-right"),
                (".ops-zone-header.zone-critical .ops-zone-label", "4", "right"),
                (".ops-row-actions", "5", "left"),
                (".ops-btn-create", "6", "left"),
            ])

        with cap.part("The buttons on a row") as cap:
            cap.goto(f"{QUEUE}?lane=mine")
            row = f"#task-row-{ids['unpaid']}"
            # Six buttons shoulder to shoulder: numbers alternate above and below.
            cap.shoot("02-row-buttons", "The buttons on a task you own",
                      clip=f"{row} .ops-row-actions", clip_pad=44, badge=14, highlight=[
                          (f"{row} .lbl-done", "1", "above"),
                          (f"{row} .lbl-release", "2", "below"),
                          (f"{row} .btn-log-comm", "3", "above"),
                          (f"{row} .btn-snooze-toggle", "4", "below"),
                          (f"{row} .btn-assign-toggle", "5", "above"),
                          (f"{row} [data-action='cancel']", "6", "below"),
                      ])

        with cap.part("Logging a call") as cap:
            cap.page.locator(f"#task-row-{ids['unpaid']} .btn-log-comm").click()
            cap.page.wait_for_timeout(600)
            cap.shoot("03-log-communication", "Logging a call, text or email",
                      clip="#logCommModal .modal-content")

        with cap.part("Completing with a note") as cap:
            cap.goto(f"{QUEUE}?lane=mine")
            cap.page.locator(f"#task-row-{ids['manual']} .lbl-done").click()
            cap.page.wait_for_timeout(600)
            cap.page.fill("#completeNotes", "Called Mr. Whitaker. Seat is on the trip and "
                                            "Calvin knows.")
            cap.shoot("04-complete-note", "Completing a task, with the note",
                      clip="#completeTaskModal .modal-content")

        with cap.part("Driver Conflict") as cap:
            cap.goto(page("conflict"))
            cap.shoot("10-driver-conflict", "Driver Conflict: what is wrong, in one line",
                      highlight=[(".cf-hero", "1"), (".cf-rec", "2"),
                                 ("a:has-text('Reassign leg')", "3", "below"),
                                 ("#cfResolve", "4", "below")])
            cap.shoot("11-driver-conflict-ladder", "Driver Conflict: the three ways to fix it",
                      clip=".cf-card:has(#cfLadder)", highlight=[
                          ("#cfLadder > .cf-step:first-of-type", "1", "inside-right"),
                          ("#cfStep2 .cf-assign", "2", "left"),
                          ("#cfStep3", "3", "inside-right"),
                      ])

        with cap.part("Driver Assignment") as cap:
            cap.goto(page("no_driver"))
            cap.shoot("12-driver-assignment", "No driver: pick someone who is free",
                      clip=[".td-title-main", card("Trip Details"), card("Driver Availability"),
                            card("Quick Actions")],
                      highlight=[(".da-driver-free", "1", "inside-right"),
                                 (action("Open Dispatch Board"), "2", "inside-right")])

        with cap.part("Unpaid Reservations") as cap:
            cap.goto(page("unpaid"))
            cap.shoot("13-unpaid", "Unpaid: the balance, the trip, and who owns it",
                      highlight=[(".cf-hero", "1"),
                                 ("a:has-text('Take payment')", "2", "below"),
                                 ("#cfComplete", "3", "below")])
            ladder = ".cf-card:has(.lbl:text-is('Collection Ladder'))"
            cap.shoot("14-unpaid-ladder", "Unpaid: call, then text, then email",
                      clip=ladder, highlight=[
                          (f"{ladder} :is(a,button):has-text('Call 555')", "1", "left"),
                          (f"{ladder} :is(a,button):has-text('What to say')", "2", "right"),
                          (f"{ladder} :is(a,button):has-text('Draft text')", "3", "right"),
                          (f"{ladder} :is(a,button):has-text('Send reminder email')", "4", "right"),
                      ])

        with cap.part("Flight mismatch") as cap:
            cap.goto(page("moved"))
            cap.shoot("15-flight-mismatch", "Flight moved: match the pickup, or keep it",
                      clip=[".td-title-main", card("Flight Mismatch"), card("Quick Actions")],
                      highlight=[(".act-match-flight", "1", "inside-left"),
                                 (action("Open Dispatch Board"), "2", "inside-right")])

        with cap.part("Flight not found") as cap:
            cap.goto(page("missing"))
            cap.shoot("16-flight-not-found", "Flight not found: get the right number",
                      clip=[".td-header", card("Quick Actions")],
                      highlight=[(action("Open Reservation"), "1")])

        with cap.part("Flight cancelled") as cap:
            cap.goto(page("cancelled"))
            cap.shoot("17-flight-cancelled", "Flight cancelled: call the guest first",
                      clip=[".td-header", card("Quick Actions")],
                      highlight=[(action("Open Dispatch Board"), "1"),
                                 (action("Open Reservation"), "2")])

        with cap.part("Overnight date") as cap:
            cap.goto(page("overnight"))
            cap.shoot("18-overnight-date", "Overnight pickup: confirm which night",
                      clip=[".td-header", card("Quick Actions")])

        with cap.part("Confirmation Texts") as cap:
            cap.goto(page("confirm"))
            cap.shoot("19-confirmation-texts", "Confirmation Texts: flights first, then send",
                      clip=[".td-title-main", card("Step 1"), card("Step 2")],
                      highlight=[(f"{card('Step 1')} .ops-card-head", "1", "inside-right"),
                                 (action("Open Confirmations Page"), "2", "inside-right")])

        with cap.part("Contact Us") as cap:
            cap.goto(page("contact"))
            cap.shoot("20-contact-us", "Contact Us: reply, then say so",
                      clip=[".td-title-main", ".cf-card"], badge=16,
                      highlight=[("[data-cf-action='contacted']", "1", "above"),
                                 ("[data-cf-action='closed']", "2", "above"),
                                 ("[data-cf-action='delete']", "3", "left")])

        with cap.part("After-Hours Fee") as cap:
            cap.goto(page("fee"))
            cap.shoot("21-after-hours-fee", "After-hours fee: charge it or settle it",
                      clip=[".td-title-main", card("After-Hours Fee")],
                      highlight=[("#chargeAfterhoursBtn", "1", "inside-right"),
                                 ("#settleAfterhoursBtn", "2", "inside-right")])

        with cap.part("Manual Task") as cap:
            cap.goto(page("manual"))
            cap.shoot("22-manual-task", "A manual task: do it, then Complete",
                      clip=[".td-header", card("Log Communication")],
                      highlight=[("#btnAssignToggle", "1", "below"),
                                 (".act-complete", "2", "below")])

        with cap.part("New Task") as cap:
            cap.goto(QUEUE)
            cap.page.locator(".ops-btn-create").click()
            cap.page.wait_for_timeout(600)
            cap.page.fill("#newTaskTitle", manual_title)
            cap.page.fill("#newTaskDescription", "Asked for a forward-facing seat for a "
                                                 "3-year-old. Confirm it's on the trip.")
            cap.shoot("23-new-task", "New Task, filled in",
                      clip="#createTaskModal .modal-content")

        # Last, because it changes the queue: claim the fee task so its row shows
        # Complete, then open the money question that Complete asks.
        with cap.part("After-Hours Fee: the Complete question") as cap:
            cap.goto(QUEUE)
            cap.page.locator(f"#task-row-{ids['fee']} .lbl-claim").click()
            cap.page.wait_for_timeout(2500)     # the queue reloads itself after a claim
            cap.page.wait_for_load_state("networkidle")
            cap.goto(f"{QUEUE}?lane=mine")
            cap.page.locator(f"#task-row-{ids['fee']} .lbl-done").click()
            cap.page.wait_for_timeout(600)
            cap.shoot("24-after-hours-complete", "Completing a fee task asks the money question",
                      clip="#completeTaskModal .modal-content")


if __name__ == "__main__":
    main()
