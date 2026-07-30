"""Findings and the "worth a conversation" list for the chauffeur load pages.

Rules, not copy: every sentence is a template filled from counted data, and every number
is paired with a comparison — cohort peers or the previous window — never an adjective.
A rule that does not fire contributes nothing, so an unremarkable window renders no
findings section at all.

Threshold design: each outlier rule needs BOTH a relative condition (versus cohort
peers) and an absolute floor. A purely self-calibrating cutoff always manufactures an
outlier, even on a perfectly fair fleet; a purely absolute cutoff is the
``COVERAGE_TARGET = 14`` mistake. Floors scale with the window so the 7/30/90-day views
all behave. The exact values are documented for dispatchers in
SOPS/chauffeur-load-metrics.md (SOP-003) — change them here and there together.

Everything here is pure: rows in (as built by load_metrics.build_load_rows), dicts out,
no queries. That keeps every rule unit-testable with hand-made rows.
"""

from __future__ import annotations

import statistics

#: Consecutive worked days before "no day off" becomes a conversation.
MIN_STREAK_DAYS = 10


def min_avail_days(window_days: int) -> int:
    """Days of availability required before idleness rules may judge a driver."""
    return max(3, window_days // 5)       # 7 -> 3, 30 -> 6, 90 -> 18


def min_worked_days(window_days: int) -> int:
    """Days actually driven required before per-day density is comparable."""
    return max(3, window_days // 10)      # 7 -> 3, 30 -> 3, 90 -> 9


def build_insights(rows, window_days, *, prior_rows=None, include_admin_link=False):
    """All findings and exceptions for one window.

    ``prior_rows`` are lite rows for the window of equal length immediately before this
    one; the trend finding stays silent without them. ``include_admin_link`` lets the
    unlabelled finding carry its fix-it link (KPI page only — dispatchers can't reach
    the admin).
    """
    findings = []
    for f in (
        _f_ft_uneven_load(rows, window_days),
        _f_ft_idle_concentration(rows, window_days),
        _f_ft_share_trend(rows, prior_rows, window_days),
        _f_pt_outworking_ft(rows, window_days),
        _f_unlabelled(rows, include_admin_link),
    ):
        if f:
            findings.append(f)

    exceptions, fired = _build_exceptions(rows, window_days)

    # When "available, never drove" is true of over a third of the roster it is not an
    # exception any more — it is the state of the window (a holiday week, a demand dip,
    # legs not closed out). A wall of names reads as 20 personal problems; one sentence
    # reads as the fleet-level fact it is.
    zero = [e for e in exceptions if e["rule"] == "never_drove"]
    if len(zero) > max(2, len(rows) // 3):
        exceptions = [e for e in exceptions if e["rule"] != "never_drove"]
        findings.append({
            "id": "widespread_zero_trips",
            "text": (f"{len(zero)} of the {len(rows)} chauffeurs were available in "
                     f"this window and had no trips at all."),
        })

    # ``fired`` is deliberately pre-collapse and includes non-winning rules: it is the
    # ground truth for "is this (driver, rule) episode still going", which dismissal
    # spending needs even when the entry itself is collapsed or outranked.
    return {"findings": findings, "exceptions": exceptions, "fired": fired}


# ──────────────────────────────────────────────────────────────────────────────
# findings — fleet-level sentences
# ──────────────────────────────────────────────────────────────────────────────

def _f_ft_uneven_load(rows, window_days):
    """Top full-timer's weekly rate at least double the bottom's."""
    floor = min_worked_days(window_days)
    qual = [r for r in rows
            if r["is_full_time"] and r["legs"] > 0 and r["worked_days"] >= floor]
    if len(qual) < 3:
        return None
    top = max(qual, key=lambda r: r["per_week"])
    bottom = min(qual, key=lambda r: r["per_week"])
    if bottom["per_week"] <= 0 or top["per_week"] < 2 * bottom["per_week"]:
        return None
    return {
        "id": "ft_uneven_load",
        "text": (f"Work is landing unevenly among the full-time team: "
                 f"{top['name']} averaged {_fmt(top['per_week'])} trips a week, "
                 f"{bottom['name']} averaged {_fmt(bottom['per_week'])}."),
    }


def _f_ft_idle_concentration(rows, window_days):
    """A large share of full-time idle days belongs to one or two people."""
    ft = [r for r in rows if r["is_full_time"]]
    if len(ft) < 3:
        return None
    total = sum(r["idle_days"] for r in ft)
    if total < max(5, window_days // 6):
        return None
    top = sorted(ft, key=lambda r: -r["idle_days"])[:2]
    for k in (1, 2):
        # Concentration means a MINORITY holds the majority: in a cohort of 3, the top
        # two holding 60% is just proportional spread, so k must stay ≤ a third.
        if k / len(ft) > 1 / 3:
            break
        subset = top[:k]
        held = sum(r["idle_days"] for r in subset)
        if held / total >= 0.6:
            names = " and ".join(r["name"] for r in subset)
            return {
                "id": "ft_idle_concentration",
                "text": (f"{total} full-time available days ended with no trips — "
                         f"{held} of them belong to {names}."),
            }
    return None


def _f_ft_share_trend(rows, prior_rows, window_days):
    """Full-time worked-share moved 10+ points versus the previous window."""
    cur, n_cur = _ft_worked_share(rows)
    prev, n_prev = _ft_worked_share(prior_rows or [])
    if cur is None or prev is None or n_cur < 2 or n_prev < 2:
        return None
    delta = cur - prev
    if abs(delta) < 0.10:
        return None
    direction = "up" if delta > 0 else "down"
    return {
        "id": "ft_share_trend",
        "text": (f"Full-timers drove on {round(cur * 100)}% of their available days, "
                 f"{direction} from {round(prev * 100)}% over the previous "
                 f"{window_days} days."),
    }


def _f_pt_outworking_ft(rows, window_days):
    """A part-timer drove more trips than the full-time median — a mislabel or a lean."""
    ft_legs = [r["legs"] for r in rows if r["is_full_time"]]
    if len(ft_legs) < 2:
        return None
    ft_median = statistics.median(ft_legs)
    floor = max(5, window_days // 6)
    pts = sorted((r for r in rows
                  if r["employment_type"] == "part_time"
                  and r["legs"] > ft_median and r["legs"] >= floor),
                 key=lambda r: -r["legs"])
    if not pts:
        return None
    if len(pts) == 1:
        p = pts[0]
        text = (f"{p['name']} is labelled part time but drove {p['legs']} trips — "
                f"more than the full-time median of {_fmt(ft_median)}.")
    else:
        listed = " and ".join(f"{p['name']} ({p['legs']} trips)" for p in pts[:2])
        extra = f" and {len(pts) - 2} more" if len(pts) > 2 else ""
        text = (f"{listed}{extra} are labelled part time but drove more than the "
                f"full-time median of {_fmt(ft_median)} trips.")
    return {"id": "pt_outworking_ft", "text": text}


def _f_unlabelled(rows, include_admin_link):
    n = sum(1 for r in rows if not r["employment_type"])
    if not n:
        return None
    noun, verb = ("chauffeur", "has") if n == 1 else ("chauffeurs", "have")
    return {
        "id": "unlabelled",
        "text": (f"{n} {noun} {verb} no full-time / part-time label, "
                 f"so their idle days can't be compared."),
        "admin_link": include_admin_link,
    }


# ──────────────────────────────────────────────────────────────────────────────
# exceptions — one entry per driver, first matching rule wins
# ──────────────────────────────────────────────────────────────────────────────

#: Rule priority. A driver matching several is LISTED once, under the first.
EXCEPTION_RULES = ("no_day_off_streak", "never_drove", "ft_mostly_idle",
                   "days_packed_harder")

#: Short labels for handled-list fallback rows, where the winning entry belongs to a
#: different rule so no full reason sentence exists for this one.
RULE_LABELS = {
    "no_day_off_streak": "Working without a day off",
    "never_drove": "Available, never drove",
    "ft_mostly_idle": "Mostly idle",
    "days_packed_harder": "Days packed harder",
}


def _build_exceptions(rows, window_days):
    """Returns (entries, fired).

    ``entries`` is the display list — one per driver, best rule wins. ``fired`` is
    every (driver_id, rule) pair that matched, winning or not: dismissal spending
    asks "is this episode still going", and a rule outranked by a streak is still
    going.
    """
    checks = (
        ("no_day_off_streak", _x_no_day_off_streak),
        ("never_drove", _x_never_drove),
        ("ft_mostly_idle", _x_ft_mostly_idle),
        ("days_packed_harder", _x_days_packed_harder),
    )
    entries = []
    fired = []
    for r in rows:
        entry = None
        for rule, check in checks:
            hit = check(r, rows, window_days)
            if not hit:
                continue
            fired.append((r["id"], rule))
            if entry is None:
                reason, magnitude = hit
                entry = {
                    "driver_id": r["id"],
                    "name": r["name"],
                    "initials": r["initials"],
                    "color": r["color"],
                    "employment_type": r["employment_type"],
                    "employment_label": r["employment_label"],
                    "rule": rule,
                    "reason": reason,
                    "_magnitude": magnitude,
                }
        if entry:
            entries.append(entry)

    entries.sort(key=lambda e: (EXCEPTION_RULES.index(e["rule"]), -e["_magnitude"]))
    for e in entries:
        del e["_magnitude"]
    return entries, fired


def _x_no_day_off_streak(r, rows, window_days):
    length, span = _longest_run(r["worked_dates"])
    if length < MIN_STREAK_DAYS:
        return None
    reason = (f"Worked {length} days in a row ({_span_label(*span)}) "
              f"without a day off.")
    return reason, length


def _x_never_drove(r, rows, window_days):
    if r["legs"] or r["avail_days"] < min_avail_days(window_days):
        return None
    others = [o for o in rows if o is not r and o["avail_days"]]
    if others:
        avg = sum(o["legs"] for o in others) / len(others)
        reason = (f"Available {r['avail_days']} days, drove none. "
                  f"The rest of the team averaged {_fmt(avg)} trips.")
    else:
        reason = f"Available {r['avail_days']} days, drove none."
    return reason, r["avail_days"]


def _x_ft_mostly_idle(r, rows, window_days):
    if (not r["is_full_time"] or not r["legs"]
            or r["avail_days"] < min_avail_days(window_days)):
        return None
    share = r["worked_days"] / r["avail_days"]
    peers = [o for o in rows
             if o["is_full_time"] and o is not r and o["avail_days"]]
    if len(peers) >= 2:
        # Relative AND absolute: clearly below the other full-timers, and at most
        # half their own available days — so a uniformly quiet window fires nothing.
        median_share = statistics.median(o["worked_days"] / o["avail_days"] for o in peers)
        if share > 0.5 or share > median_share - 0.25:
            return None
    else:
        # Too few peers to compare against, so only an extreme absolute fires.
        if share > 1 / 3:
            return None
    if peers:
        avg_worked = sum(o["worked_days"] for o in peers) / len(peers)
        avg_avail = sum(o["avail_days"] for o in peers) / len(peers)
        reason = (f"Drove {r['worked_days']} of {r['avail_days']} available days; "
                  f"the other full-timers averaged {_fmt(avg_worked)} of "
                  f"{_fmt(avg_avail)}.")
    else:
        reason = f"Drove {r['worked_days']} of {r['avail_days']} available days."
    return reason, 1 - share


def _x_days_packed_harder(r, rows, window_days):
    if r["worked_days"] < min_worked_days(window_days):
        return None
    peers = [o for o in rows
             if o["employment_type"] == r["employment_type"] and o is not r
             and o["worked_days"]]
    if len(peers) < 2:
        return None
    median_pwd = statistics.median(o["per_worked_day"] for o in peers)
    # Relative AND absolute: half again the peer median, and at least 1.5 trips more.
    if r["per_worked_day"] < 1.5 * median_pwd or r["per_worked_day"] < median_pwd + 1.5:
        return None
    reason = (f"Averages {_fmt(r['per_worked_day'])} trips on days they work; "
              f"the rest of their group averages {_fmt(median_pwd)}.")
    return reason, r["per_worked_day"]


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ft_worked_share(rows):
    ft = [r for r in rows if r["is_full_time"]]
    avail = sum(r["avail_days"] for r in ft)
    worked = sum(r["worked_days"] for r in ft)
    return (worked / avail) if avail else None, len(ft)


def _longest_run(dates):
    """Longest run of consecutive calendar dates. Returns (length, (start, end))."""
    best_len, best_span = 0, (None, None)
    run_start, prev, length = None, None, 0
    for dt in dates:
        if prev is not None and (dt - prev).days == 1:
            length += 1
        else:
            run_start, length = dt, 1
        if length > best_len:
            best_len, best_span = length, (run_start, dt)
        prev = dt
    return best_len, best_span


def _span_label(start, end):
    """'Jun 2–17' or 'Jun 28 – Jul 4'. No %-d on Windows, so day numbers are built."""
    a = f"{start:%b} {start.day}"
    if start.month == end.month and start.year == end.year:
        return f"{a}–{end.day}"
    return f"{a} – {end:%b} {end.day}"


def _fmt(v):
    """One decimal, trailing zero trimmed: 5.0 -> '5', 5.25 -> '5.3'."""
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s
