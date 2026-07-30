# SOP-003: Reading the Chauffeur Load Page

**Applies to:** Dispatchers and management using Availability & Load / Chauffeur KPIs
**Last updated:** July 29, 2026

---

## Purpose

The load pages answer "who is doing how much work, and who is sitting idle." The page
itself stays plain on purpose — every number on it is counted from records, and anything
that needs explaining is explained here instead of in a footnote. If you only read one
section, read **full time vs part time**.

There are two versions of the page:

| Page | Who | Shows |
|---|---|---|
| Availability & Load | Any dispatcher | Tiles, findings, distribution, roster |
| Chauffeur KPIs | Management | The same, plus the "Worth a conversation" list |

There is **no money on either page**. Revenue, pay and margin are moving to a separate
Driver economics page (not built yet — the KPI header carries a placeholder). Volume and
fairness questions should not be answered while looking at margin figures.

---

## The one thing to understand first: full time vs part time

Every number about idle time depends on this label.

- **Full time** — an available day is a **commitment**. They expect to work it. A day
  available with no trips is a real finding.
- **Part time** — an available day is an **offer**. They are telling us they *could*
  work. Not working it is normal and not a problem.

The same "6 available days with no trips" is a serious finding for a full-timer and
completely unremarkable for a part-timer. That is why the roster groups by label instead
of ranking everyone in one list, and why every comparison the page makes stays inside
one group.

**Chauffeurs with no label are shown in their own group** and their idle days are not
judged. Set the label in Django admin → Drivers → *Employment type*. Leave it blank if
you are unsure; a wrong label is worse than no label, because it inverts what the page
is telling you.

---

## The tiles

Four headline numbers, each paired with a comparison rather than an adjective:

- **Trips completed** — with the same count for the previous window.
- **Trips per working day** — the fleet median, with the previous window's median. This
  is *density*: how heavy a working day is, not how many days were worked.
- **Full-time days idle** — days full-timers were available but had no trips, out of
  their total available days, with the previous window's count.
- **Available, never drove** — chauffeurs who were available in the window and did not
  drive a single trip. Names listed.

## "What stands out" — the findings

Short sentences computed from the window's data. Each one has a rule behind it; when no
rule fires, the section does not appear — an empty findings panel would invite reading
noise as signal. The rules, exactly as implemented (`dispatching/load_insights.py`):

| Finding | Fires when |
|---|---|
| Work landing unevenly | At least 3 full-timers each drove on 3+ days (9+ in the 90-day view), and the busiest's weekly trip rate is at least **double** the lightest's. |
| Idle days concentrated | Full-timers' idle days total at least 5 (15 in the 90-day view), and a **third or fewer** of the full-time group holds **60%+** of them. |
| Worked-share moved | Full-timers' share of available days actually worked moved **10+ points** versus the previous window of the same length. |
| Part-timer outworking | A part-timer drove **more trips than the full-time median** (and at least 5). Usually means the label is wrong or we are leaning on them like a full-timer. |
| Unlabelled chauffeurs | Anyone has no full/part-time label. Their numbers are shown but never compared. |

## "Worth a conversation" — management page only

One line per chauffeur, with the reason and the comparison. A chauffeur appears at most
once, under the first rule that matches:

| Rule | Fires when | Why it is a conversation |
|---|---|---|
| No day off | Worked **10+ consecutive days** in the window. | Fatigue. This one outranks everything else. |
| Never drove | Available **6+ days** (30-day view; 3+/18+ for 7/90) with **zero trips**. If more than a third of the roster matches, the entries collapse into a single finding instead — that is a fleet situation, not twenty personal ones. | Either we failed them or the availability is stale. |
| Mostly idle | A full-timer who drove **half or fewer** of their available days *and* sits **25+ points below** the other full-timers' median. With fewer than 3 full-timers to compare, only a third or less fires. | A committed person the schedule is not using. |
| Days packed harder | Drove on 3+ days (9+ in 90-day view) and averages **1.5× the group median and at least 1.5 more trips** per working day. | Their days are heavier than everyone else's. |

Every idleness rule needs **both** a relative condition (versus the group) and an
absolute floor. That is deliberate: a purely relative cutoff always manufactures an
outlier even on a perfectly fair fleet, and a purely absolute one is an invented target.
The floors scale with the 7/30/90-day window so short views don't judge on thin
evidence.

The roster highlights the idle count of anyone on this list. The dispatcher page never
receives the list or the highlights — its rows carry counts only.

### Marking one handled

Had the conversation? Press **Handled** on the entry (an optional note travels with it —
useful when more than one person reads the page). What that does:

- The entry moves out of the active list into the collapsed **Handled (n)** line at the
  bottom of the panel, showing who marked it, when, and the note. The roster highlight
  for that chauffeur turns off too. **Undo** puts it straight back.
- It stays handled **for as long as the same situation lasts** — a driver you know is
  on leave does not re-nag every week.
- The moment the situation clears (the streak breaks, the idle driver drives), the
  dismissal is spent. If the **same problem starts again later, it comes back fresh** —
  a new episode is a new conversation.
- Whether a situation "still lasts" is judged **only on the window where you pressed
  Handled** — flipping between the 7/30/90 views can never accidentally clear one. A
  chauffeur being deactivated doesn't clear one either: that is an unevaluated episode,
  not an ended one.
- If a bigger issue takes over that chauffeur's spot on the list (say a no-day-off
  streak outranks the item you handled), the handled item stays visible in the Handled
  line as "*… — still applies*" so it can still be undone.

Handled state is management-only, like the list itself — nothing about it reaches the
dispatcher page.

## The roster, column by column

### Days available

Days this chauffeur was marked available in the window. Counted from the schedule and
approved exceptions — pending time-off requests change nothing until approved.

### Days worked

Days they actually drove at least one trip.

### Days idle

**Available days with no trips** — days available minus days worked. For a full-timer
this is the column to watch. For a part-timer it is just information. There is
deliberately no target percentage anywhere; the day count is the signal (see the
methodology appendix).

### Trips

Completed trips in the window.

### Per worked day (and the "Trips averaged per" switch)

**Trips ÷ days they actually drove** — how heavy a day is *when* they work. Worked
example:

| | Trips | Days worked | Per worked day |
|---|---|---|---|
| Michael | 58 | 10 | **5.8** |
| Seline | 68 | 24 | **2.8** |

Seline did more total trips, but Michael's working days are twice as heavy. No other
column shows that.

The switch changes the divisor:

- **Worked day** — density. The default.
- **Available day** — folds idle days back in; drops for anyone with unassigned days.
- **Week / Month** — a normalised rate that stays comparable whichever window is
  selected. This is the number to quote in a conversation with a chauffeur.

### Last 3 weeks · next 7

One square per day, three weeks back then one week forward.

| Square | Meaning |
|---|---|
| Solid, taller = more trips | Worked |
| Hollow outline | Available, nothing assigned |
| Flat tick | Scheduled day off |
| Blue | Approved time off |
| Gold vertical line | Today — squares to the right are upcoming |

Because it crosses today it doubles as a quick build-ahead reference.

### Next time off

The soonest approved upcoming time off. Pending requests do not appear.

---

## What this page does not tell you

- **"Who can take this leg tomorrow at 4pm?"** Use the dispatch board and the planner.
  This page is the weekly "who should I lean on, who should I spare" layer above that.
- **Whether a chauffeur is any good.** No complaints, on-time or guest-feedback figures
  here. Volume is not quality.
- **Who caused an imbalance.** The page can show work landed unevenly; it cannot say
  whether the scheduler did it or a dispatcher did it by hand.
- **Anything about money.** By design — Driver economics is separate, future work.

## Use it as a tiebreaker, never as an argument

If two chauffeurs are equally suitable for a trip, giving it to the one who has been
light is a good use of this page. Overriding a tight-turn, rest, or feasibility warning
to balance the numbers is not. The operational check always wins. Findings and the
conversation list are prompts to *talk to a person*, never triggers for any automatic
action.

---

## Methodology appendix — why the page looks the way it does

Kept here so the page itself doesn't have to explain itself.

**Why days, not an hours-based percentage.** Earlier versions showed a percentage of
worked hours against available hours. Its denominator rested on two assumptions: an
open/flex day was assumed to be 12 hours, and "available" in our system today records
*willingness* to work rather than a commitment (Phase 0 audit: several chauffeurs marked
available 7 days a week whom nobody expects to work 7 days). A percentage built on two
assumptions invites precise-sounding judgements of people. Idle days are counted, so
they carry no such risk. The hours logic survives in code
(`load_metrics.available_hours_for`) for the future Driver economics page.

**Why there is no "share of work" ratio.** The same fairness question is now answered by
the findings sentences, which name the actual numbers being compared ("X averaged 14
trips a week, Y averaged 5") instead of asking everyone to interpret a ratio around
1.00. The within-group rule is unchanged: a part-timer's trip share measured against
availability makes them look starved for being part time, so groups are never mixed.

**Why there is no shaded "healthy range".** An early draft shaded 50–82% as healthy —
numbers invented to make the picture readable. Checked against 90 days of real history,
the fleet median sat near the bottom of that band, which would have painted half the
roster as under-used. A band computed from percentiles at runtime is no better: it
always manufactures an outlier, even on a perfectly fair fleet. The findings rules above
are the replacement — each pairs a relative comparison with an absolute floor, and each
can stay silent.

**Why the numbers may differ from the dispatch board.** Only legs with status
`completed` (excluding cancelled reservations) count as work. Past-dated legs stuck
`in-progress` under-count real work until closed out — that close-out hygiene is its own
task and predates this page.
