# The Day Manager — plain-English read-through

**A translation of `06_DAY_MANAGER.md` for reading, not a substitute for it.** Where this
disagrees with 06, 06 is correct. No file names, no field names, no engine words.

---

## The short version, in one breath

The tool you're asking for has already been built. It's been sitting in the system since
early August, switched on for exactly one person — you — and it has **never once been used
to move a trip on a real day.** Nobody has ever measured whether it's right. This project
isn't "build a day manager." It's "measure the one we have, fix the one broken clock it
shares with the board, stop three different alarms from arguing about the same trip, and
then hand it to the floor — but only the parts that pass a test."

---

## Section 1 — what "managing the day" actually means here

The day-before builder decides who works and who drives what. This is the next question:
once the day starts and reality drifts — a plane lands late, a job runs long, a driver calls
out — who notices, how fast, and what do they do about it?

The rule you set is the whole design: **never rebuild the day to fix one trip.** Find the
smallest safe set of changes, explain it in words, let a dispatcher say yes.

## Section 2 — what actually eats dispatcher time on the day

I ranked it by how much work it creates and how much damage it does.

1. **Chasing alarms that are mostly wrong.** The system today runs *three separate alarm
   systems* that each measure the same "can this driver make his next pickup?" question with
   different arithmetic. So the same turn can be green on the board, amber in the task list,
   and red on the GPS chip at the same minute. Dispatchers spend real time deciding which of
   the three to believe. **Roughly half of these alarms close with nobody moving anything** —
   the clock simply moved on, or the trip completed.
2. **A driver's previous job running long.** This is the real cause of lateness, and no plan
   can see it coming. Only the driver's own "picked up" tap and the live GPS know. This is
   where a day manager genuinely earns its keep.
3. **A later plane breaking the trip *after* the airport run.** About one trip in ten moves
   by half an hour or more after the night-before cut-off; for airport pickups it's one in
   five.
4. **Same-day new or uncovered work** — about thirteen trips a day get their first driver on
   the day itself.
5. **Driver call-outs and no-shows** — real, but the system keeps no record of them at all,
   so nothing can be measured. Handled by the same ladder of fixes, not by an alarm.
6. **Coverage** — deliberately *not* this tool's job. The data is clear that same-day
   reshuffling loses trips rather than winning them.

**Correction to your numbers.** Your "148 hand moves a day" is real, but it isn't a day-of
number — two-thirds of it happens one to three days out, which belongs to the builder. **On
the day itself it's about 53 driver changes across 26 trips.** And "97.5% of arrivals get
retimed" is real but is not hand work — it's about 36 clicks of the bulk flight-match
button, which is already automated.

## Section 3 — should this be a second engine, or the same one?

**Same organs, different tool.** The day-before builder takes minutes to think, plans the
whole day, and — because it works to a time limit — can give slightly different answers to
the same question twice. That's fine at 8pm the night before. It's useless at 2pm when a
plane just moved.

The tool that already exists thinks in **four seconds**, only looks at the rest of today,
never touches a trip already under way, gives the same answer every time, and proposes
rather than does. It already uses the same feasibility rules as the builder. That's the
right split: one set of rules about what's possible, two tools that ask at different speeds.

I checked whether it still runs fast enough on today's busier days: **yes — a fifth of a
second to just over one second**, even on a 186-trip Saturday.

## Section 4 — we tested it, and the answer was not what anyone expected

I replayed 28 real days through it — rewinding the system every 15 minutes to exactly what it
knew at that moment, running the tool, and checking each warning against what actually happened.
Nearly four thousand runs.

**The good news, and it's real:**

- **It is not noisy.** About **4 warnings on screen at a time** — not the 16 the earlier
  four-day sample suggested. Against roughly **71 alarms a day** from the system the floor uses
  now, that's a tenfold reduction in things to look at.
- **It is fast.** Six hundredths of a second, typically. It could run on every screen refresh
  and nobody would notice.
- **Most warnings arrive with a fix already checked.** Nine times in ten, the card comes with a
  specific move that has already been tested against the whole rest of the day.

**The bad news, and it's the important part:**

**It cannot predict lateness.** Not one warning type reaches the 7-in-10 bar you set. The best
genuine forecast is right about **4 times in 10**; the two types we most expected to work —
a plane moving, and a driver's tap showing he's running behind — are right **1 in 4** and
**1 in 3**.

And two warning types that *looked* good turned out to be cheating. They flag a trip that is
already visibly late, and then get marked "correct" when that same trip turns out to be late. A
tool that says "this trip is late" about a trip that is late isn't predicting anything — it's
reading back the screen. Once you strip those out, the honest ceiling is 4 in 10.

**What this means, plainly.** The tool is not an early-warning system and shouldn't pretend to
be — the whole industry of "this trip is at risk" alerts is where these systems lose people's
trust, and your own risk band already proved that. What it genuinely is: **the fastest, most
reliable second pair of hands you have once something has already gone wrong.** "George is 20
minutes overdue, and here is a checked way to cover his 4pm return" is worth screen space even
if George would have made it. "George is going to be late" is not, because it's wrong more often
than it's right.

So the recommendation is to ship the fix, not the fortune-telling: cards state what is happening
and offer a verified move, ranked by urgency, capped at five on screen — and the 70% bar moves
from the warnings to the fixes, where it belongs. That's a decision for you, and it's written up
with two alternatives in the main document.

## Section 5 — what we'd actually do, in order

**First, build the measuring stick — no product changes at all.** A script that replays
28 real days, scores every warning against what really happened, and writes down the honest
number for each type. Nothing ships until that number exists.

**Second, invisible repairs the floor never sees:**
- Fix the one broken clock. There's a rule the engine follows — when the next pickup is a
  fixed-time job, the driver isn't free until the earlier job actually ends — that the *board*
  doesn't follow. On real days that makes **about six turns a day look clean when they're
  actually impossible.**
- Point the three arguing alarm systems at one piece of arithmetic, so a turn gets one verdict.
- Start writing down what each warning said and what actually happened, so the accuracy
  number stays true after launch instead of being a one-off. **Done, and one thing about it
  is worth knowing.** The plan was to write a warning down when it appears on your screen —
  but the tool is switched on for two people, and nobody can tell how often either of you
  actually has it open. So it now writes itself down every three minutes whether anyone is
  watching. That matters for the choice in Section 4: "keep it hidden and just collect a
  month of evidence" is now a real option, because the evidence collects itself.
  (Three minutes rather than half an hour is not fussiness: at half-hourly checks the
  system would have caught fewer than half the short-lived warnings — and it would have
  missed them unevenly, so two warning types would be judged on half the evidence of
  the two they're ranked against.)
- Start keeping the GPS history, which today is thrown away.

**Third, the visible step:** open it to dispatchers — but only the warning types that passed
the test. The failures show as a quiet line with no suggestion, or not at all. Tasks and
flags get filed *from* this tool so the floor sees one verdict per trip instead of three.
Add a "this isn't a problem" button so disagreement becomes data.

**Fourth:** teach it to suggest calling in a bench driver on a free car — priced against
what farming the trip out would cost. It suggests; a human picks up the phone.

## Section 6 — what we're deliberately not building

No second engine. No whole-day re-plan. No clever "smallest set of changes" optimiser — real
day-of fixes are one move, or two drivers trading, sixteen times out of seventeen; anything
fancier is false precision. No new background process, no push alerts, nothing that contacts
a driver. And no early-warning guesswork: predicting trouble from the plan alone is right
about a third of the time, and that's where these systems lose people's trust.

## Section 7 — is this the right next investment?

**Yes for the cheap half, and the test made that clearer, not muddier.** Fixing the clock is
now a named, countable problem — five turns a day where the screen tells a dispatcher a driver
is fine and the engine says he cannot possibly make it, with names and times attached. Cutting
70 alarms a day down to about 4 is worth doing on its own. Neither of those depended on the tool
being able to predict anything.

What the test killed is the expensive half: there is no case for building a smarter predictor.
The data says prediction doesn't work here, and we now know that from 28 days of evidence rather
than from an argument.

And the bigger money is still on the other side: the day-before builder leaks roughly **five
trips a day** to affiliates that it shouldn't, against roughly **three** lost to same-day churn.
Do the cheap half here — then go back to the builder.

## Section 8 — where your plan needed correcting

**Every claim about the system itself checked out** — the tool, the four-second limit, the
broken clock, the three arguing alarms, the fact that it has never once been used on a real
day. All confirmed.

The counts of daily alarms did not — but that turns out to be a machine problem, not a plan
problem. **This plan was measured on your desktop; I checked it on the MacBook, and the
MacBook's copy of the data is thinner** — its alarm records, dispatcher flags and flight
records all trail off in mid-July. So the numbers are probably fine; they just can't be
re-checked here.

Two practical consequences:

1. **The first task of the project is to agree which copy of the data we measure from**, and
   to write that down. No baseline gets cut on the wrong machine.
2. **One real question stays open on either machine:** four fixes landed in late August aimed
   squarely at this alarm noise. If they worked, part of the problem this plan exists to solve
   may already be gone — which would make the "collapse three alarms into one" step smaller
   than planned. That's worth ten minutes to check before we size it.
