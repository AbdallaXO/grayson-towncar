---
date: 2026-08-31
audience: Dispatchers
title: Local trips price themselves, extra stops show up, and the rest say they need a price
---

# Local trips price themselves, extra stops show up, and the rest say they need a price

<!--
  Everything inside the block below gets pasted into the group chat as-is.
  Read docs/release-notes/README.md before writing it. Under ~150 words.
  No file names, no field names, no jargon. Say what to click, what is
  different, and what did NOT change.
-->

## Send this to the team

> Hey team — driver pay now works off zones, so a local trip pays the local rate whether or not that exact pairing was ever set up.
>
> Anything local — airport, Disney, Universal, I-Drive, the parks, hotel to hotel — is one zone. Sanford and Port Canaveral are the other. Trips inside the local zone pay the local rate, trips touching Sanford or the port pay the higher one. Championsgate, Flamingo Crossings and Clermont to the port keep their own prices.
>
> On a driver's pay page you'll now also see any extra stops on a trip, with what the guest was charged, so you can pay for them in the Additional box.
>
> Trips we genuinely can't price — Tampa, an address we don't recognise — show a red NEEDS PRICE instead of a number. Before, those quietly showed a price borrowed from the booking, and it was usually wrong.
>
> And a trip that already has a price but can't be checked — usually because one of the addresses is somewhere we've never listed — now says so instead of passing quietly. That's how a run out to Sebastian sat at the local $25.
>
> There is also a new Payroll Run screen under Drivers: every in-house driver on one page, whoever needs a decision at the top, and a Record statement button on each row. “Review every trip” opens the lot — every trip for every driver, with the amounts editable where you sit. You still approve each driver yourself, you just stop opening seventeen pages to do it.
>
> The Pay Rates page is where you change any of it now — zone prices, which zone a place is in, and the few trips that have their own price. You no longer need the admin for rates.
>
> Nothing changed about assigning drivers or recording a payment, and a price you type is never overwritten.

---

## Behind the scenes

**Where it lives:** the new Payroll Run screen, the driver pay page, and the Pay Rates page

**Why:** a Clermont run to the cruise port paid $25 because the price was borrowed off the booking's airport-to-Disney rate. Two cars ran that trip; one was corrected to $55 by hand, the other was paid eight minutes before anyone noticed. About 150 trips a month were being priced that way. Zones fix the cause rather than the symptom — sixteen of the nineteen routes already followed the zone rule, so this is mostly writing down what we were already doing.

**Expect to be asked:**
- *"Where do I change the local rate?"* — Pay Rates page, the Zone prices grid at the top. Change one number and every local trip follows it.
- *"Championsgate says Local but it is $35 from the airport?"* — right, and the page says so under the zone. It is local everywhere except the airport run, which is listed as an exception.
- *"Clermont is Local? It's miles out."* — to the airport it has always been paid the local rate, nine times out of nine. The long one is Clermont to the cruise port, and that is listed at $55.
- *"A trip has its own special price, will the zone override it?"* — no. A route with its own price always wins.
- *"Why does this address say NEEDS PRICE?"* — we don't recognise it. Mostly private homes and a few hotels we haven't listed. Tell Abdalla the property and it can be added to a zone once.
- *"Do I still add the late-night bonus by hand?"* — no. If a pickup moves into or out of the late-night window, the bonus follows it.
