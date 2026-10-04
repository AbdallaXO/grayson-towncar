"""The profile's driver-knowledge forms (structured shifts, Stage 1, S19).

    /drivers/<id>/tags/add/              (POST)  any staff user: add a tag + note
    /drivers/<id>/tags/<tag_id>/remove/  (POST)  managers: take a tag off
    /drivers/<id>/tags/new/              (POST)  managers: make a new tag, then add it
    /drivers/<id>/log/add/               (POST)  any staff user: add to the log
    /drivers/<id>/log/<entry_id>/edit/   (GET/POST) managers: change an entry
    /drivers/<id>/log/<entry_id>/delete/ (POST)  managers: delete an entry

Each one answers with a message and a redirect back to the driver's profile,
never JSON. The one exception is a log entry that doesn't pass its checks:
it comes back on the log entry page with what was typed and what to fix.
Managers are is_superuser, as everywhere on the profile; dispatcher logins
are is_staff. The logic lives in drivers/driver_knowledge.py.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from drivers import driver_knowledge
from drivers.forms import DriverLogEntryForm
from drivers.models import Driver, DriverLogEntry, DriverTag, DriverTagAssignment

NOTE_MAX = DriverTagAssignment._meta.get_field("note").max_length


def _back(driver):
    """The profile, scrolled to the Strengths & habits card."""
    return redirect(reverse("driver_profile", args=[driver.id]) + "#strengths-habits")


@login_required(login_url="login")
@require_POST
def driver_tag_add(request, driver_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Only the office can tag a driver.")
    driver = get_object_or_404(Driver, id=driver_id)
    tag_id = request.POST.get("tag", "")
    tag = DriverTag.objects.filter(id=tag_id).first() if tag_id.isdigit() else None
    note = request.POST.get("note", "").strip()
    if tag is None:
        messages.error(request, "Pick a tag to add.")
    elif not tag.is_active:
        messages.error(request, "That tag is no longer in use.")
    elif len(note) > NOTE_MAX:
        messages.error(request, f"Keep the note to {NOTE_MAX} characters or fewer.")
    else:
        had_it = DriverTagAssignment.objects.filter(driver=driver, tag=tag).exists()
        driver_knowledge.add_tag(driver, tag, note, request.user)
        if had_it:
            messages.success(request, f"Updated the note on {tag.name}." if note
                             else f"{driver} already has {tag.name}.")
        else:
            messages.success(request, f"{tag.name} added.")
    return _back(driver)


@login_required(login_url="login")
@require_POST
def driver_tag_remove(request, driver_id, tag_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only a manager can remove a tag.")
    driver = get_object_or_404(Driver, id=driver_id)
    tag = get_object_or_404(DriverTag, id=tag_id)
    driver_knowledge.remove_tag(driver, tag)
    messages.success(request, f"{tag.name} removed.")
    return _back(driver)


@login_required(login_url="login")
@require_POST
def driver_tag_create(request, driver_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only a manager can create a tag.")
    driver = get_object_or_404(Driver, id=driver_id)
    try:
        tag = driver_knowledge.create_tag(
            request.POST.get("name", ""), request.POST.get("category", ""),
            request.POST.get("polarity", ""), request.user,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return _back(driver)
    driver_knowledge.add_tag(driver, tag, "", request.user)
    messages.success(request, f"{tag.name} created and added.")
    return _back(driver)


# ── The log ─────────────────────────────────────────────────────────────────

def _back_to_log(driver):
    """The profile, scrolled to the Log card."""
    return redirect(reverse("driver_profile", args=[driver.id]) + "#driver-log")


def _entry_page(request, driver, form, entry=None):
    """The log entry page: editing an entry, or an add that needs fixing."""
    return render(request, "drivers/driver_log_edit.html", {
        "driver": driver, "form": form, "entry": entry,
        "can_edit": request.user.is_superuser,
    })


@login_required(login_url="login")
@require_POST
def driver_log_add(request, driver_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Only the office can add to a driver's log.")
    driver = get_object_or_404(Driver, id=driver_id)
    form = DriverLogEntryForm(driver, request.user, request.POST)
    if not form.is_valid():
        return _entry_page(request, driver, form)
    entry = form.save(commit=False)
    entry.driver = driver
    entry.logged_by = request.user
    if not request.user.is_superuser:
        entry.is_strike = False         # the form has no strike box for them; belt and braces
    entry.save()
    messages.success(request, "Added to the log.")
    return _back_to_log(driver)


@login_required(login_url="login")
@require_http_methods(["GET", "POST"])
def driver_log_edit(request, driver_id, entry_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only a manager can change a log entry.")
    driver = get_object_or_404(Driver, id=driver_id)
    entry = get_object_or_404(DriverLogEntry, id=entry_id, driver=driver)
    if request.method == "POST":
        form = DriverLogEntryForm(driver, request.user, request.POST, instance=entry)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.updated_by = request.user
            entry.save()
            messages.success(request, "Log entry updated.")
            return _back_to_log(driver)
    else:
        form = DriverLogEntryForm(driver, request.user, instance=entry)
    return _entry_page(request, driver, form, entry)


@login_required(login_url="login")
@require_POST
def driver_log_delete(request, driver_id, entry_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only a manager can delete a log entry.")
    driver = get_object_or_404(Driver, id=driver_id)
    entry = get_object_or_404(DriverLogEntry, id=entry_id, driver=driver)
    entry.delete()
    messages.success(request, "Log entry deleted.")
    return _back_to_log(driver)
