---
date: 2026-08-27
audience: Dispatchers
title: A trip with a card on file no longer looks the same as one nobody paid for
---

# A trip with a card on file no longer looks the same as one nobody paid for

## Send this to the team

> Hey team — the board now tells you apart the two kinds of unpaid trip.
>
> A trip where we already hold the guest's card shows a small gold card mark and
> stays calm. A trip where nobody has given us a card keeps the dollar sign and the
> amber ring. One is a click to collect; the other is a phone call. Hover either one
> and the popup says which.
>
> On the reservations list, "Saved Cards" is now "Unpaid — Card on File" and shows
> only trips that still owe us. It was listing everything ever booked with a card,
> including trips paid for months ago — that's why the number looked so big.
>
> And in the payment screen, using a saved card now offers **Trip Fare** as the
> charge type, filled in with what's owed. Before, collecting a fare meant picking
> "Additional Charge → Extra Stop", and that's what printed on the guest's receipt.
>
> What did NOT change: paid trips look exactly as they always did, nothing that was
> blank starts showing a mark, and auto-assign still skips the same trips it skipped
> yesterday.

---

## Behind the scenes

**Where it lives:** the schedule board and the capacity planner (the payment mark on
a trip), the reservations list ("Unpaid — Card on File"), and the dispatcher payment
screen (Charge Type)

**Why:** three complaints, one root cause — nothing in the product treated "card on
file" as its own state. The board only asked "is it paid?", so a card-on-file trip
got the full unpaid alarm. The list filter asked "was there ever a card?", which
matched every collected trip too. And the payment screen only offered charge types
for *new* money, so there was no way to name the fare itself.

**Expect to be asked:**
- *"The Saved Cards number dropped a lot."* — Right, and it's correct now. On the
  default view it goes from 137 rows to 83; all-time, 280 to 92. The old number was
  counting trips already paid for.
- *"Revenue changed too."* — Yes, and it was wrong before, not now. A trip paid in
  two instalments was having its full price counted twice. The total on that page was
  overstated by about 10%.
- *"Does the gold card mark mean it's paid?"* — No. It means we hold the card and can
  charge it. The money is still owed until someone collects it.
- *"Will auto-assign now assign these?"* — No. That toggle is untouched and behaves
  exactly as it did. Worth deciding separately whether it should.
