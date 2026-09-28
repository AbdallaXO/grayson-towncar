---
date: 2026-09-17
audience: Dispatchers
title: The $20 question comes up while you're still on the trip
---

# The $20 question comes up while you're still on the trip

## Send this to the team

> Hey team — when you move a pickup into the 10 PM–6 AM window, you'll now get a
> box about the $20 right after it saves. Your time change goes through either
> way; the box is just the money question.
>
> Three answers, and all three stick:
>
> 1. **Charge $20 now** — bills the card on file and emails the guest the new
>    total. That's the whole thing, nothing else to press.
> 2. **Already collected** — it came in on the balance, in cash, or in your price
> 3. **Waive it** — not charging this one, recorded under your name
>
> Whichever you pick, that trip stops asking. Until now only charging settled it,
> so a trip you'd already dealt with came back the next day. One trip asked six
> times over two days.
>
> Close the box and nothing is lost — the time change is saved and the $20 shows
> up on the board as a task, same as before.
>
> No card on file? The charge button is greyed out. Take payment your usual way,
> then press "Already collected".
>
> You'll also see a small gold **After-hours fee** block on the trip, listing what
> was decided, by who, and when. It's marked *staff only* and chauffeurs can't see
> it — that's the point. Their notes are unchanged and still show the gratuity.
>
> Nothing changed for daytime pickups, and nothing changed if the $20 is already
> on the booking — you won't be asked at all.

---

## Behind the scenes

**Where it lives:** two places a dispatcher changes a pickup — the "match pickup
to flight" button, and editing the time on the trip card.

**Why:** the fee was filed as a task instead of decided at the keyboard. Leg 36690
is the worked example — a real MCO → Animal Kingdom Lodge trip:

| when | what |
|---|---|
| Aug 24 | Booked at $230, exactly the standard round-trip rate. Pickup 10:49 PM — already after hours. Fee never added. |
| Sep 15 08:26 | Pickup matched to flight; task raised the same minute. |
| Sep 15 08:28 | Closed. No note, no charge. |
| Sep 15 08:51 | Guest sent their confirmation. |
| Sep 16 | Raised and closed four more times, then cancelled. |

Six tasks, one trip, 39 hours, the same dispatcher each time, and the guest never
heard about it.

**The order matters.** The save happens first and the question comes after, so a
dispatcher can never lose a pickup change to a dialog they wanted to think about.
Dismissing it leaves the time changed and the fee flagged — the backstop runs
regardless. The wrong-day question still blocks, because that one decides which
*day* the trip runs and getting it wrong strands a guest at the curb.

The trip-card edit was the worse hole: it never raised the after-hours flag at
all, so a hand-typed 1:22 AM changed the time and said nothing, anywhere.

**Fee notes are off the driver's screen.** Settling used to append a line to the
leg's notes — which drivers read, on their board, their completed trips and their
weekly schedule. Our fee admin was showing to the chauffeur.

The trail now has its own block on the trip card, built from the staff activity
log and pinned per leg in one query. Deliberately a separate section rather than
folded back into the notes, so nobody can merge the two by accident: that field
goes to drivers. Gratuity stays the one money note a chauffeur sees, unchanged.

**Names, not roles.** "confirmed by dispatcher" is unattributable a week later,
which is exactly why five identical closes on leg 36690 told us nothing. Every
settle now records the person.

**Expect to be asked:**
- *"Does Charge actually take the money?"* Yes — card on file, and the guest gets
  an email with the new total. No second step.
- *"What if I close the box?"* The time change is saved and the $20 is on the
  board as a task. Nothing is lost.
- *"Does this catch every late trip?"* No. Two more places can still move a pickup
  quietly: the full Edit Reservation page (which now at least raises the task) and
  the Django admin. Neither shows the box yet.
