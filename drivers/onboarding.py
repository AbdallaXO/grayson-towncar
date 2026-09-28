"""The onboarding checklist on a driver's profile.

One plain list of what the office should have on file for a person who drives
for us, and whether it is there yet. Read-only: it never decides what a driver
*must* have (the DOT card, for example, only matters for someone cleared on a
vehicle that needs one) — it just says what is still blank so the person doing
the onboarding does not have to hunt through the page.
"""
from drivers import paperwork, phones


def _item(key, label, state, detail="", href=None):
    return {"key": key, "label": label, "state": state, "detail": detail, "href": href}


def checklist(driver, today=None):
    """{"items": [...], "done": n, "total": m, "pct": 0-100, "complete": bool}.

    ``state`` per item is one of ``done`` / ``partial`` / ``missing``.
    """
    user = driver.profile
    items = []

    # 1. Can they sign in?
    if driver.has_login():
        if user.last_login:
            items.append(_item("login", "Driver app login", "done",
                               f"last signed in {user.last_login:%b %-d}"))
        else:
            items.append(_item("login", "Driver app login", "partial",
                               "password set, has not signed in yet"))
    else:
        invite = driver.open_invite()
        if invite:
            when = invite.last_sent_at or invite.created_at
            how = {"sms": "texted", "email": "emailed", "link": "link copied"}.get(invite.sent_via, "sent")
            items.append(_item("login", "Driver app login", "partial",
                               f"welcome link {how} {when:%b %-d}, not opened yet"))
        else:
            items.append(_item("login", "Driver app login", "missing",
                               "no login yet — send a welcome link"))

    # 2. Contact.
    phone_ok = phones.is_valid(driver.phone_number)
    items.append(_item("phone", "Mobile number", "done" if phone_ok else "missing",
                       phones.pretty(driver.phone_number) if phone_ok else
                       ("number on file doesn't look right" if driver.phone_number else "")))
    items.append(_item("email", "Email address", "done" if user.email else "missing", user.email or ""))

    # 3. Paperwork — same states the directory uses.
    summary = paperwork.summarize(driver, today, with_urls=False)
    needs_dot = driver.certified_vehicle_types.exists()
    for doc in summary["docs"]:
        if doc["key"] == "dot" and not needs_dot:
            continue
        state = {
            paperwork.ON_FILE: "done",
            paperwork.EXPIRING: "partial",
            paperwork.PHOTO_ONLY: "partial",
            paperwork.EXPIRED: "missing",
            paperwork.MISSING: "missing",
        }[doc["state"]]
        label = {"license": "Driver's license", "permit": "Chauffeur permit", "dot": "DOT medical card"}[doc["key"]]
        items.append(_item(doc["key"], label, state, doc["detail"]))

    # 4. Personal details.
    items.append(_item("address", "Home address", "done" if driver.home_address else "missing",
                       driver.home_address))
    items.append(_item("hired_on", "Start date", "done" if driver.hired_on else "missing",
                       f"{driver.hired_on:%b %-d, %Y}" if driver.hired_on else ""))

    # 5. How we work with them.
    if driver.driver_type == "inhouse":
        items.append(_item("employment", "Full time or part time", "done" if driver.employment_type else "missing",
                           driver.get_employment_type_display().split(" — ")[0] if driver.employment_type else ""))

    done = sum(1 for i in items if i["state"] == "done")
    total = len(items)
    return {
        "items": items,
        "done": done,
        "total": total,
        "pct": int(round(100 * done / total)) if total else 100,
        "complete": done == total,
        "missing": [i for i in items if i["state"] != "done"],
    }
