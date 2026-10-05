"""Drivers -> Regular Shifts (structured shifts, Stage 1, Tasks 7 and 8).

Four staff pages over drivers/regular_shifts.py:

  * regular_shifts — who still needs a regular shift (with what their last 8
    weeks suggest) and who has one, plus the switch that hands confirmed
    regular shifts to auto-assign. Every staff user may look.
  * regular_shift_edit — one driver's week: the usual shift, the days he
    works, and any day that differs. Staff may look; only a manager
    (is_superuser) confirms (S10).
  * regular_shift_switch — turns the switch on or off. Managers only; on is
    refused while anyone still needs a regular shift (S3).
  * shift_templates — the four shapes' usual times (targets, not limits) and
    the longest shift each may run. Staff may look; only a manager saves.

Regular shifts are for in-house chauffeurs (the roster): an affiliate or an
operator never gets one, so the editor 404s for them.

Plan: docs/scheduling-redesign/08_STAGE1_FOUNDATION_PLAN.md (Tasks 7 and 8).
"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from dispatching.models import SchedulerSettings

from . import regular_shifts as rs
from .availability import fmt_time_long
from .forms import RegularDayFormSet, RegularShiftForm, ShiftTemplateFormSet, regular_week
from .models import Driver, ShiftTemplate

WEEKS = rs.LOOKBACK_DAYS // 7
NO_TRIPS = f"no trips in the last {WEEKS} weeks"
NO_REGULAR_DAYS = f"no regular days in the last {WEEKS} weeks"
FLOAT_HINT = "Any shift — goes where the day needs him"
# What blank times mean, under a day's times (the page's script says the same).
BLANK_HINT = "Leave both blank for the usual times of his shift."
OPEN_DAY_HINT = ("This day goes wherever the day needs him, within 12 hours, so times "
                 "here are only a note.")


def _today():
    """The page's today (one place, so tests can pin it)."""
    return timezone.localdate()


def _confirmed_by(user) -> str:
    return (user.get_full_name() or user.username) if user else ""


def _role_text(template_id, templates) -> str:
    """'Morning driver' for a usual shift that isn't saved yet (a suggestion)."""
    return rs.role_label(SimpleNamespace(shift_role_id=template_id), templates)


def _still_need(n) -> str:
    return (f"{n} driver still needs a regular shift" if n == 1
            else f"{n} drivers still need a regular shift")


def _times(start, end) -> str:
    return f"{fmt_time_long(start)} – {fmt_time_long(end)}"


def _sentence(text) -> str:
    """'leaves 3 AM–6 AM, …' -> 'Leaves 3 AM–6 AM, …' (str.capitalize would
    lower the AM/PM)."""
    return text[:1].upper() + text[1:]


# ════════════════════════════════════════════════════════════════════════════
# The list
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url="login")
def regular_shifts(request):
    if not request.user.is_staff:
        return redirect("home")

    templates = rs.templates_by_id()
    roster = rs.roster_drivers()
    waiting = [d for d in roster if not d.has_regular_shift]
    confirmed = [d for d in roster if d.has_regular_shift]
    # One Leg query for everyone still waiting.
    suggestions = rs.suggest_regular_shifts(waiting, _today()) if waiting else {}
    confirmers = User.objects.in_bulk(
        {d.regular_shift_confirmed_by_id for d in confirmed} - {None})

    needs = []
    for driver in waiting:
        week = suggestions[driver.id]
        if not any(s.weeks_worked for s in week):
            summary = NO_TRIPS                  # Review Focus 4
        elif not any(s.template_id for s in week):
            summary = NO_REGULAR_DAYS           # worked, but no weekday 4 weeks in 8
        else:
            summary = rs.summary_label(week, templates)
        needs.append({"driver": driver, "summary": summary,
                      "role": _role_text(rs.suggest_role(week, templates), templates)})

    confirmed_rows = [{
        "driver": driver,
        "summary": rs.summary_label(rs.current_days(driver), templates),
        "role": rs.role_label(driver, templates),
        "confirmed_by": _confirmed_by(confirmers.get(driver.regular_shift_confirmed_by_id)),
        "confirmed_at": driver.regular_shift_confirmed_at,
    } for driver in confirmed]

    return render(request, "drivers/regular_shifts.html", {
        "switch_on": rs.regular_windows_on(),
        "can_manage": request.user.is_superuser,
        "needs": needs,
        "confirmed": confirmed_rows,
        "still_need": _still_need(len(needs)) if needs else "",
    })


# ════════════════════════════════════════════════════════════════════════════
# The editor
# ════════════════════════════════════════════════════════════════════════════

def _initial_row(day, role):
    """A day's starting values in the editor: no shape of its own when it is
    the usual shift ("Same as usual")."""
    return {"template": day.template_id if day.template_id != role else None,
            "alt_template": day.alt_template_id,
            "start": day.start, "end": day.end,
            "day_earliest_start": day.day_earliest,
            "day_latest_finish": day.day_latest,
            "day_latest_finish_next_day": day.day_latest_next_day}


def _evidence(suggestion) -> str:
    """What the last 8 weeks say about one weekday."""
    name = rs.DAY_NAMES[suggestion.day]
    if not suggestion.weeks_worked:
        return "Not a regular day"
    worked = f"Worked {suggestion.weeks_worked} of the last {WEEKS} {name}s"
    if suggestion.template_id is None:
        return f"{worked} · not a regular day"
    return f"{worked} · usual {_times(suggestion.start, suggestion.end)}"


def _shape_info(templates) -> dict:
    """Per shape, what the page's script needs to relabel a day as it is
    changed: the name, the kind, and the usual times blank means."""
    out = {}
    for t in templates.values():
        start, end = rs.band_fill(t)
        out[str(t.id)] = {"name": t.name, "kind": t.kind,
                          "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                          "usual": _times(start, end)}
    return out


def _rows(formset, days, works_on, role, suggestion, templates):
    """One entry per weekday for step 3: the form, what the day reads as, what
    blank times mean for its shift (also the time inputs' placeholders), and
    what the last 8 weeks say. A day with an error, or one a problem names, is
    opened. The page's script redoes label and hint as the day is changed."""
    problems = list(formset.non_form_errors())
    rows = []
    for i, form in enumerate(formset.forms):
        day, name = days[i], rs.DAY_NAMES[i]
        worked = i in works_on
        if not worked:
            label = "Off"
        elif day.template_id in templates:
            label = rs.day_label(day, templates)
        else:
            label = "Same as usual"             # no usual shift picked yet
        # An Off day still shows what the usual shift's blank times would be.
        tpl = templates.get(day.template_id) or templates.get(role)
        usual = ""
        if tpl is None:
            hint = BLANK_HINT
        elif tpl.kind == rs.FLOAT_KIND or rs.is_open_day(day, templates):
            hint = OPEN_DAY_HINT
        else:
            start, end = rs.band_fill(tpl)
            usual = _times(start, end)
            hint = f"Leave both blank for the usual times, {usual}."
            form.fields["start"].widget.attrs["placeholder"] = start.strftime("%H:%M")
            form.fields["end"].widget.attrs["placeholder"] = end.strftime("%H:%M")
        rows.append({
            "form": form, "name": name, "short": name[:3], "worked": worked,
            "label": label, "usual": usual, "hint": hint, "evidence": _evidence(suggestion[i]),
            "open": bool(form.errors) or any(p.startswith((f"{name}:", f"{name} to "))
                                             for p in problems),
        })
    return rows


def _limits(driver) -> list[tuple[str, str]]:
    """The driver's hard limits as (words, value), blank ones left out."""
    out = []
    if driver.hard_earliest_start is not None:
        out.append(("Never starts before", fmt_time_long(driver.hard_earliest_start)))
    if driver.hard_latest_finish is not None:
        latest = fmt_time_long(driver.hard_latest_finish)
        out.append(("Never finishes after",
                    latest + (" (next day)" if driver.hard_latest_finish_next_day else "")))
    if driver.max_days_per_week:
        out.append(("Days a week", str(driver.max_days_per_week)))
    return out


@login_required(login_url="login")
def regular_shift_edit(request, driver_id):
    if not request.user.is_staff:
        # Only a manager saves: anyone else's POST is forbidden, not sent home.
        return HttpResponseForbidden() if request.method == "POST" else redirect("home")
    driver = get_object_or_404(
        Driver.objects.select_related("profile", "regular_shift_confirmed_by"),
        pk=driver_id, driver_type="inhouse", portal_role="driver")
    can_manage = request.user.is_superuser
    if request.method == "POST" and not can_manage:
        return HttpResponseForbidden()

    templates = rs.templates_by_id()
    suggestion = rs.suggest_regular_shifts([driver], _today())[driver.id]
    if driver.has_regular_shift:
        days, role = rs.current_days(driver), driver.shift_role_id
    else:
        days = [rs.DayShift(s.day, s.template_id, s.start, s.end) for s in suggestion]
        role = rs.suggest_role(suggestion, templates)
    formset_kwargs = {"driver": driver, "templates": templates,
                      "rest_min": SchedulerSettings.get_settings().rest_min_gap_minutes}

    if request.method == "POST":
        shift_form = RegularShiftForm(request.POST, templates=templates)
        formset = RegularDayFormSet(request.POST, week_form=shift_form, **formset_kwargs)
        # Both, so a page with a missing usual shift still shows the days'
        # errors: their fields', and the week's checks on every day that has
        # a shift of its own (BaseRegularDayFormSet.clean).
        shift_ok, days_ok = shift_form.is_valid(), formset.is_valid()
        if shift_ok and days_ok:
            try:
                rs.save_regular_shift(driver, formset.days, request.user,
                                      role_template_id=shift_form.cleaned_data["role"])
            except ValueError as exc:       # the shapes changed under the page
                for line in str(exc).splitlines():
                    messages.error(request, line)
            else:
                messages.success(request, f"Regular shift confirmed for {driver}.")
                for warning in rs.band_warnings(
                        formset.days, templates,
                        hard_earliest_start=driver.hard_earliest_start,
                        hard_latest_finish=driver.hard_latest_finish,
                        hard_latest_finish_next_day=driver.hard_latest_finish_next_day):
                    messages.warning(request, warning)
                return redirect("regular_shifts")
        week = getattr(shift_form, "cleaned_data", {})
        role = week.get("role")
        works_on = set(week.get("works_on") or [])
        days = regular_week(week, [getattr(f, "cleaned_data", {}) for f in formset.forms])
    else:
        works_on = {d.day for d in days if d.template_id is not None}
        shift_form = RegularShiftForm(initial={"role": role, "works_on": sorted(works_on)},
                                      templates=templates)
        formset = RegularDayFormSet(initial=[_initial_row(d, role) for d in days],
                                    week_form=shift_form, **formset_kwargs)

    picked = shift_form["role"].value()
    cards = [{"id": t.id, "name": t.name, "kind": t.kind,
              "hint": FLOAT_HINT if t.kind == rs.FLOAT_KIND else _sentence(t.band_label()),
              "checked": picked is not None and str(t.id) == str(picked)}
             for t in sorted(templates.values(), key=lambda t: (t.sort_order, t.id))]
    worked_recently = any(s.weeks_worked for s in suggestion)
    return render(request, "drivers/regular_shift_edit.html", {
        "driver": driver,
        "can_manage": can_manage,
        "shift_form": shift_form,
        "day_formset": formset,
        "cards": cards,
        "rows": _rows(formset, days, works_on, role, suggestion, templates),
        "limits": _limits(driver),
        "confirmed_by": _confirmed_by(driver.regular_shift_confirmed_by),
        "worked_recently": worked_recently,
        "weeks": WEEKS,
        "shapes": _shape_info(templates),
    })


# ════════════════════════════════════════════════════════════════════════════
# The switch
# ════════════════════════════════════════════════════════════════════════════

@login_required(login_url="login")
@require_POST
def regular_shift_switch(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    on = request.POST.get("on") == "1"
    done, refusal = rs.set_regular_windows(on, request.user)
    if not done:
        messages.error(request, refusal)
    elif on:
        messages.success(request, "Auto-assign now uses regular shifts.")
    else:
        messages.success(request, "Auto-assign is back on today's hours.")
    return redirect("regular_shifts")


# ════════════════════════════════════════════════════════════════════════════
# Shift Templates
# ════════════════════════════════════════════════════════════════════════════

def _shape_card(form, stored, used_by) -> dict:
    """What a shape's card shows besides its fields, from the shape as saved
    (``stored``), not as posted: how many drivers have it as their usual
    shift, the times blank means on a day of it, and who last changed it.
    A form the page never offered (a POST with extra forms) has an unsaved
    shape with no times, so it shows no usual times."""
    is_float = stored.kind == rs.FLOAT_KIND
    end_lo, end_hi = stored.end_earliest, stored.end_latest
    has_times = None not in (stored.start_latest, end_lo, end_hi)
    return {
        "form": form, "name": stored.name, "kind": stored.kind,
        "is_float": is_float,
        "used_by": used_by,
        # A Float day (or one that may be either of two shifts) runs anywhere
        # from the earliest leave to the latest return (S18); its times are
        # only a note, so there are no usual times to show.
        "usual": _times(*rs.band_fill(stored)) if has_times and not is_float else "",
        "back_next_day": has_times and end_hi < end_lo,
        "updated_by": _confirmed_by(stored.updated_by),
        "updated_at": stored.updated_at,
    }


@login_required(login_url="login")
def shift_templates(request):
    """The four shapes (Morning, Midday, Evening, Float): their usual leave and
    return times, the longest shift each may run and notes. Every staff user
    may look; only a manager saves (S10). Saving drops the cached shapes so
    every page and auto-assign read the new ones."""
    if not request.user.is_staff:
        return HttpResponseForbidden() if request.method == "POST" else redirect("home")
    can_manage = request.user.is_superuser
    if request.method == "POST" and not can_manage:
        return HttpResponseForbidden()

    if request.method == "POST":
        formset = ShiftTemplateFormSet(request.POST, prefix="shapes")
        if formset.is_valid():
            with transaction.atomic():
                for template in formset.save(commit=False):    # the changed ones
                    template.updated_by = request.user
                    template.save()
            rs.clear_template_cache()
            messages.success(request, "Shift templates saved.")
            return redirect("shift_templates")
    else:
        formset = ShiftTemplateFormSet(prefix="shapes")

    # A refused POST has put the posted values on the forms' instances; the
    # cards' own lines read the shapes as saved.
    stored = {t.id: t for t in ShiftTemplate.objects.select_related("updated_by")}
    used_by = Counter(d.shift_role_id for d in rs.roster_drivers() if d.shift_role_id)
    cards = [_shape_card(form, stored.get(form.instance.pk) or form.instance,
                         used_by[form.instance.pk])
             for form in formset.forms]
    return render(request, "drivers/shift_templates.html", {
        "can_manage": can_manage,
        "formset": formset,
        "cards": cards,
        "float_hint": FLOAT_HINT,
    })
