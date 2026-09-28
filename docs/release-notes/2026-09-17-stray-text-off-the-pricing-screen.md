---
date: 2026-09-17
audience: Dispatchers
title: The stray sentence on the pricing screen is gone
---

# The stray sentence on the pricing screen is gone

## Send this to the team

> Hey team — if you've seen a stray line of text on the pricing screen when
> booking a late-night trip, something like *"The one question only the person
> setting the price can answer…"*, that's gone.
>
> It was a note meant for whoever maintains the code, and it was never supposed
> to be on your screen. Nothing was broken and nothing you entered was affected —
> it was just words in the wrong place.
>
> The after-hours question itself is unchanged, and so is everything else on that
> screen.

---

## Behind the scenes

**Where it lives:** the pricing step of the dispatcher booking wizard, on any
booking with a late-night leg.

**Why:** Django's `{# ... #}` comment is **single-line only**. Spread across more
than one line it silently stops being a comment and renders as page text. Nothing
errors; the words simply appear. It shipped in the after-hours pricing question
and has been visible since.

The same mistake was caught a second time on the trip card before it shipped, so
this is now guarded by a test that walks every template and fails on any `{#` that
does not close on its own line. Multi-line explanation has to use
`{% comment %}` / `{% endcomment %}`.

**Expect to be asked:**
- *"Did that break anything?"* No. It was display-only — a comment that rendered
  instead of hiding. No pricing, no booking, no field was affected.
