"""Cross-platform strftime.

``%-d`` / ``%-I`` ("no leading zero") are a glibc extension. Linux accepts them,
Windows raises ``ValueError: Invalid format string`` — so every one of them is a
500 that only ever fires on a developer's machine, never in production, which is
the worst possible place for a bug to hide. One of them took down /dispatching/
via Leg.route_map_url.

The codebase had grown five independent workarounds for this (``ops.coverage.md``,
``farmout_optimizer._fmt_time``, a try/except in ghl_integration, and two
"Windows-safe" comment blocks), each covering only its own call site. This is the
one place that knows the trick:

    from business.datefmt import strf
    strf(leg.pickup_date, "%a, %b %-d")     # 'Sun, Aug 17' everywhere

Substitutes the unpadded numbers directly into the format string, then hands the
rest to the platform's own strftime — so locale-aware pieces (%a, %b, %p) keep
working exactly as before. Only the directives actually present are touched. A
directive the value genuinely can't satisfy (%-I on a date) falls through to
strftime and raises there, as it should: that's a caller bug, not a platform
difference, and silently swallowing it would hide it on Linux too.
"""

# Each entry yields the unpadded number as a string. Values are always digits, so
# substituting them into the format string can't inject a stray % directive.
_UNPADDED = {
    "%-d": lambda v: str(v.day),
    "%-m": lambda v: str(v.month),
    "%-y": lambda v: str(v.year % 100),
    "%-H": lambda v: str(v.hour),
    "%-I": lambda v: str(((v.hour - 1) % 12) + 1),
    "%-M": lambda v: str(v.minute),
    "%-S": lambda v: str(v.second),
    "%-j": lambda v: str(v.timetuple().tm_yday),
}


def strf(value, fmt):
    """``value.strftime(fmt)``, accepting ``%-X`` directives on any platform.

    Works with date, time, and datetime. Returns "" for None so callers can drop
    their own None guards.
    """
    if value is None:
        return ""
    if "%-" in fmt:
        for token, unpadded in _UNPADDED.items():
            if token not in fmt:
                continue
            try:
                fmt = fmt.replace(token, unpadded(value))
            except (AttributeError, ValueError):
                pass  # e.g. %-I against a date — leave it for strftime to judge
    return value.strftime(fmt)


def time12(value):
    """'3:45 PM' — the format this app shows times in, minus the leading zero."""
    return strf(value, "%-I:%M %p")


def day_month(value):
    """'Aug 5'."""
    return strf(value, "%b %-d")


def weekday_day_month(value):
    """'Sun, Aug 17'."""
    return strf(value, "%a, %b %-d")
