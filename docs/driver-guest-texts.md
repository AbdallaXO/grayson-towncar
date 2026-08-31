
# Driver → guest text templates

Every standard text a chauffeur can send from the driver app. **Edit the wording
here and hand this file back** — the IDs are what I'll match on, so keep them.

Generated from `drivers/client_messages.py`; verified byte-identical to what the
code renders. If you change this file, the code does not change until I apply it.

Rewritten 2026-08-31 — the previous version of this doc (and the code) named the
flight number and landing time, and quoted a carousel number. Guest-facing copy
no longer does either; see Design rules below.

## Slots

| Slot | Fills with | If missing |
|---|---|---|
| `{guest}` | Guest's first name, title-cased | `there` |
| `{driver}` | Chauffeur's first name, folded into an intro line | phrase becomes "I'm your Grayson Towncar chauffeur" |
| `{daypart}` | `morning` / `afternoon` / `evening`, from the BOOKED pickup time (before noon / before 5 PM / after) | `day` |
| `{time}` | Pickup time, e.g. `6:15 AM` | drops out — the sentence just says "your pickup" |
| `{airport}` | e.g. `Orlando International Airport` — departure copy only, no internal airport code | `the airport` |
| `{meet_point}` | Where to find the chauffeur at an arrival airport — see Meet points below | `the baggage claim area` |
| `{car}` | ` in a Chevrolet Suburban` — make + model, **never a colour** | omitted |
| `{terminal}` | `the Royal Caribbean terminal` | `the cruise terminal` |
| `{port}` | `the Royal Caribbean terminal at Port Canaveral` | `the cruise terminal at Port Canaveral` |
| `{pickup}` | The booked pickup address, verbatim | omitted |
| `{review_url}` | The Google review link | — |

## Meet points (airport-specific)

An arrival text names where to find the chauffeur. This is only ever as precise
as a VERIFIED set of directions for that specific airport — never invented.

| Airport | Meet point |
|---|---|
| Orlando International (MCO) | the baggage claim area on the 2nd floor, right at the bottom of the escalators by the information desk |
| Sanford International (SFB) | the baggage claim area on level 1, at the bottom of the escalator or elevator by the information desk |
| Melbourne (MLB), Lakeland (LAL), anything else | the baggage claim area *(no floor or landmark — none verified yet)* |

**Open question, needs your call:** `services/mco-terminal-c-transportation.html`
(an existing page on the site) describes MCO's Terminal C meet point as
**"Level 6 near the escalators and elevators… vehicle on Level 1"** — different
from the "2nd floor" instructions above. MCO has more than one terminal
building and the trip data has no field to tell them apart. Until this is
reconciled, every MCO arrival gets the "2nd floor" instructions regardless of
terminal.

## Design rules the copy has to keep

1. **Airport pickups are never curbside.** The driver walks in and waits inside.
2. **Never promise a colour.** Nothing in the system stores one.
3. **Never quote a flight number or landing time.** The driver tracks the flight
   internally, but the guest text doesn't say so.
4. **Never quote a departing flight TIME.** Only arrival times exist in the data.
   A departure text can only use `{time}`, the booked pickup.
5. **Never invent a meet point.** An airport without verified instructions gets
   the plain "baggage claim area", not a guessed floor or landmark.
6. Keep `{review_url}` on its own line, last.

---

## Airport arrival

`ARR` — triggers when: pickup is at any airport. (Whether or not a flight
arrival time is on file no longer changes the wording — both render the same.)

### ARR-WAY · On the way

```
Hello, {guest}! This is {driver} with Grayson Towncar. Welcome to Orlando — I hope you had a great flight.

Please send me a quick message as soon as you get off the plane. I'll meet you in {meet_point}. I'll be holding a sign with your name.

I look forward to meeting you shortly!
```

<details><summary>Reads as (MCO)</summary>

> Hello, Jane! This is Marcus with Grayson Towncar. Welcome to Orlando — I hope you had a great flight.
>
> Please send me a quick message as soon as you get off the plane. I'll meet you in the baggage claim area on the 2nd floor, right at the bottom of the escalators by the information desk. I'll be holding a sign with your name.
>
> I look forward to meeting you shortly!

</details>

<details><summary>Reads as (SFB)</summary>

> Hello, Jane! This is Marcus with Grayson Towncar. Welcome to Orlando — I hope you had a great flight.
>
> Please send me a quick message as soon as you get off the plane. I'll meet you in the baggage claim area on level 1, at the bottom of the escalator or elevator by the information desk. I'll be holding a sign with your name.
>
> I look forward to meeting you shortly!

</details>

### ARR-LOC · On location

```
Hi {guest}, I'm here at {meet_point}. I'll be holding a sign with your name.

See you shortly!

— {driver}, Grayson Towncar
```

<details><summary>Reads as (MCO)</summary>

> Hi Jane, I'm here at the baggage claim area on the 2nd floor, right at the bottom of the escalators by the information desk. I'll be holding a sign with your name.
>
> See you shortly!
>
> — Marcus, Grayson Towncar

</details>

### ARR-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Enjoy your stay!

{review_url}
```

<details><summary>Reads as</summary>

> It was a pleasure driving you today, Jane. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Enjoy your stay!
>
> https://g.page/r/CRWIXii71sLGEBM/review

</details>

---

## Airport departure

`DEP` — triggers when: drop-off is at an airport and the pickup is not.

### DEP-WAY · On the way

```
Good {daypart}, {guest}! This is {driver} with Grayson Towncar. I'm on my way for your {time} pickup from {pickup} to {airport}.

I'll send you a quick message as soon as I arrive. I look forward to seeing you shortly!
```

<details><summary>Reads as</summary>

> Good morning, Jane! This is Marcus with Grayson Towncar. I'm on my way for your 6:15 AM pickup from The Ritz-Carlton Orlando, Grande Lakes to Orlando International Airport.
>
> I'll send you a quick message as soon as I arrive. I look forward to seeing you shortly!

</details>

### DEP-LOC · On location

```
Good {daypart}, {guest}! I've arrived at {pickup} and I'm outside for your pickup.

Just send me a quick message when you're coming out, and I'll be ready to assist you with your luggage.

— {driver}, Grayson Towncar
```

<details><summary>Reads as</summary>

> Good morning, Jane! I've arrived at The Ritz-Carlton Orlando, Grande Lakes and I'm outside for your pickup.
>
> Just send me a quick message when you're coming out, and I'll be ready to assist you with your luggage.
>
> — Marcus, Grayson Towncar

</details>

### DEP-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Safe travels!

{review_url}
```

<details><summary>Reads as</summary>

> It was a pleasure driving you today, Jane. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Safe travels!
>
> https://g.page/r/CRWIXii71sLGEBM/review

</details>

---

## Cruise departure — from the airport

`CRU-AIR` — triggers when: drop-off is a cruise port and the pickup is an airport.

Uses the **Airport arrival** messages above, word for word — a cruise guest
arriving by air gets exactly the same on-the-way/on-location texts as a plain
airport arrival, right down to the meet point. Only the review closing differs.

### CRU-AIR-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Have a wonderful cruise!

{review_url}
```

---

## Cruise departure — from a hotel

`CRU-HOTEL` — triggers when: drop-off is a cruise port and the pickup is not an airport.

### CRU-HOTEL-WAY · On the way

```
Good {daypart}, {guest}! This is {driver} with Grayson Towncar. I'm on my way for your {time} pickup to {port}.

I'll send you a quick message as soon as I arrive. I look forward to seeing you shortly!
```

<details><summary>Reads as</summary>

> Good morning, Jane! This is Marcus with Grayson Towncar. I'm on my way for your 10:00 AM pickup to the Royal Caribbean terminal at Port Canaveral.
>
> I'll send you a quick message as soon as I arrive. I look forward to seeing you shortly!

</details>

### CRU-HOTEL-LOC · On location

```
Good {daypart}, {guest}! I've arrived and I'm outside for your pickup{car}.

Just send me a quick message when you're coming out, and I'll be ready to assist you with your luggage.

— {driver}, Grayson Towncar
```

<details><summary>Reads as</summary>

> Good morning, Jane! I've arrived and I'm outside for your pickup in a Chevrolet Suburban.
>
> Just send me a quick message when you're coming out, and I'll be ready to assist you with your luggage.
>
> — Marcus, Grayson Towncar

</details>

### CRU-HOTEL-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Have a wonderful cruise!

{review_url}
```

---

## Cruise return — off the ship

`CRU-OFF` — triggers when: pickup is at a cruise port.

### CRU-OFF-WAY · On the way

```
Good {daypart}, {guest}! This is {driver} with Grayson Towncar. Welcome back! I'll be your chauffeur from {port} today.

Once you're through customs and ready for pickup, please send me a quick message. I'll be nearby and ready to meet you.

See you shortly!
```

<details><summary>Reads as</summary>

> Good morning, Jane! This is Marcus with Grayson Towncar. Welcome back! I'll be your chauffeur from the Royal Caribbean terminal at Port Canaveral today.
>
> Once you're through customs and ready for pickup, please send me a quick message. I'll be nearby and ready to meet you.
>
> See you shortly!

</details>

### CRU-OFF-LOC · On location

```
Hi {guest}, I'm here at {terminal}{car}.

Once you're through customs and ready for pickup, just send me a quick message and I'll pull around to meet you.

— {driver}, Grayson Towncar
```

<details><summary>Reads as</summary>

> Hi Jane, I'm here at the Royal Caribbean terminal in a Chevrolet Suburban.
>
> Once you're through customs and ready for pickup, just send me a quick message and I'll pull around to meet you.
>
> — Marcus, Grayson Towncar

</details>

### CRU-OFF-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Welcome back!

{review_url}
```

---

## Charter / hourly booking

`CHARTER` — triggers when: the booking carries an hourly / as-directed stop.

### CHARTER-WAY · On the way

```
Good {daypart}, {guest}! This is {driver} with Grayson Towncar. I'm on my way for your {time} pickup and will be your chauffeur for the day.

I'll send you a quick message as soon as I arrive. I look forward to seeing you shortly!
```

<details><summary>Reads as</summary>

> Good morning, Jane! This is Marcus with Grayson Towncar. I'm on my way for your 9:00 AM pickup and will be your chauffeur for the day.
>
> I'll send you a quick message as soon as I arrive. I look forward to seeing you shortly!

</details>

### CHARTER-LOC · On location

```
Good {daypart}, {guest}! I've arrived and I'm outside for your pickup{car}.

Just send me a quick message when you're coming out, and I'll be ready for you.

— {driver}, Grayson Towncar
```

<details><summary>Reads as</summary>

> Good morning, Jane! I've arrived and I'm outside for your pickup in a Chevrolet Suburban.
>
> Just send me a quick message when you're coming out, and I'll be ready for you.
>
> — Marcus, Grayson Towncar

</details>

### CHARTER-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Take care!

{review_url}
```

---

## Point to point

`P2P` — triggers when: everything else — hotel to a venue, and similar.

### P2P-WAY · On the way

```
Good {daypart}, {guest}! This is {driver} with Grayson Towncar. I'm on my way for your {time} pickup.

I'll send you a quick message as soon as I arrive. I look forward to seeing you shortly!
```

<details><summary>Reads as</summary>

> Good evening, Jane! This is Marcus with Grayson Towncar. I'm on my way for your 7:00 PM pickup.
>
> I'll send you a quick message as soon as I arrive. I look forward to seeing you shortly!

</details>

### P2P-LOC · On location

```
Good {daypart}, {guest}! I've arrived and I'm outside for your pickup{car}.

Just send me a quick message when you're coming out, and I'll be ready for you.

— {driver}, Grayson Towncar
```

<details><summary>Reads as</summary>

> Good evening, Jane! I've arrived and I'm outside for your pickup in a Chevrolet Suburban.
>
> Just send me a quick message when you're coming out, and I'll be ready for you.
>
> — Marcus, Grayson Towncar

</details>

### P2P-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Take care!

{review_url}
```

---

## Not documented here: who gets these texts

Every in-house chauffeur AND every affiliate who drives his own jobs (a one-man
affiliate, not a re-dispatching operator) sees these buttons on his job card.
A true operator — who re-dispatches to his own drivers and never sees a guest
phone number — does not. See `drivers.views._attach_client_messages`.
