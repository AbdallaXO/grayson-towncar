---
date: 2026-09-16
audience: Dispatchers
title: The walk-around is one tap for a good car, and the round reads as a week
---

# The walk-around is one tap for a good car, and the round reads as a week

## Send this to the team

> Hey team — the inspection screens have had a proper going-over.
>
> 1. Open a car to inspect it. The **plate** is now in the header, so you can
>    check you're at the right car before you start.
> 2. Walked it and everything's fine? Tap **All 18 good** once at the top.
>    That's the whole form. Then flag anything that isn't.
> 3. A note and a camera button sit behind the **+** beside each item, and open
>    by themselves the moment you mark something **Problem**.
> 4. **Save and next car** at the bottom takes you straight to the next one due.
>
> Two checks changed wording: *Washed, presentable for a guest* is now **Clean
> exterior**, and the brake test drive is now **Brake fluid reservoir level**
> and **Brake life — pads and discs** — two things you can actually look at
> standing on the lot.
>
> On the round itself, the cars you've **already walked come first** with what
> you found written out in full, and every card says how long since that car was
> last seen. Nothing is pre-ticked: the form still records only what you answer.

---

## Behind the scenes

**Where it lives:** Fleet → **Inspections**, and any car opened from it.

**Why:** the realistic answer on a walk-around is every item good and one
exception, and the form charged eighteen deliberate taps for it — on 29-pixel
targets, one-handed, in a car park. It also rendered the browser's own file
picker on every row, so the page read *"No file chosen"* seventeen times, which
looks like an error state. The round showed the same five cars twice and stamped
**DUE** on eighteen of nineteen cards, which made the word invisible.

**Measured:** the form was 3,989 pixels tall on a phone (4.7 screens) and is now
3,143 (3.7). Tap targets went from 56×29 to 98×44. The real saving is the
interaction, not the scroll: one tap instead of eighteen.

**"All good" is a tap, not a default.** The form arrives blank on purpose — a
pre-ticked form records a check nobody made, and this whole subsystem's rule is
that it says so when it does not know.

**Expect to be asked:**
- *"Where did the note boxes go?"* — Behind the **+** on each row. Marking an
  item Problem opens it for you.
- *"Why is the brake test drive gone?"* — Replaced with two checks that can be
  made on the lot. Any older inspection that answered the old question still
  reads back exactly as it was saved.
- *"Why does every card say Never walked?"* — Because they have not been. It
  will start reading "Last walked 2 weeks ago" as the rounds build up.
