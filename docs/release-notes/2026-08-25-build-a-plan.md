---
date: 2026-08-25
audience: Dispatchers
title: Day Setup can now build you a plan for the whole day
---

# Day Setup can now build you a plan for the whole day

<!--
  NOTE: this ships SWITCHED OFF. Abdalla turns it on when he's ready — send
  the message below the day he flips it, not before.
-->

## Send this to the team

> Hey team — Day Setup has a new "Build a plan" button. It looks at the whole
> day and tells you if a different car setup would cover more trips or send
> less money to affiliates.
>
> 1. Open Day Setup for the date, set your crew like always.
> 2. Hit "Build a plan" — it thinks for a few minutes, you can close the
>    window and come back.
> 3. It shows what it found, in plain terms: "these two should swap cars, it
>    keeps another trip in-house." If your setup is already the best, it says
>    so.
>
> It never changes anything on its own — you still make every change yourself
> and hit Apply, same as always. If it suggests running someone past 13.5
> hours, it shows that as a choice with the dollars, never as a default.
>
> Assigning trips, the schedule board, and the driver app all work exactly the
> same as yesterday.

---

## Behind the scenes

**Where it lives:** the Day Setup window on the capacity planner, bottom
panel. Only visible when the master switch is on (Scoring Settings → 
Day-Builder).

**Why:** we proved the trip-assignment engine already places trips about as
well as a hand-finished board — the money is in which driver sits in which
car. This searches those setups automatically instead of by feel.

**Expect to be asked:**
- "It said my setup was already best — is it broken?" No, that's a real
  answer. It only speaks up when a change actually wins something.
- "Does it call drivers or change the schedule?" Never. It proposes; you
  apply. Nothing is written until you make the change yourself.
- "There's a dial about extra farmed trips" — leave it at 0 unless you're
  deliberately trading a farm-out for shorter days; it explains the trade in
  dollars when it matters.
