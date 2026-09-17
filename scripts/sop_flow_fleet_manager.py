"""Capture the fleet manager's screens for the getting-started guide.

    python scripts/sop_flow_fleet_manager.py

Taken as **mhale** (Marcus Hale), the fictional fleet manager seeded by
``seed_fleet_sop_fixtures`` — is_staff, is_fleet_manager, not a superuser, and
working 7:30-4. That matters: every one of these screens branches on the role,
so a capture taken as a dispatcher or a founder would photograph a top bar and a
set of suggestions the real fleet manager never sees.

Prerequisites, once:

    python manage.py migrate --settings=business.settings_sop
    python manage.py seed_sop_fixtures --settings=business.settings_sop
    python manage.py seed_fleet_sop_fixtures --settings=business.settings_sop

Nothing here touches content/db.sqlite3, and the harness refuses to start if it
is pointed at anything but the disposable capture database. That is not
belt-and-braces: The day prints a guest's name, their pickup address and their
flight on every block, and these images are going into a document that leaves
the building.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sop_harness import SopCapture  # noqa: E402

# Templates and views these shots depend on. The drift check hashes them, so a
# change to any one of them flags the images as possibly stale.
DEPENDS_ON = [
    "dispatching/templates/dispatching/dispatcher_navbar.html",
    "dispatching/templates/dispatching/fleet_desk.html",
    "dispatching/templates/dispatching/fleet_day.html",
    "dispatching/templates/dispatching/fleet_inspections.html",
    "dispatching/templates/dispatching/fleet_list.html",
    "dispatching/templates/dispatching/fleet_detail.html",
    "dispatching/templates/dispatching/includes/_fleet_luxe_css.html",
    "dispatching/fleet_views.py",
    "dispatching/fleet_day.py",
    "dispatching/fleet_inspection.py",
]


def main():
    with SopCapture(slug="fleet-manager-guide", depends_on=DEPENDS_ON,
                    as_user="mhale") as cap:

        with cap.part("The Desk") as cap:
            cap.goto("/dispatching/fleet/")
            cap.shoot("01-desk", "Where you land every morning")
            cap.shoot("02-desk-full", "The whole Desk, top to bottom", full_page=True)

        with cap.part("A fault explained") as cap:
            cap.goto("/dispatching/fleet/")
            # Open the first fault code so the plain-English panel — and the
            # line saying we wrote it and it can be wrong — is in the picture.
            try:
                cap.page.locator(".fl-code").first.click()
                cap.page.wait_for_timeout(400)
            except Exception:
                print("    ! no fault codes on the Desk, skipping the open panel")
            cap.shoot("03-fault-code", "Tapping a fault code", full_page=True)

        with cap.part("Inspections") as cap:
            cap.goto("/dispatching/fleet/inspections/")
            cap.shoot("04-inspections", "This week's round, and today's handful",
                      full_page=True)

        with cap.part("Walking a car") as cap:
            href = cap.page.locator(".fi-card.due").first.get_attribute("href")
            cap.goto(href)
            cap.shoot("05-checklist", "The walk-around checklist", full_page=True)

        with cap.part("The day") as cap:
            cap.goto("/dispatching/fleet/day/")
            cap.shoot("06-the-day", "What every car is doing, in number order",
                      full_page=True)

        with cap.part("Vehicles") as cap:
            cap.goto("/dispatching/fleet/vehicles/")
            cap.shoot("07-vehicles", "All ten units", full_page=True)

        with cap.part("One car, and its service") as cap:
            href = cap.page.locator("a[href*='/dispatching/fleet/']").filter(
                has_text="#009").first.get_attribute("href")
            cap.goto(href)
            cap.shoot("08-vehicle-detail", "A car's own page", full_page=True)

        with cap.part("Outlook") as cap:
            cap.goto("/dispatching/fleet/outlook/")
            cap.shoot("09-outlook", "Booking a car in for shop work", full_page=True)


if __name__ == "__main__":
    main()
