---
date: 2026-09-14
audience: Dispatchers
title: Fleet has its own desk — and you can tell it about a car without calling
---

# Fleet has its own desk — and you can tell it about a car without calling

## Send this to the team

> Hey team — there's a **Fleet** link in the top bar now. It's the fleet
> manager's desk, but it's for you too: open it (or tap **Fleet** on the
> planner's vehicle pool) and every car is listed with why it's down and when
> it's due back. No need to call to ask.
>
> Two things you can do from a car's page:
>
> 1. **Report a problem** — a chauffeur says the brakes grind, the AC blows
>    warm, a warning light is on. Type it in, pick how urgent, and it lands on
>    the fleet desk straight away. Tick **Take it out of service now** if the
>    car must not go out.
> 2. **Take out of service** — same as before, but it now asks for what kind of
>    job it is and when the car is expected back, and it checks the coming days
>    and tells you if dispatch would be short a car on any of them.
>
> On the planner, a car that was due back but fleet hasn't confirmed yet shows
> a small amber **back? not confirmed** tag. You can still assign it — it's a
> heads-up, not a block.
>
> Nothing about assigning changed: a car that's out of service still shows red
> in the pool, still refuses the drop, and you can still override it if you know
> it's back.

---

## Behind the scenes

**Where it lives:** top bar → **Fleet** (every dispatcher and founder). The
fleet manager's own login lands there and gets a short bar of just the fleet
pages: Desk, Vehicles, Outlook, Report. The old Fleet entry under Analytics is
gone; the vehicle table is the **Vehicles** tab.

**Why:** we hired a fleet manager. Until now "out of service" was one window per
car with no history, nothing said which days were safe for a shop visit, and the
only way a chauffeur's "it's making a noise" reached fleet was a phone call.

**What changed underneath:**
- Out of service is now a **downtime record** per shop visit — kind, reason,
  shop, planned dates, the day it actually came back. A car can have a repair
  this week and a tyre slot next month at the same time. The two old windows on
  #008 and #11 were carried across as closed history.
- On the expected-back date the car goes back in the pool **by itself**; fleet
  then confirms it's back so the record closes with the real date. If they
  forget, you see the amber tag and fleet gets nagged — nobody loses a car.
- The **Outlook** turns booked trips into busy/quiet days per vehicle type, using
  the same in-flight arithmetic the planner uses, plus what each weekday has
  actually run lately. That's what "Short on Saturday" is based on.
- Fault codes from the cars now show as actual codes ("P0420 — catalyst") on the
  car's page, with how long they've been lit.

**Expect to be asked:**
- *"Do I have to do anything different when a car breaks?"* — Only if you want
  to: report it from the car's page instead of texting. Marking it out of service
  works the same way it always did, one screen over.
- *"It said 'short on Saturday' — can I still take the car down?"* — Yes. Tick
  the box and it saves. It's telling you, not stopping you.
- *"The planner shows 'back? not confirmed' — can I use the car?"* — Yes.
  Fleet hasn't signed it back in yet. If it's actually still at the shop, the
  fleet page for that car has the shop and the reason.
- *"Where did Fleet go from the Analytics menu?"* — It's a top-level link now.
- *"Will the fleet manager get texts?"* — Only if that's switched on. When it
  is, they get one text when someone reports a problem, and one in the morning
  listing what needs attention. Nothing texts chauffeurs.
