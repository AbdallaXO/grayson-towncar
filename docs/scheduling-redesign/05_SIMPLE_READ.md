# Build 3b — plain-English read-through

**A translation of `05_BUILD3B_TICKETS.md` for reading, not a substitute for it.** Where this
disagrees with 05, 05 is correct — this is a guide to help you read it, not a replacement.

---

## Section 0 — what's already built (invisible groundwork)

Before writing this plan, two things got built first:

1. **The trip-building engine was pulled out of the web page code into its own reusable
   piece**, so it can be handed a "what-if" roster — a lineup of drivers that doesn't
   actually exist yet — and asked "if this were the lineup, how would today go?" Tested by
   running it on 10 real days and checking the result matches the live system exactly. It did.
2. **The three car-sharing rules got moved into one file** so they're easy to find and compare,
   without changing what any of them actually decide (that's a separate open question, §9.1 —
   which we already resolved together).

## Section 1 — the proof this project is worth doing, and where the value actually is

We tested the **existing** trip-assignment logic — the part that decides who drives what — by
wiping 10 real past days and letting it rebuild them from scratch, using the same drivers your
team had already chosen. **It matched your team's real results, and actually beat them on
hours** (nobody over 15 hours, versus about 2 people a day on your real boards).

**What this proves:** the part of the system that assigns individual trips already works. We
don't need to rebuild that. **The only real gap is deciding who works at all, which car they
get, and where the splits happen** — the part nobody's automated. That's the entire target of
this build.

One more number that shapes everything downstream: testing one "what-if" roster on one day
takes the engine **6 seconds on average, up to 15 on the busiest days.** That number decides
how many different lineups the tool can afford to try before it has to give you an answer.

## Section 2 — the actual decision-making logic (the core of the build)

This is the heart of it. Three things it decides, always in this order — it can never trade one
for the one above it:

1. **First: use as few drivers as possible without covering fewer trips than your team already
   covers today.** Not "the fewest drivers period" — the fewest *that don't cost you coverage*.
   This is the rule that makes "leave three people off" a valid, intended answer.
2. **Second, among plans that tie on that: cover more trips / spend less on affiliates.**
3. **Third, only to break remaining ties: make the day nicer** — fairer workloads, less risky
   handoffs, fewer long idle gaps. This is the only place "preferences" get weighed against each
   other with adjustable numbers — the first two steps are hard rules, not preferences.

**Some things it will never do, no matter what:** propose an impossible back-to-back trip,
schedule anyone past your legal hour limits, double-book a shared car, or suggest a handoff
timed so tight it's flagged red. If a possible plan breaks any of these, it's thrown out before
it's even scored — not penalized, discarded.

**How it actually searches:** it starts with the roster your team picked. Then it asks, one at a
time, "what if I pulled this specific person off — does the day still work?" If yes, it keeps
that version and asks again. It only ever removes people, never adds someone back mid-search,
which is what makes its reasoning explainable: "these three came off, in this order, here's
what letting each one go actually cost."

**Which specific trips get farmed out, for a given lineup, is decided by the trip-assignment
logic that already exists today** — and that logic already knows to protect departures over
arrivals when it has to choose (see the note below).

**On your farm-out question:** already handled. The existing engine already treats departures
and returns as the trips to protect, and treats arrivals as the ones to farm first when
something has to give — almost exactly your Publix-stop-arrival vs. 30-minute-departure example.
It's not literally comparing dollars-and-minutes the way you described it, but it produces your
answer today. I've noted this in the document so it doesn't get lost, and flagged that a future
review should check it against more real examples.

## Section 3 — the one dial you'll actually see and touch

A slider in Day Setup: **"Allow up to [0/1/2/3] more trips to go to affiliates to get a
better day."** Starts at zero. At zero, the tool will *never* suggest a plan that farms out more
than what your team already achieves today — that's the built-in floor.

If you ever move it up, it explains the trade in real terms — dollars and hours, never a score
or a technical term. Example: *"Leaving Marcus off farms out 2 more trips (about $142) but
shortens three other people's days by 1.5 hours."* You decide if that's worth it.

## Section 4 — a built-in sanity check, run once before anything ships

Before the "which drivers to use" logic is trusted, it gets tested against random noise: does
trying different-sized lineups actually produce a *meaningfully different* answer, or is the
difference just random luck in how the engine happens to place trips? If the signal isn't real,
**that whole "pick the fewest drivers" feature gets cut from this version** and the tool falls
back to just optimizing trip placement and splits at whatever headcount you already chose. This
protects you from being handed a fake-precise recommendation.

## Section 5 — how it actually runs, technically

It never makes you wait at your screen — building a full plan can take a few minutes, so it runs
in the background. You click "Build a plan," walk away, and come back to a result stamped with
when it was actually built (so you know if it's stale because new bookings came in since). It
never writes anything to the real schedule by itself — it only reads and proposes.

## Section 6 — how a finished plan actually gets applied

You still click Apply, exactly like today — nothing here changes that. Two honest limits in this
first version, both because a piece of the underlying system doesn't support them yet:

- **"Leave these drivers off" can't be applied with one click yet.** The tool will show you
  who to leave off; you manually untick them and Apply, the same motion as today.
- **If a day is being held/reviewed in the sandbox**, the tool refuses to build a plan for it
  and tells you why, rather than risk conflicting with what's being reviewed there.

## Section 7 — the pass/fail bar before this ships at all

Tested against 10 real days, it must, every time:
- Cover at least as many trips as your team's real, hand-finished board did that day
- Use no *more* drivers than it takes to hit that coverage
- Produce zero impossible back-to-back trips
- Put nobody over 15 hours
- Create zero new rest-rule violations
- Show every crunch-day hour exception with its price, never hidden
- Never put more than 2 drivers on one car in a day
- Never propose a red-flagged handoff
- Finish in a reasonable time, or clearly say "still working" rather than hang

Two of these matter most: **coverage at least matching your team**, and **using no more drivers
than needed**. A version that's great on the first and ignores the second hasn't actually built
what this project is for.

## Section 8 — hard walls (things it will never do, by design)

No auto-apply, ever. No writing to the schedule on its own. Nothing that contacts or messages
drivers. No changes to which affiliate gets picked. No hiring/buying-cars advice (that's the
separate, later phase). No the trip-simulator tool (deliberately parked for later). No new
background jobs beyond what already runs. No reviving the old GPS tracking. No touching pricing
or payments.

## Section 9 — leftover open questions

- **§9.1, the car-sharing rule question — already resolved** in our conversation: you
  ground-truthed it against real days and we shipped the fix (the engine now uses its own
  65-minute dial, separate from the 120-minute one the warning and second-shift tool use).
- **A couple of small, low-stakes items** — a warning tool reading a stale number (easy fix,
  makes it more conservative, no real risk), and one thing that looks like an outright bug
  where a double-booking check can silently switch off in a specific edge case. Neither blocks
  anything; worth a quick "yes, fix it" whenever convenient.
- Two small pre-existing display quirks noticed along the way, not caused by anything we built,
  low priority.

## Section 10 — build order

Build the pass/fail test first (so every later piece can be checked as it lands), fix a small
duplicate-logic issue between two existing tools, run the sanity check from Section 4, then build
the actual decision logic, then the slider, then the background-running piece, then the
apply/refuse behavior — each one checked before moving to the next.

---

*Read 05 itself for the exact numbers, code locations, and technical definitions — this is the
map, not the territory.*
