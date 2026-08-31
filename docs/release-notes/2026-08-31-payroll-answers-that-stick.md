---
date: 2026-08-31
audience: Dispatchers
title: A price you give once is now remembered, and shop stops stop asking
---

# A price you give once is now remembered, and shop stops stop asking

<!--
  Everything inside the block below gets pasted into the group chat as-is.
  Read docs/release-notes/README.md before writing it. Under ~150 words.
  No file names, no field names, no jargon. Say what to click, what is
  different, and what did NOT change.
-->

## Send this to the team

> Hey team — the payroll page asked for the same answers every week. It won't now.
>
> When a trip needs a price, the row has a **make this price stick** button. Put the price in, and if it's a hotel or address we've never listed, give it a name and pick its zone. That trip is priced and so is every future one through that place. If it's a pair that just costs more, tick the box and it becomes the price for everyone, both ways.
>
> Publix and Walmart stops no longer ask to be dealt with — they show on the row and pay nothing, same as always. A second **drop-off** still asks, because that one you get paid for.
>
> A trip with a price we can't confirm now says which: "we haven't listed that address, price looks normal" is quiet, but a local price on a run to the port or an eighty-mile drive shouts.
>
> Nothing changed about how you assign drivers or record a statement.

---

## Behind the scenes

**Where it lives:** the Payroll Run screen and the Pay Rates page

**Why:** the 30 August run put about eighty items in front of one person and roughly a dozen genuinely needed him. The rest were the same shapes over and over — an unlisted Disney property priced perfectly correctly, a grocery stop nobody pays for, and a price answered last week that had nowhere to live. Every one of those is now a root fix rather than a weekly answer.

Three things underneath, none of them visible:

- **The auto-fill gate was wrong.** It needed all four pay fields empty, so any trip that picked up a tip before it had a rate could never be priced again — permanently reading "needs a price" on a route we run daily. It now looks at the rate alone and fills each field only if that field is empty. **2,329 legs** were stuck like that.
- **Recalculate refused any trip without a route row** — exactly the trips zones exist for — and nulled attributed tips on the way past. Both fixed.
- **A known endpoint now sets a floor.** Nothing touching the port zone has ever cost under $40, so $25 on a port run is money even when the other end is unlisted. And a price in the local band on an eighty-mile drive is flagged off the real cached distance.

**Expect to be asked:**
- *"Where did the grocery-stop warnings go?"* — still on the row, just not as something to action. They never paid.
- *"It says the address isn't listed but the price is fine?"* — that's the point; it's telling you, not asking you. List the place when convenient and it stops.
- *"Does the button change anything for other drivers?"* — placing an address, yes: every trip through it prices from then on. That is what it's for.
