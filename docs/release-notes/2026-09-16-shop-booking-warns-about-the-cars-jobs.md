---
date: 2026-09-16
audience: Dispatchers
title: Booking a car into the shop now warns if that car is already working
---

# Booking a car into the shop now warns if that car is already working

## Send this to the team

> Hey team — Fleet can no longer book a car into the shop on a day it is
> already carrying jobs without saying so.
>
> 1. Pick a car and a window in **When can I take a car down?** (or on the
>    **Outlook**). A line now appears under the window before you press
>    anything: *"#003 has 8 trips Wed 16 Sep — 8 jobs that would have to move
>    to another car. Neuma is on it."*
> 2. Press **Book** on a day like that and it asks once more before saving.
>    Press it again and it books — the system tells you, you decide.
> 3. Green line means that car has nothing of its own that day.
>
> This applies everywhere a car goes off the road: the Desk, the Outlook, the
> car's own page, and moving a shop slot onto a different day.
>
> Nothing changed about how the car comes back, and Fleet still cannot move
> anybody's job — it can only tell you how many would have to be moved.

---

## Behind the scenes

**Where it lives:** Fleet → **Desk** → *When can I take a car down?*, Fleet →
**Outlook**, and the car's own page.

**Why:** a car was booked into a Wednesday shop window while it carried seven
assigned trips that Wednesday, with no warning on any screen. The check that
existed asks whether the **fleet** goes short — and pulling one of five
Sprinters never does — so it stayed quiet. The question nobody was asking was
about that one car's own board.

**Where the guard lives:** in the save endpoint, not in a screen. Every path
that books a downtime shares it, so the Desk, the Outlook, the vehicle modal
and the edit-a-window flow all ask the same question; wiring it into one
screen is how the first version of this missed the other three.

**It informs, it does not refuse.** Same shape as the existing short-day check:
409 with a reason, saved on the second press. Nothing here reads machine-guessed
condition — it is the board's own assignment rows, which a person put there.

**What it counts:** this unit's chauffeur on each blocked day and that
chauffeur's trips, through the same job-end estimator **The day** uses. A day
with no chauffeurs on it anywhere says "not assigned yet" rather than reporting
zero trips — which is a different thing from a car that simply has nobody on it.

**Expect to be asked:**
- *"It says 8 jobs move but also 'no job gets farmed out'."* — Both are true.
  The other cars can cover the work; somebody still has to re-assign it.
- *"Why did it let me book it after warning me?"* — Because sometimes that is
  the right call. It makes sure you knew, then does what you asked.
