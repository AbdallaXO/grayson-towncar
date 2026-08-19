
# Driver → guest text templates

Every standard text a chauffeur can send from the driver app. **Edit the wording
here and hand this file back** — the IDs are what I'll match on, so keep them.

Generated from `drivers/client_messages.py`; verified byte-identical to what the
code renders. If you change this file, the code does not change until I apply it.

## Slots

| Slot | Fills with | If missing |
|---|---|---|
| `{guest}` | Guest's first name, title-cased | `there` |
| `{driver}` | Chauffeur's first name | phrase becomes "your Grayson Towncar chauffeur" |
| `{daypart}` | `morning` / `afternoon` / `evening`, from the pickup time | `day` |
| `{time}` | Pickup time, e.g. `6:15 AM` | drops to "your pickup" |
| `{airport}` | e.g. `Orlando International Airport (MCO)` | `the airport` |
| `{flight}` | e.g. `Delta 1423` | becomes "your flight" |
| `{landing}` | ` (landing 4:35 PM)` | omitted entirely |
| `{carousel}` | ` at carousel 7` | omitted — usually blank, the airline rarely reports it |
| `{car}` | ` in a Chevrolet Suburban` — make + model, **never a colour** | omitted |
| `{terminal}` | `the Royal Caribbean terminal` | `the cruise terminal` |
| `{port}` | `the Royal Caribbean terminal at Port Canaveral` | `the cruise terminal at Port Canaveral` |
| `{review_url}` | The Google review link | — |

Slots that start with a space (`{landing}`, `{carousel}`, `{car}`) carry their own
leading space so the sentence closes up cleanly when they're empty. Keep them
tight against the preceding word.

## Rules the copy has to keep

1. **Airport pickups are never curbside.** The driver walks in. Don't introduce
   "curb", "outside" or "arrivals level" into any arrival message.
2. **Never promise a colour.** Nothing in the system stores one.
3. **Departures can't quote a flight time.** Only arrival times exist in the data.
   A departure text can only use `{time}`, the booked pickup.
4. Keep `{review_url}` on its own line, last.

---

## Airport arrival — flight tracked

`ARR-T` — triggers when: pickup is at an airport AND we have a usable landing time

### ARR-T-WAY · On the way

```
Hi {guest}, this is {driver} with Grayson Towncar — I'll be your chauffeur today. I'm tracking {flight}{landing} and I'll meet you inside baggage claim with a name sign. No need to call if you're running behind, I'll be watching the flight. Just text me here once you have your bags.
```

<details><summary>Reads as</summary>

> Hi Jane, this is Marcus with Grayson Towncar — I'll be your chauffeur today. I'm tracking Delta 1423 (landing 4:35 PM) and I'll meet you inside baggage claim with a name sign. No need to call if you're running behind, I'll be watching the flight. Just text me here once you have your bags.

</details>

### ARR-T-LOC · On location

```
{guest}, I'm here and waiting for you inside baggage claim{carousel} with a name sign — {driver}, Grayson Towncar. Take your time. Just text me when you have your bags and I'll bring the car around.
```

<details><summary>Reads as</summary>

> Jane, I'm here and waiting for you inside baggage claim at carousel 7 with a name sign — Marcus, Grayson Towncar. Take your time. Just text me when you have your bags and I'll bring the car around.

</details>

### ARR-T-REV · Review request

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

## Airport arrival — no flight data

`ARR-U` — triggers when: pickup is at an airport but no landing time is known (no flight on the booking, or a red-eye whose arrival lands on a different date)

### ARR-U-WAY · On the way

```
Hi {guest}, this is {driver} with Grayson Towncar — I'll be your chauffeur today. I'll meet you inside baggage claim at {airport} with a name sign. Text me here once you've landed and have your bags, and I'll walk you out.
```

<details><summary>Reads as</summary>

> Hi Jane, this is Marcus with Grayson Towncar — I'll be your chauffeur today. I'll meet you inside baggage claim at Orlando International Airport (MCO) with a name sign. Text me here once you've landed and have your bags, and I'll walk you out.

</details>

### ARR-U-LOC · On location

```
{guest}, I'm here and waiting for you inside baggage claim{carousel} with a name sign — {driver}, Grayson Towncar. Take your time. Just text me when you have your bags and I'll bring the car around.
```

<details><summary>Reads as</summary>

> Jane, I'm here and waiting for you inside baggage claim with a name sign — Marcus, Grayson Towncar. Take your time. Just text me when you have your bags and I'll bring the car around.

</details>

### ARR-U-REV · Review request

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

## Departure — to the airport

`DEP` — triggers when: drop-off is at an airport and the pickup is not

### DEP-WAY · On the way

```
Good {daypart}, {guest} — this is {driver} with Grayson Towncar. I'm on my way to you now for your {time} pickup to {airport}{car}. I'll text you the moment I'm outside.
```

<details><summary>Reads as</summary>

> Good morning, Jane — this is Marcus with Grayson Towncar. I'm on my way to you now for your 6:15 AM pickup to Orlando International Airport (MCO) in a Chevrolet Suburban. I'll text you the moment I'm outside.

</details>

### DEP-LOC · On location

```
{guest}, I'm outside now{car} — {driver}, Grayson Towncar. No rush — come out whenever you're ready.
```

<details><summary>Reads as</summary>

> Jane, I'm outside now in a Chevrolet Suburban — Marcus, Grayson Towncar. No rush — come out whenever you're ready.

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

## Cruise embarkation — from the airport

`CRU-AIR` — triggers when: drop-off is a cruise port and the pickup is an airport

### CRU-AIR-WAY · On the way

```
Hi {guest}, this is {driver} with Grayson Towncar — I'll be your chauffeur out to {port} today. I'm tracking {flight}{landing} and I'll meet you inside baggage claim with a name sign. No need to call if you're running behind, I'll be watching the flight. Just text me here once you have your bags.
```

<details><summary>Reads as</summary>

> Hi Jane, this is Marcus with Grayson Towncar — I'll be your chauffeur out to the Royal Caribbean terminal at Port Canaveral today. I'm tracking Delta 1423 (landing 4:35 PM) and I'll meet you inside baggage claim with a name sign. No need to call if you're running behind, I'll be watching the flight. Just text me here once you have your bags.

</details>

### CRU-AIR-LOC · On location

```
{guest}, I'm here and waiting for you inside baggage claim{carousel} with a name sign — {driver}, Grayson Towncar. Take your time. Just text me when you have your bags and I'll bring the car around. We'll head straight for the ship.
```

<details><summary>Reads as</summary>

> Jane, I'm here and waiting for you inside baggage claim at carousel 7 with a name sign — Marcus, Grayson Towncar. Take your time. Just text me when you have your bags and I'll bring the car around. We'll head straight for the ship.

</details>

### CRU-AIR-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Have a wonderful cruise!

{review_url}
```

<details><summary>Reads as</summary>

> It was a pleasure driving you today, Jane. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Have a wonderful cruise!
>
> https://g.page/r/CRWIXii71sLGEBM/review

</details>

---

## Cruise embarkation — from a hotel

`CRU-HOTEL` — triggers when: drop-off is a cruise port and the pickup is not an airport

### CRU-HOTEL-WAY · On the way

```
Hi {guest}, this is {driver} with Grayson Towncar. I'm on my way to you now for your {time} pickup to {port}{car}. I'll text you the moment I'm outside.
```

<details><summary>Reads as</summary>

> Hi Jane, this is Marcus with Grayson Towncar. I'm on my way to you now for your 6:15 AM pickup to the Royal Caribbean terminal at Port Canaveral in a Chevrolet Suburban. I'll text you the moment I'm outside.

</details>

### CRU-HOTEL-LOC · On location

```
{guest}, I'm outside now{car} — {driver}, Grayson Towncar. No rush — come out whenever you're ready.
```

<details><summary>Reads as</summary>

> Jane, I'm outside now in a Chevrolet Suburban — Marcus, Grayson Towncar. No rush — come out whenever you're ready.

</details>

### CRU-HOTEL-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Have a wonderful cruise!

{review_url}
```

<details><summary>Reads as</summary>

> It was a pleasure driving you today, Jane. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Have a wonderful cruise!
>
> https://g.page/r/CRWIXii71sLGEBM/review

</details>

---

## Cruise debarkation — off the ship

`CRU-OFF` — triggers when: pickup is at a cruise port (whether they go on to a hotel or the airport)

### CRU-OFF-WAY · On the way

```
Hi {guest}, this is {driver} with Grayson Towncar — I'll be your chauffeur today. I'll be waiting for you at {terminal} when you come off the ship. Take your time with customs and text me here once you're through — I'll bring the car right to you.
```

<details><summary>Reads as</summary>

> Hi Jane, this is Marcus with Grayson Towncar — I'll be your chauffeur today. I'll be waiting for you at the Royal Caribbean terminal when you come off the ship. Take your time with customs and text me here once you're through — I'll bring the car right to you.

</details>

### CRU-OFF-LOC · On location

```
{guest}, I'm here at {terminal}{car} — {driver}, Grayson Towncar. Text me as soon as you're through customs and I'll pull right up.
```

<details><summary>Reads as</summary>

> Jane, I'm here at the Royal Caribbean terminal in a Chevrolet Suburban — Marcus, Grayson Towncar. Text me as soon as you're through customs and I'll pull right up.

</details>

### CRU-OFF-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Welcome back!

{review_url}
```

<details><summary>Reads as</summary>

> It was a pleasure driving you today, Jane. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Welcome back!
>
> https://g.page/r/CRWIXii71sLGEBM/review

</details>

---

## Charter / hourly — as directed

`CHARTER` — triggers when: the booking carries an hourly / as-directed stop

### CHARTER-WAY · On the way

```
Hi {guest}, this is {driver} with Grayson Towncar. I'm on my way to you now for your {time} pickup{car}. I'm at your service for the day — wherever you'd like to go. I'll text you the moment I'm outside.
```

<details><summary>Reads as</summary>

> Hi Jane, this is Marcus with Grayson Towncar. I'm on my way to you now for your 6:15 AM pickup in a Chevrolet Suburban. I'm at your service for the day — wherever you'd like to go. I'll text you the moment I'm outside.

</details>

### CHARTER-LOC · On location

```
{guest}, I'm outside now{car} — {driver}, Grayson Towncar. No rush at all — come out whenever you're ready and we'll go from there.
```

<details><summary>Reads as</summary>

> Jane, I'm outside now in a Chevrolet Suburban — Marcus, Grayson Towncar. No rush at all — come out whenever you're ready and we'll go from there.

</details>

### CHARTER-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Take care!

{review_url}
```

<details><summary>Reads as</summary>

> It was a pleasure driving you today, Jane. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Take care!
>
> https://g.page/r/CRWIXii71sLGEBM/review

</details>

---

## Point to point — everything else

`P2P` — triggers when: everything else — hotel to venue, point to point

### P2P-WAY · On the way

```
Hi {guest}, this is {driver} with Grayson Towncar. I'm on my way to you now for your {time} pickup{car}. I'll text you the moment I'm outside.
```

<details><summary>Reads as</summary>

> Hi Jane, this is Marcus with Grayson Towncar. I'm on my way to you now for your 6:15 AM pickup in a Chevrolet Suburban. I'll text you the moment I'm outside.

</details>

### P2P-LOC · On location

```
{guest}, I'm outside now{car} — {driver}, Grayson Towncar. No rush — come out whenever you're ready.
```

<details><summary>Reads as</summary>

> Jane, I'm outside now in a Chevrolet Suburban — Marcus, Grayson Towncar. No rush — come out whenever you're ready.

</details>

### P2P-REV · Review request

```
It was a pleasure driving you today, {guest}. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Take care!

{review_url}
```

<details><summary>Reads as</summary>

> It was a pleasure driving you today, Jane. If I took good care of you, a quick review means a great deal to us at Grayson Towncar — and it's the surest way to have me requested again. Take care!
>
> https://g.page/r/CRWIXii71sLGEBM/review

</details>

---

