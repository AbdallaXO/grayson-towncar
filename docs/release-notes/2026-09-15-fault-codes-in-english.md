---
date: 2026-09-15
audience: Dispatchers
title: Fault codes now say what's wrong and whether the car can go out
---

# Fault codes now say what's wrong and whether the car can go out

## Send this to the team

> Hey team — the fault codes on the **Fleet** desk are in English now.
>
> Every car with a lit code carries one of three answers, and it's the first
> thing on the row:
>
> - **Fine to run** — deal with it whenever.
> - **Runs, but book the shop** — it'll drive today; get it in this week.
> - **Don't send it out** — don't put a guest in it until someone looks.
>
> Tap a code and you get what it actually means, what happens if it's left, and
> the car's own wording underneath — that last bit is what you read down the
> phone to the shop.
>
> One thing to know: a car can now move up the list on the strength of what the
> code means, not just how the car flagged it. #10's turbo fault came in as an
> ordinary warning and now sits at the top as **Don't send it out**.
>
> Nothing has been taken off the road automatically and nothing will be. This
> only changes what the screen says. If a car must not go out, someone still has
> to press **Take off road** — same as always.

---

## Behind the scenes

**Where it lives:** the **Do now** rows on the Fleet desk, and the detail behind
each code chip.

**Why:** three of the five rows on a normal morning said "read them before the
next assignment" and then showed the ECU's own string —
*"Reductant Injection Valve Circuit Range/Performance Bank 1 Unit 1"*. That is an
instruction the fleet manager cannot follow. He is not a mechanic, and the only
question he is actually asking is whether a guest can get in the car.

**How it decides**, in order:

1. An explicit entry written for these vehicles. Every code the fleet has ever
   thrown is in the table, plus the common neighbours of each family — a fault
   arrives at 6 AM and the answer has to already be there.
2. Failing that, the shape of the code itself. OBD-II codes are systematic, so an
   unrecognised one is still confidently "an ignition misfire" or "brakes,
   steering or suspension".
3. Failing that, it says it doesn't know and shows the car's own wording. "Ring
   the shop with this code" is a better morning than a confident wrong answer
   about a car carrying guests.

**Severity is not the verdict.** A known answer is never softened by the
vehicle's flag — the table was written for these vehicles, the flag is generic —
but an unrecognised code that the car calls critical is treated as serious.

**Four DEF codes are still one problem.** The Sprinters throw them in packs, and
the row says so rather than reading as four faults.

**It informs, it never gates.** The downtime ledger remains the only thing that
removes a car from the planner's pool, because a person sets it by hand. An
automatic readiness gate was built once and pulled for false positives; this is
not that.

**Editing it:** the table is plain data at the top of
`dispatching/fault_codes.py`, one entry per code, in ordinary sentences. Adding a
code is adding a dictionary entry — no migration, no deploy ceremony.

**Expect to be asked:**
- *"Why did #10 jump to the top?"* — Its turbo fault means the van is down on
  power and can drop into limp mode. Merging onto I-4 with guests is the case to
  avoid, so it outranks a code the car happened to flag the same way.
- *"It says don't send it out — is it blocked?"* — No. It is a sentence on a
  screen. Use **Take off road** if it genuinely must not go.
- *"A code shows no explanation."* — Then it isn't in the table yet. The car's
  own wording is still there; tell whoever maintains the system and it's a
  one-line addition.
