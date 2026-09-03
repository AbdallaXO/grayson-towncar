---
date: 2026-09-02
audience: Dispatchers
title: Addresses tidy themselves so the read-back reads properly
---

# Addresses tidy themselves so the read-back reads properly

## Send this to the team

> Hey team — type the pickup and drop-off however is fastest. The booking screen
> cleans up the spacing and the capitals for you, so the sentence you read back to
> the guest at the last step reads like a sentence.
>
> Type `PORT CANAVERAL` and it books as Port Canaveral. Type
> `1234 sand lake rd,orlando,fl 32819` and it books as
> 1234 Sand Lake Rd, Orlando, FL 32819. Extra spaces and stray commas come out on
> their own.
>
> It only fixes the way it is written. It never changes the place. If you write
> MCO Airport it stays MCO Airport — it will not swap in a longer name, and the
> price you get is the price for the place you typed.
>
> Nothing about how you pick a place changed. The suggestions list and the MCO
> button work exactly as they did.

---

## Behind the scenes

**Where it lives:** the trip step of the dispatcher booking screen, on both address
boxes.

**Why:** the last step prints the address in bold inside a sentence a dispatcher
reads out loud. An address typed with caps lock on reads as shouting, and one typed
with no space after the comma reads as a jumble. Tidying it on the way in fixes both
without anyone having to retype.

It deliberately does *not* rename anything. The address is the key the rate card is
matched on, so rewriting "MCO Airport" into the airport's full name would quietly
change which price the trip gets. Spacing and capitals are safe to touch; words are
not.

Only an address written in all one case gets its capitals redone. Anything with
mixed capitals was written that way on purpose — or picked off the suggestions list
— and is left exactly as it is.

**Expect to be asked:**
- *"It changed what I typed."* — Only the spacing and the capitals. If the words
  moved, that is a bug worth reporting.
- *"Do I have to type it a particular way now?"* — No. Type it any way you like.
