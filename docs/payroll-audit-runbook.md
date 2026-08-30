# Driver Payroll — Cowork Runbook

Read this in full before acting. Run Pass A straight through without stopping. Stop only
at the checkpoint at the end of Pass A, and only for the reasons under "When to stop."

This runbook got much shorter in August 2026. The system now works out what a trip should
pay and flags the ones it cannot. Your job is no longer to recompute every leg by hand — it
is to decide the handful the system has already picked out, and to notice when the *flags
themselves* look wrong.

---

## 0. What the buttons actually do (read this first)

There are two different things in this system called "payment." They are not the same and
only one of them touches money.

**Customer payment — real money.** `Process Payment` on a *reservation* page opens a Stripe
checkout session and charges a card. Never touch this during a payroll run.

**Driver payment — no money moves.** `Record statement` on the Payroll Run screen (and
`Record Payment & Send Statement` on the Driver Payments page) does exactly three things:
1. creates a `DriverPayment` row (a statement) plus one `LegPayment` line per leg,
2. flips those legs' `payment_status` from `unpaid` to `paid`,
3. optionally emails a statement.

No card is charged, no ACH is initiated, no bank API is called. Abdalla pays the drivers by
hand in Gusto afterwards, using the Gusto CSV exported from the same page.

It is a bookkeeping action, not a transfer — but it is a real state change, so it is not
free. It is reversible: any line can be voided (Void on the driver's statement detail page),
which reverses that leg back to unpaid and leaves an audit record. Do not treat it as
untouchable, and do not treat it as trivial. Process deliberately, once, at the end.

---

## Objective

For every in-house driver: look at the trips the system has flagged, fix what can be read
straight off the rate table, put the rest on a review list for Abdalla, and — after he
answers — process one statement per driver.

---

## Input: To Date

Abdalla gives you a **To Date** at the start of the conversation — "run payroll through
8/24," "through last Sunday," "through today." Parse it and echo it back before you touch
anything, e.g. "Running payroll through Sunday, August 24, 2026." If he doesn't give one,
default to the most recently completed Sunday and say plainly that you defaulted.

**There is no From date.** Scope is every completed, unpaid, in-house leg with
`pickup_date <= To Date` — whatever week it's from. This is deliberate: a leg flagged two
weeks ago and never resolved should still show up today, not get silently stranded by a
fixed weekly window. The Payroll Run screen marks any driver carrying trips more than two
weeks old with **old trips**; say so in the report when you see it.

---

## Where to work

**The Payroll Run screen** (`Drivers → Payroll Run`) is the whole run on one page: every
in-house driver, their trip count, their total, and how many of their trips need a decision.
Drivers needing a decision sort to the top. Set the **Through** date to the To Date first.

**Read the whole run in one request first.** Add `&show=all` to the Payroll Run URL and the
page lists *every* trip for *every* driver — date, route, base, tip, extra, total, and the
reason for any flag — instead of only the flagged ones. Take that in one pass before you act
on anything. It is also the fastest way for you to check the flags themselves: if a trip
looks wrong and is *not* flagged, that is a finding about the checks, and it belongs in the
report.

Use the Driver Payments page (`Open` on any row) only when you need to look into or edit one
driver's legs.

Founder and placeholder accounts are excluded from payroll automatically now — you should
not see Abdalla, Rayyan, or the unnamed placeholder account at all. If one appears, that is
itself a finding: report it, don't work around it.

---

## How pay is worked out now

You do **not** need to carry a rate table. In-house base pay is decided in this order, and
the first one that answers wins:

1. a `DriverPayRate` for that driver and route,
2. the `Route.inhouse_base_pay` for that exact trip — an **exception**, deliberately set,
3. the **zone price** for the two endpoints' pay zones,
4. nothing — the trip is flagged as needing a price.

A zone is a price tier, not a place: any two Local endpoints pay the Local rate whether or
not that pairing was ever entered. Zones and their prices are on the **Pay Rates** page.

Two things that follow from this:

- **"No route linked" is no longer a finding.** Most trips have no route row and price
  perfectly well from their zones. Do not flag a leg for lacking one.
- **A place can be in a zone and still have exceptions.** Championsgate is Local, but the
  airport run is $35 rather than $25. The Pay Rates page lists a place's exceptions directly
  under its zone; read them there rather than assuming one flat rate.

**Night bonus is automatic.** It is per driver (`Driver.night_bonus` — most are $10, at
least one is $20, some are $0) and the window is pickup at or after 22:01 or at or before
05:59. A 22:00 pickup gets no bonus, and that is correct. Since August 2026 the bonus also
follows a pickup that moves into or out of that window, so you should not be adding it by
hand. If you find a night pickup with no bonus, that is worth reporting — it means something
bypassed the recalculation.

---

## The three pay fields — compare like to like

| Field | What it is |
|---|---|
| `driver_base_pay` | the trip's rate, from the chain above |
| `driver_gratuity` | this leg's share of the **customer's** tip — **never** compare it to a rate |
| `driver_additional` | night bonus, wait time, extra stops |

`total_driver_pay` is their sum. Roughly one in four legs carries a gratuity share, so
comparing totals to a rate flags all of them as wrong when nothing is wrong.

---

## Hard constraints

- Only `status = completed` legs. Never touch a leg that isn't completed.
- Only `payment_status = unpaid` legs. The backend already refuses edits to paid legs and
  excludes them from processing — do not try to route around either refusal.
- Only in-house drivers. Affiliates are a different pay model and are out of scope.
- Scope is bounded by the **To Date** only — never invent a lower bound.
- **Never** use `force = true` on the recalculate action. Hand-typed amounts are now marked
  and protected from automatic recalculation, but `force` is still the one thing that can
  overwrite them.
- **Never email the driver.** Leave the statement-email option off. Drivers receive nothing
  from this run. Abdalla gets one report.
- Blast radius: if a single driver would need more than 15 corrections, or the corrections
  change more than $500 in total, stop and put the whole driver on the review list instead.

---

## Pass A — read the flags, correct what is unambiguous

Open the Payroll Run screen with the To Date set. Work down from the top, since the drivers
needing decisions are already there.

**For each flagged trip, the flag tells you what is wrong.** There are six:

| Flag | What it means | What to do |
|---|---|---|
| **needs a price** | No rate, no exception, and at least one endpoint is in no zone | **Flag for Abdalla.** Name the place. It usually needs adding to a zone once, on the Pay Rates page, and then it never recurs. |
| **$0 pay** | Priced, but at zero | Recalculate the leg (`force = false`). If it stays at zero, flag it. |
| **N extra stops, nothing added for it** | The trip had a stop the guest was charged for; the driver's extra-pay box is empty | **Flag for Abdalla** with the stop and the guest fee. What the driver gets for a stop is his call, not a lookup. |
| **holds more of the tip than its share** | A sibling leg on the same booking sits at exactly $0.00, so this leg absorbed the whole tip | **Flag for Abdalla.** Do not re-divide it yourself: moving money off this leg strands it, because the sibling is not re-saved. |
| **pays $X but the rates say $Y** | The stored amount disagrees with what the zones and exceptions work out today | If it is a clean 10x/100x slip, correct it. Otherwise **flag it** — it may be a deliberate override from before amounts were marked. |
| **night pickup, $X bonus not on it** | Pickup is inside 22:01–05:59 and the bonus is missing | Add the driver's bonus to the extra-pay box. Also report it: pay follows the window automatically now, so this means something bypassed that. |

**Fix yourself, only these:**
- Pay is blank or $0 and the trip is priceable. Use the built-in recalculation (driver-scoped,
  `force = false`) rather than typing amounts by hand.
- Base pay is off by an exact factor of 10 or 100 from what the chain says ($250 where it
  says $25). Correct to the computed figure.

**Flag, don't guess — anything else**, including:
- base pay that disagrees with the chain and isn't a clean 10x/100x slip (it may be a
  deliberate override — the leg will be marked as manually set if so),
- addresses that don't look like the trip that was priced,
- a completed leg missing pickup or dropoff data,
- two legs on one reservation that look like duplicates,
- a night pickup with no night bonus (see above — this should not happen any more),
- anything where the fix requires knowing intent rather than reading a number.

**Record what you changed.** Per driver: trips corrected, dollar delta, one line each.

When every driver is done, send Abdalla **one** email:

- Subject: `Payroll — through <To Date>`
- To Date, oldest outstanding trip's date, drivers reviewed, trips reviewed.
- Corrections made, grouped by driver, with the dollar delta per driver and in total.
- The review table (format below).
- Per driver: what the statement would come to if processed now.
- Any driver marked **old trips**, and how far back they go.
- Close with: "Reply with what to do about the review items and I'll process."

Then stop. **Do not process anything in Pass A.**

### Review table format

One table. Columns in this order: **Driver | Reservation | Issue | Pickup/Dropoff**.
Identify each leg by driver + reservation ID (e.g. "runner #13964") — no dates, no customer
names. Group each driver's rows together with a blank divider row between drivers.

---

## Pass B — process (only after Abdalla replies)

1. Apply his answers. If an answer is ambiguous, ask about that one item rather than picking
   a reading — you are already in a conversation with him at this point.
2. Anything he didn't resolve stays unpaid and rolls into the next run untouched — it will
   surface again automatically, since scope is "unpaid through To Date," not a fixed week.
3. Reload the Payroll Run screen with the To Date and read the totals fresh — do not process
   off Pass A's numbers.
4. Press **Record statement** once per driver, from the run screen, with the driver-email
   option off. The
   backend only picks up completed, unpaid legs with pay above zero, so a leg cannot be
   double-paid; even so, process each driver exactly once and confirm the trip count matches.
5. Confirm back to Abdalla: per driver, the statement ID, trip count, and total; then the
   grand total, and the count of items still parked for next week.
6. Tell him the Gusto CSV is ready. Do not download or send it yourself.

---

## When to stop and ask

Only for something that blocks the whole run: you can't log in, the site is down, the period
has no data at all, or the corrections you'd have to make exceed the blast-radius limits for
most of the roster. A single trip you're unsure about never stops the run — it goes on the
review table.

Two newer reasons to stop, because they mean the system is wrong rather than the data:

- **A founder or placeholder account appears in the run.** They are meant to be excluded.
- **More than about a quarter of trips are flagged "needs a price."** That is not a payroll
  problem, it is a zone or alias gap, and correcting it trip by trip is the wrong fix.

The one built-in checkpoint is the end of Pass A. That one is by design, not an interruption.
