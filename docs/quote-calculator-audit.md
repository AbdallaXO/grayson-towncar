# Quote Calculator — Audit & Readiness Report

**Date:** 2026-07-29
**Scope:** `dispatching/views.py:18421-18586`, `dispatching/templates/dispatching/quote_calculator.html`, `dispatching/urls.py:493-495`
**Question asked:** can dispatchers start using this now, and does it handle out-of-area work (e.g. Miami → Orlando) correctly?

**Short answer:** the plumbing works, but it is not ready for dispatchers today. It is superuser-gated, it has no tests, it disagrees with our own published rate card on every local route, and the out-of-area "charge them both ways" rule is baked in invisibly rather than being something a dispatcher can see or adjust.

---

## 1. What it is

A dispatcher-facing price estimator for **custom / long-distance trips that aren't on the published rate card**. Two addresses in, a suggested price out.

- Page: `/dispatching/quote-calculator/` → `quote_calculator()` (`views.py:18457`)
- API: `POST /dispatching/quote-calculator/calculate/` → `quote_calculator_api()` (`views.py:18476`)
- Nav: Dispatcher navbar → **Admin** dropdown → Quote Calculator (`dispatcher_navbar.html:215`)
- Shipped in commit `95e31f46` ("quote calculator under admin"), never opened up since.

---

## 2. How it works today

### Step by step

1. Dispatcher types a pickup and drop-off address. Google Places autocomplete assists (US-only).
2. Picks a vehicle and One Way / Round Trip.
3. Clicks **Calculate**. Browser POSTs the two address strings to the API.
4. Server calls `get_drive_time()` (`drivers/utils.py:26`) → Google Distance Matrix, `units=imperial`, `departure_time=now`. Result cached 2 hours per exact address pair.
5. Server parses miles out of the returned text (e.g. `"45.2 mi"` → `45.2`).
6. Applies a hardcoded per-vehicle formula.
7. Returns: headline price, distance, drive time, base fee, mileage fee, a five-row all-vehicle comparison table, and (sometimes) a matching published rate as an FYI.

### The formula

Hardcoded in `views.py:18425-18439` — **not in the database, not editable without a code deploy.**

| Vehicle | Base fee | Per mile | RT multiplier | One-way minimum |
|---|---|---|---|---|
| Towncar | $55 | $3.35 | ×1.90 | $135 |
| Mini Van | $60 | $3.55 | ×1.85 | $135 |
| SUV | $65 | $3.85 | ×1.90 | $170 |
| Van | $70 | $4.25 | ×1.93 | $175 |
| Van (14 Pax) | $85 | $5.85 | ×1.95 | $220 |

```
one_way   = max( round_to_$5( base + per_mile × miles ), minimum )
round_trip = round_to_$5( one_way × rt_multiplier )
```

The code comment at `views.py:18423` states the intent plainly:

> These are CUSTOM/RESIDENTIAL rates (higher than standard hotel zone rates **because the driver has a dead leg back**).

**That is the whole out-of-area answer as currently implemented: the return trip is already inside the per-mile rate.** There is no separate deadhead line, no toggle, and nothing on screen that tells the dispatcher this assumption is being made.

### Worked example — Miami → Orlando, ~235 mi

| Vehicle | One Way | Round Trip |
|---|---|---|
| Towncar | **$840** | $1,595 |
| Mini Van | $895 | $1,655 |
| SUV | $970 | $1,845 |
| Van | $1,070 | $2,065 |
| Van (14 Pax) | $1,460 | $2,845 |

Towncar one-way = `$55 + $3.35 × 235 = $842.25` → $840. Implied: 470 miles of driving (235 revenue + 235 empty) at ~$1.79/mile all-in.

---

## 2b. The formula, explained in plain terms

Four numbers per vehicle. Here is what each one is actually paying for.

### Base fee — "the cost of showing up"
$55 on a towncar. This is everything that happens regardless of how far the trip is: dispatching it, the driver getting to the vehicle, prep, greeting, loading bags. It's the reason a 2-mile run isn't priced at $7.

### Per-mile rate — **the load-bearing number**
$3.35 on a towncar, and this is the one worth understanding, because **it is charging for two miles of driving for every one mile of trip.** The empty return is inside it.

So the honest way to read it is: divide by two.

| Vehicle | Rate charged per trip-mile | What we actually earn per **mile driven** |
|---|---|---|
| Towncar | $3.35 | **$1.68** |
| Mini Van | $3.55 | $1.77 |
| SUV | $3.85 | $1.93 |
| Van | $4.25 | $2.12 |
| Van (14 Pax) | $5.85 | $2.92 |

That right-hand column is the number to sanity-check against reality — fuel + driver pay + wear + margin, per mile the wheels actually turn. If $1.68/mile covers a towncar with room left over, the rate is right.

### Minimum — "the floor"
$135 on a towncar. Below a certain distance the formula produces less than the minimum, so the minimum takes over and distance stops mattering:

| Vehicle | Minimum | Everything shorter than this is flat-priced |
|---|---|---|
| Towncar | $135 | 23.9 mi |
| Mini Van | $135 | 21.1 mi |
| SUV | $170 | 27.3 mi |
| Van | $175 | 24.7 mi |
| Van (14 Pax) | $220 | 23.1 mi |

This is why MCO → Disney (20 mi) and Disney → Universal (12 mi) both come out at exactly $135 — neither is being priced on distance at all. **It is also the direct cause of the biggest gaps against the published card**, since the card sells those same trips at $105 and $85.

### Round-trip multiplier — "two trips, small discount"
×1.90 on a towncar. A round trip is two separate transfers, usually days apart, each with its own empty return — so it's ~2 one-ways, minus about 5%. This matches how the published card is built (MCO → Disney is $105 OW / $195 RT = 1.86×), so the multiplier is sound.

Where it *doesn't* fit: a same-day out-and-back with the driver waiting (Orlando → Miami → Orlando). That's 470 miles driven with **no** empty legs, but it prices at $1,595 — nearly double the one-way for identical driving. That job is really hourly work, and the formula has no way to express it.

### Rounding
Nearest $5, so prices are quotable. (Currently rounds the wrong way half the time — see C2.)

### The whole thing on one line

```
Price = max( Base + PerMile × miles , Minimum )     rounded to $5
        └─ shows up ─┘ └ trip + empty return ┘  └ floor for short runs ┘

Round trip = that × multiplier (~1.9, i.e. two of them less ~5%)
```

---

## 3. Findings

### 3.1 Blockers — must fix before dispatchers touch it

**B1. Dispatchers cannot open it.** Both the page (`views.py:18459`) and the API (`views.py:18478`) hard-require `is_superuser`, and the navbar link sits inside the superuser-only Admin block. A dispatcher who is given the URL is silently redirected to the dashboard. This is the single reason it isn't live.

**B2. It disagrees with our own published rate card on every route we sell.** The formula is above the rate card everywhere, by 20–60%:

| Route (approx mi) | Vehicle | Rate card OW | Calculator OW | Diff |
|---|---|---|---|---|
| MCO → Disney (20) | Towncar | $105 | $135 | **+$30** |
| MCO → Disney (20) | SUV | $140 | $170 | +$30 |
| MCO → Universal (15) | Towncar | $105 | $135 | +$30 |
| MCO → Kissimmee 192 (22) | Towncar | $115 | $135 | +$20 |
| MCO → Championsgate (30) | Towncar | $130 | $155 | +$25 |
| Disney → Universal (12) | SUV | $105 | $170 | **+$65** |
| Port Canaveral → MCO (52) | Towncar | $160 | $230 | **+$70** |
| Disney → Port Canaveral (72) | Towncar | $185 | $295 | **+$110** |
| Disney → Port Canaveral (72) | Van | $255 | $375 | +$120 |

Round-trip gaps are roughly double these. A dispatcher quoting Disney → Port Canaveral from this tool says **$295** while the website sells the identical trip at **$185**.

The tool *does* look for a matching published rate and shows it — but only as a small blue "Existing rate found" note *below* the big green number. The wrong number is the loud one.

**Root cause:** one flat per-mile rate that assumes a 100% empty return on every trip. That assumption is roughly true for a Miami run. It is false locally, where drivers chain jobs back-to-back — which is exactly why the published card is lower. One rate cannot serve both cases.

**B3. Feet parse as miles.** `views.py:18505` does `Decimal(distance_text.split()[0])`. Google returns imperial distances under ~0.1 mi in **feet** — `"285 ft"`. That parses as **285 miles**, producing a $1,010 towncar quote for a trip across a parking lot. Reachable with two nearly-identical addresses, which is a realistic typo.

**B4. No tests.** Zero test coverage on the formula, the minimums, the rounding, or the rate matching. Nothing catches a fat-fingered rate edit.

### 3.2 Correctness bugs

**C1. The "existing rate" match picks the wrong location.** `views.py:18543-18554` loops every `Location`, and the inner `break` only exits the keyword loop — the outer loop keeps going, so **the last matching location wins**, not the best one. With aliases like `MCO` / `Orlando Airport` / `Orlando`, which route is detected depends on database row order. Should match longest-alias-wins and stop.

**C2. Rounding is inconsistent at the $2.50 boundary.** `_round_to_5()` (`views.py:18451`) uses Python's `round()`, which is banker's rounding on Decimals:

| Raw | Rounded |
|---|---|
| $127.50 | $130 ↑ |
| $132.50 | **$130 ↓** |
| $137.50 | $140 ↑ |
| $142.50 | **$140 ↓** |

A dispatcher checking the math by hand gets a different answer half the time. Should be `ROUND_HALF_UP` (which the rest of the codebase already uses — see `reservations/utils.py:589`).

**C3. Unknown vehicle silently prices as a Towncar.** The dropdown is `Vehicle.objects.all()`, but the formula is keyed by a hardcoded dict with `.get(vehicle_type, QUOTE_FORMULA["towncar"])` (`views.py:18510`). Add a vehicle type in the admin, and it quotes at towncar prices with no warning. The comparison table has the mirror problem — it always lists all five tiers, including Van (14 Pax), whether or not that vehicle exists in this environment.

**C4. Drive time shown is for right now, not for the pickup.** `get_drive_time()` passes `departure_time=now`, so a 3 AM airport run quoted at 5 PM shows rush-hour drive time. Doesn't affect price (distance is traffic-independent), but it is on screen and dispatchers will quote it to customers.

### 3.3 Pricing gaps

The calculator produces a **base fare only**. Everything the real booking flow charges is missing:

| Charge | Exists in the system? | In the calculator? |
|---|---|---|
| After-hours fee (10 PM–6 AM), $20/leg | Yes — `reservations/utils.py:24` | **No** |
| Extra car seats / boosters | Yes — `Vehicle.extra_carseat_fee` | **No** |
| Extra stops | Yes — `Vehicle.extra_stop_fee`, `LegStop` | **No** |
| Gratuity % | Yes — `extra_charges()` | **No** |
| Tolls | No | **No** |
| Wait time / hourly charter | Partial — `LegStop` type `charter` | **No** |
| Meet & greet / airport parking | No | **No** |

Tolls matter specifically for the Miami case: Florida's Turnpike round-trip is real money and is currently absorbed into margin.

Also absent:
- **No hourly / as-directed pricing.** A same-day Orlando → Miami → Orlando with the driver waiting is 470 total miles with *zero* deadhead, but the tool quotes it as a round trip at $1,595 — nearly double the one-way for the same driving. That job should be priced hourly.
- **No cost or margin view.** For a Miami job we would likely farm out to a Miami affiliate. The tool shows a sell price with no idea what the job costs us in-house or farmed out, even though `farmout_optimizer.py` and `Route.inhouse_base_pay` already exist.
- **No distance tiering.** One flat $/mile from 2 miles to 235 miles.

### 3.4 Workflow gaps

**W1. Nothing is saved.** There is already a `Quote` model (`reservations/models.py:2892`) with status tracking, `is_current`, and a link to `Lead`. The calculator writes to none of it. Consequence: no record of what was quoted, to whom, by which dispatcher; no follow-up; no win/loss data; and no way to settle a "you quoted me $600" dispute.

**W2. Dead end.** The "New Booking" button starts the 6-step dispatcher booking wizard from scratch — the quote does not carry over. The dispatcher re-keys the addresses and re-types the price into `manual_base_price` at step 5 (`views.py:7331`).

**W3. Not connected to the out-of-area flag we already have.** `LegStop.requires_manual_review` (`models.py:2361`) exists precisely to mark *"out-of-area stop needing a custom quote before charging."* Those flagged stops are what this calculator is for, and there is no link between them.

**W4. No quote expiry or date input.** Fuel and demand move; a quote given today has no stamped validity. There is also no pickup date field, so seasonality and after-hours can never be applied.

---

## 4. What "Miami → Orlando" needs specifically

The current tool answers it, but silently and without the levers dispatchers need:

1. **It never says it's charging for the return.** $840 appears with "Base Fee $55 / Mileage Fee $790" and no mention of a 235-mile empty leg. Dispatchers can't defend the number on the phone.
2. **It can't be switched off.** If we already have a Miami → Orlando job that day (a backhaul), the return is free to us — but there is no way to drop that charge, so we quote high and lose the job.
3. **It can't distinguish direction.** Orlando → Miami (deadhead *after*) and Miami → Orlando (deadhead *before*) price identically. The second is worse for us: the driver is out of position for hours before earning anything.
4. **No tolls, no overnight.** A Miami run may need a hotel or a second driver. Nothing captures that.
5. **No affiliate comparison.** No prompt to check whether farming it out to a Miami affiliate beats sending our own car 235 miles.

---

## 5. Recommended target design

**Tier 1 — required before dispatchers use it**
1. Open access to `is_staff` (page + API), move the nav link out of the Admin dropdown.
2. Published rate card wins when the route matches; formula only fills gaps. Show the card price as the headline with the formula as a secondary "custom estimate."
3. Fix B3 (feet), C1 (location match), C2 (rounding), C3 (unknown vehicle).
4. Add tests: formula per vehicle, minimums, rounding boundaries, feet/comma parsing, rate-card precedence.

**Tier 2 — make the price defensible**
5. Itemize the quote: base, mileage, **deadhead return (toggleable)**, tolls, after-hours, car seats, stops, gratuity → total.
6. Move the formula out of code into an editable settings model, so rates change without a deploy.
7. Add a pickup date/time field so after-hours and seasonality can apply.
8. Distance tiers — a lower $/mile past ~75 miles.

**Tier 3 — close the loop**
9. Save every calculation to the existing `Quote` model, attributed to the dispatcher.
10. "Book this quote" → prefill the booking wizard.
11. Show estimated cost (in-house pay vs affiliate) and margin next to the sell price.
12. Surface it from `LegStop.requires_manual_review` items.

---

## 5c. Competitive check — Blacklane, 14 quotes

Founder-collected, all **Friday 31 July 2026, 1:30 PM**, booked two days out. Blacklane runs Business Class (≈ our towncar) and Business SUV (≈ our SUV); no Van or Sprinter tier, so nothing here calibrates our Van rates.

> **Read with care.** Blacklane is a **network**, not a fleet — they have partner supply in Miami and Tampa, so a long one-way may carry little or no deadhead cost. Their long-distance numbers are not apples-to-apples with ours. The short and mid-range rows are. Also: Friday afternoon, two days out, is near the top of their dynamic range, so our premium over them is probably *understated* here.

### Their formula

Fitting the 12 non-Miami Business Class quotes:

```
vs drive time   $70 + $2.48/minute     R² = 0.92    ($149/hour)
vs distance     $92 + $2.26/mile       R² = 0.977   (distances estimated)
```

**Their per-minute rate is within 1% of what our own published card implies** ($2.51/min, §5 fit) — we arrived at the same hourly rate independently, which is the strongest evidence yet that the card is well-calibrated to market.

Solving the discriminating pairs (#5 vs #8: 7% more time but 60% more distance; #10 vs #9) rules out either pure model — price moves far more than time alone and far less than distance alone:

```
Blacklane ≈ $44 base + ~$1.90/mile + ~$2.00/minute + ~$20 airport surcharge
```

A **hybrid**. Treat the exact mile/minute split as indicative — it rests on estimated distances — but the presence of both terms is solid, and it is what motivated D5.

### Head to head — our towncar vs their Business Class

| Trip | ~Mi | Blacklane | Grayson | Diff |
|---|---|---|---|---|
| Ritz-Carlton → OCCC | 7 | $95.99 | $135 | **+41%** |
| MCO → Lake Nona Wave | 6 | $109.09 | $135 | **+24%** |
| Lake Nona → Ritz-Carlton | 11 | $114.28 | $135 | **+18%** |
| MCO → Portofino Bay | 15 | $134.27 | $135 | **+1%** |
| Grand Floridian → MCO | 20 | $143.33 | $135 | −6% |
| MCO → Swan & Dolphin | 19 | $144.38 | $135 | −7% |
| MCO → Grove Resort | 30 | $160.62 | $155 | −3% |
| MCO → Port Canaveral | 48 | $201.30 | $215 | +7% |
| Grand Floridian → LEGOLAND | 45 | $176.48 | $205 | +16% |
| Grand Floridian → Tampa | 78 | $267.42 | $315 | +18% |
| Tampa → Grand Floridian | 78 | $284.44 | $315 | +11% |
| Grand Floridian → Port Canaveral | 72 | $244.57 | $295 | **+21%** |
| Grand Floridian → Miami | 230 | $645.01 | $825 | **+28%** |
| Miami → Grand Floridian | 230 | $1,053.33 | $825 | **−22%** |

Three bands:

- **15–30 mi: within ±7%.** Our core airport business is priced almost exactly at market.
- **Under 12 mi: 18–41% over.** The $135 floor is the one number clearly off-market. Kept anyway — Blacklane carries no deadhead on a short network hop, and the founder is explicit that a private car service charges to send a car out at all. Worth *choosing* rather than inheriting.
- **45+ mi: 7–28% over,** because their per-mile decays with distance ($18/mi at 6 mi → $2.80/mi at 230) while ours is flat at $3.35. Note the several rows above are all *rate-card* routes where D1 now applies, so the calculator no longer quotes those figures at all.

### The Miami asymmetry — the most useful row

$645 outbound vs $1,053 inbound, same route reversed, **63% apart**. Identical mileage, so it isn't distance. See D6 for the mechanism and what we did about it.

Our symmetric $825 sat almost exactly on the average of their two directions (**$849**) — the founder's "charge them both ways" instinct produced the right *average*; it just wasn't differentiating direction.

On Tampa: founder's gut was $300–340, they charge $267–284, our formula says $340. ~15% above market is a defensible premium for a private fleet over a network.

---

## 6. Decisions — 2026-07-29 (founder)

**D1. Published rate card always wins.** If the route is on the card, the card price is the answer. The formula only fills gaps for unlisted / custom / out-of-area routes. This closes B2.

**D2. Deadhead stays baked into the per-mile rate — but the dispatcher gets to see it.**
Two audiences, one number:

- **Guest sees:** one price. `$840, one way.` No mention of empty miles, no line items, no abstraction leaking out.
- **Dispatcher sees (internal panel only):** why that number is what it is, so they can hold the line if the guest pushes back on an out-of-town price.

```
GUEST-FACING                 INTERNAL — dispatcher only, do not read out
─────────────                ──────────────────────────────────────────
                             Distance          235 mi each way
  $840                       Driving           470 mi (return is empty)
  One Way · Towncar          Base              $55
  Miami → Orlando            Mileage           $787   ($394 out + $394 back)
                             Est. tolls        $30    ← our cost, not a line item
                             ─────────────────────────
                             Quote             $840
```

The internal panel is explanatory, not editable — the math does not change. No removable deadhead line.

**D3. Rates are calibrated correctly.** $840 towncar one-way Miami → Orlando stands. No changes to base fees, per-mile rates, minimums, or multipliers. See §2b for what each number means.

**D4. Extras are add-ons, not baked in.** For out-of-state work we quote an **all-inclusive** price to the guest. Tolls and similar costs appear on the internal panel only, so we can see what a long run actually costs us — they are never a separate line on the guest's quote.

**D5. Drive time sets a floor, mileage still leads.** Founder point: *"it's also the traffic and drive time. For Tampa it's at least an hour and a half. The time matters."* Confirmed by the data — refitting the 13 published towncar prices against drive time beat distance (R² 0.92 vs 0.88) at **$43 + $2.51/min ≈ $150/hour**.

But every price the founder has confirmed is set by *mileage*, so time became a **floor**, not the driver: `price = max(mileage_formula, committed_hours × hourly_floor)`, at **$105/h** for a towncar (scaled per vehicle). Committed hours = 2 × one-way drive time, i.e. out plus the empty return.

It only binds on genuinely slow routes. Every confirmed anchor is untouched:

| Trip | Mileage | Time floor | Result |
|---|---|---|---|
| Miami inbound (235 mi / 3h35) | $842 | $753 | **$840** ✓ |
| Tampa (85 mi / 78 min) | $340 | $273 | **$340** ✓ |
| Short custom (13 mi) | $99 | $70 | **$135** ✓ (minimum) |
| LEGOLAND (45 mi / 61 min) | $206 | **$214** | **$215** ← floor bites |

The long runs earn ~$113–120 per committed driver-hour against ~$150 on local chained work. That discount is deliberate: chaining is where the margin is, and the founder confirmed both endpoints.

**D6. Out-of-area prices split by direction.** From the Blacklane check (§5c): the same 230-mile route quoted **$645 outbound vs $1,053 inbound**. Both directions drive identical miles, so the gap isn't distance — on an **inbound** the empty positioning leg has a *deadline* (the car must be there before pickup or the job is missed), while an outbound's empty return is unscheduled and can be filled opportunistically.

Their spread was only 6% at 78 miles but 63% at 230, so the adjustment ramps: nothing below 100 miles, reaching a **20% outbound discount** at 235 miles. Inbound is unchanged, which preserves the confirmed $840.

| Miami, towncar | Grayson | Blacklane |
|---|---|---|
| Inbound (Miami → Orlando) | **$840** ✓ confirmed | $1,053 |
| Outbound (Orlando → Miami) | **$675** | $645 |

Direction needs the pickup's distance from base, which costs a second Distance Matrix call — so it is only fetched above the 100-mile threshold, where it can actually change the price. If that lookup fails, pricing falls back to symmetric (the old behaviour).

**Still open (not blocking):**
- Hourly / as-directed pricing — the formula can't express a same-day out-and-back with waiting. Worth a follow-up.
- Farm-out cost comparison for out-of-area jobs (below, #11).
- **SUV per-mile.** Blacklane's SUV premium *grows* with distance (1.13× short → 1.35× at 230 mi); ours *shrinks* (1.26× → 1.13×), so we likely underprice SUV on long runs. Raising per-mile from $3.85 to ~$4.19 would hold a ~1.25× premium. Left alone deliberately — every confirmed anchor was a towncar and this is a live price change. One-line flip in `quote_engine.VEHICLE_RATES`, documented in place.

---

## 6b. Decisions — local custom trips (founder, 2026-07-29)

Triggered by a real quote: **Grand Floridian → 2596 Carrickton Cir, Orlando** (21.8 mi, 33 min) — a residential address a few miles from MCO. The mileage formula said $135 towncar; the founder said *"this seems a bit high… I would price this $120."*

**D7. Local custom trips are priced off the comparable card route, not from mileage.**

The founder's four figures aren't gut feel. He priced by analogy — *"this location is almost the same as the airport"* — because the destination sits next to MCO. Take the MCO ⇄ Disney card price and multiply:

| Vehicle | MCO ⇄ Disney card | × 1.135 | Founder said |
|---|---|---|---|
| Towncar | $105 | $120 | **$120** ✓ |
| Mini Van | $120 | $135 | **$135** ✓ |
| SUV | $140 | $160 | **$160** ✓ |
| Sprinter | $220 | $250 | **$250** ✓ |

All four exact. Any premium in **12.5%–14.6%** reproduces them after $5 rounding, so the fit is robust rather than curve-bent; **13.5%** is the midpoint. The Sprinter reasoning he spelled out — *"$220 is MCO to Disney, and there is usually no empty head back, so maybe $250"* — is the same rule, just said aloud.

Mechanism: an address that doesn't match a zone by name is **snapped to its nearest card zone** (Carrickton Cir → MCO, 4.1 mi), then priced off that route + premium. Snapping by *nearest zone* rather than nearest route-by-distance matters: 21.8 mi is closer to MCO → Kissimmee (22 mi, $115 → $130) than to MCO → Disney (20 mi), so a distance-based comparable would have produced $130, not $120. An address farther than **15 mi** from every zone doesn't snap and falls through to the mileage formula.

**D8. No empty return on local work.** Founder: *"we probably don't need to think about the empty return for local roads."* Drivers chain local jobs, so the out-and-back doubling in `per_mile` applies to out-of-area runs only. This was the actual cause of every local overquote — and of the 24–41% gap against Blacklane on short trips (§5c).

**D9. Local floor $110 (towncar).** Founder: *"it can be 6 miles, but I will have to drive from my base 10 miles, then 6 miles, then back to my base. So let's say $110."* That is positioning cost, not trip cost. Other vehicles scale by their MCO ⇄ Disney card ratio: Mini Van $125, SUV $145, Van $170, Sprinter $230. This supersedes the $135 minimum for in-area work — required, since a monotonic price cannot charge $135 at 6 mi and $120 at 22 mi. The out-of-area minimums are unchanged.

**D10. Local fares are quoted pre-gratuity.** Founder wants room for a **20% recommended gratuity** on top. $120 + 20% = $144, which sits right at Blacklane's all-inclusive ~$151 for the same trip — so the fare is correctly placed and the tip is additive. Out-of-area quotes stay **all-inclusive** with no gratuity line (D4 unchanged).

**Round trips** apply the same +13.5% to the card's round-trip price (founder's choice of three options). Note this lands $5 under two of his off-the-cuff RT figures — SUV computes to **$310**, not the $325 in my option preview, which had a rounding slip; likewise Van $345 not $350. One-ways are exact.

### Pricing regimes, final

| Regime | Trigger | Priced by |
|---|---|---|
| **Rate card** | both ends match a zone by name, and a route exists | card price verbatim |
| **Local — card comparable** | in-area, and a card route links the two zones | comparable card route + 13.5%, floored |
| **Local — no comparable** | in-area, no card route (usually intra-zone) | one direction of driving, floored |
| **Out of area** | no end resolves to a zone, or trip > 60 mi | mileage formula, empty return included, direction-adjusted |

"In-area" means at least one end resolves to a card zone (matched or snapped) **and** the trip is ≤ 60 mi. Nothing in-area carries an empty return (D8); everything in-area carries the recommended gratuity (D10).

**Bug found by the founder testing a real trip, 2026-07-29 (fixed):** MCO → a residence 4 mi from MCO. Both ends resolved to the *same* zone, and a zone has no route to itself, so the quote fell through to the out-of-area formula — charging the $135 dispatch minimum instead of the $110 local floor, showing an empty-return split that does not apply locally, and omitting the gratuity line. The "Local — no comparable" regime above closes it. Three tests pin the case, including one asserting that a long trip from a known zone (Disney → Tampa) still keeps its empty return.

---

## 6c. Decisions — out-of-town pricing, second pass (founder, 2026-07-29)

Triggered by two real quotes the founder judged **too low**: Miami → Disney SUV, and Disney → Port Everglades (218 mi / 3h14m) at $650 / $745 / $1,120 against his floors of $850 / $920 / $1,400.

**D11. Gratuity is always quoted ON TOP of the fare, never folded in.** Founder: *"we would let the guest know it will be nine hundred and twenty dollars plus twenty percent gratuity."* Every figure the engine produces is a **fare**. What differs by regime is whether the 20% is obligatory:

| Regime | Gratuity | Dispatcher hint |
|---|---|---|
| Local | 20% **suggested** — guest's call | *"Quote this as the fare. Gratuity is suggested on top, not billed."* |
| Out of town | 20% **billed** automatically | *"Say '$X plus 20% gratuity'. Out of town — the gratuity is billed, not optional."* |

This reversed an earlier same-day implementation that backed the gratuity *out* of the total. Internally the gratuity is **margin, not a pass-through** — drivers are paid a flat or hourly rate on out-of-town work, so it is an upsell. Two internal notes carry that, including *"never discuss with a guest how a gratuity is split."*

**D12. The outbound discount was removed — it was my error.** Introduced hours earlier (D6) from Blacklane pricing the same long route $645 outbound vs $1,053 inbound. That asymmetry is a **network property**: Blacklane has supply at the far end, so their outbound leg costs them little. An Orlando-based fleet eats the empty return whichever way the paying leg runs. The discount was cutting **17.5%** off exactly the trips priced highest — the single largest cause of the "too low" complaint. Direction is still classified, but only to explain the trip in the notes.

**D13. Long-haul rate tier.** With the discount gone, SUV was 1.6% light and Sprinter 2.9%, but Towncar was still 7.6% light — a flat per-mile underprices a genuine long haul, which commits a driver's whole day. So per-mile now steps up past 100 miles:

| Vehicle | ≤100 mi | >100 mi |
|---|---|---|
| Towncar | $3.35 | **$3.90** |
| Mini Van | $3.55 | $3.90 |
| SUV | $3.85 | $3.98 |
| Van | $4.25 | $4.45 |
| Sprinter | $5.85 | $6.19 |

Fitted to hit all three Port Everglades floors exactly: **$850 / $920 / $1,400**. Tampa (85 mi) is below the threshold and stays **$340**; local pricing is untouched.

> **Consequence to note:** Miami → Orlando towncar moved **$840 → $915**. Mathematically forced — a $850 floor at 218 mi means a distance-rising price cannot be under $850 at 235 mi. The earlier "$840 is about right" is superseded, and the test says so with the reasoning.
>
> **Mini Van $875 and Van $1,020 at 218 mi are interpolations** — only Towncar, SUV and Sprinter were specified. Placed proportionally, with a test enforcing that tiers stay in price order.

**D14. Airport pickup surcharge, in the backend.** Founder: *"for airport pickups always add an additional fee since we have to go thru commercial lane/tunnel. If it's point to point not airport, you can take that fee."* **$40** on long trips, **$20** on short.

- **Pickups only.** Collecting a guest means the commercial lane (and at MIA, the tunnel); dropping at departures does not. Disney → Tampa Airport carries nothing.
- **Once per trip,** not per leg — a round trip collects at the airport and drops at departures.
- **Built into the fare,** not itemised to the guest; visible in the internal breakdown.
- Detected from the pickup address by the word "airport" or a parenthesised code (`(MIA)` — Google Places' format). Guarded so **"1234 Airport Rd" does not trigger it**.
- **Never applied to a published card price.** MCO → Disney is an airport pickup, but $105 is what the website charges and already absorbs that cost — adding to it would quote above the website and break D1. Applies to off-card fares only.

**D15. Demo notice on the page.** A standing banner: *"Demo — still in progress. These prices are still being calibrated… If a number looks off, or the trip is unusual, double-check with Ab & Ray before quoting a guest."* Remove the `.qc-demo` CSS block and markup together when pricing is signed off.

---

## 7. What shipped — 2026-07-29

All pricing rules moved out of `dispatching/views.py` into **`dispatching/quote_engine.py`**, so they are testable without a request and validated against the published card. **73 tests in `dispatching/tests_quote_engine.py`, all passing; full 852-test dispatching suite green.**

The founder's four local figures, the $110 floor, the 20% gratuity, Tampa at $340 and Miami at $840 are each pinned by a test, so a future rate edit that moves any of them fails loudly rather than silently.

### ⚠ Needs a founder fix: the rate card contradicts itself

Validating the engine against all 56 published prices surfaced a **data error in the card itself** — not a code bug. The card stores each direction as its own row, and one pair disagrees:

| Vehicle | Disney → Universal | Universal → Disney |
|---|---|---|
| Towncar | $85 | $85 ✓ |
| SUV | $105 | $105 ✓ |
| Van | $115 | $115 ✓ |
| **Mini Van** | **$190** | **$100** ✗ |

$190 for a ~12-mile minivan hop, when the SUV is $105 and the Van is $115 on the identical route, is almost certainly a typo — **$100 looks like the intended figure** (it sits naturally between towncar $85 and SUV $105). Every other bidirectional pair on the card agrees with itself; this is the only one.

Live consequence: a guest booking a Disney → Universal minivan online may be paying $190 while the return leg sells for $100.

Until the data is corrected, the engine **quotes the direction of travel and warns the dispatcher**, naming both figures rather than silently picking a side. Two tests pin this behaviour, so correcting the card will make the conflict test fail — which is the signal to delete it.

**Done**
1. **Rate-card precedence (D1).** Card price wins outright; the formula never runs on a card route. Tested against every published price, both trip types, both directions of travel.
2. **Alias seeding — the fix that makes D1 actually work.** Audit found *every* `Location.aliases` field empty, so a match needed a dispatcher to literally type "All WDW Disney Property Resorts". Nothing real would ever have matched, and the card would never have won. `DEFAULT_LOCATION_ALIASES` now seeds ~90 conservative aliases (resort names, MCO/SFB, Portofino Bay, Rosen properties, ChampionsGate…), supplementing whatever is in the database. Deliberately conservative: a **missed** match falls to the formula and is flagged on screen; a **wrong** match would silently quote the wrong published price. Generic names spanning zones ("Lake Buena Vista", "Orlando") are excluded.
3. **B3 — feet parsed as miles.** `"285 ft"` read as 285 **miles** and quoted ~$1,010 for a trip across a parking lot. Now unit-aware (mi/ft/km/m, thousands separators).
4. **C1 — location matcher.** Longest matching alias wins and stops; short aliases like `MCO` require word boundaries so they can't hit inside an unrelated word. Result no longer depends on database row order.
5. **C2 — rounding.** `ROUND_HALF_UP` replaces banker's rounding, so $132.50 → $135 instead of $130. A dispatcher checking by hand now agrees with the tool.
6. **C3 — unknown vehicle.** Raises instead of silently pricing a Van at towncar rates. Dropdown only offers vehicles that have rates.
7. **D5 hourly floor** and **D6 directional pricing**, per above.
8. **Two-audience output (D2/D4).** One all-inclusive figure for the guest, marked *"this is the figure to give the guest."* A collapsed, clearly-labelled **"Internal breakdown — do not read to the guest"** panel shows the base, the mileage split into *with the guest* vs *empty return*, earnings per mile driven, committed hours, implied $/driver-hour, and which rule set the price. Explanatory only — no editable deadhead line.
9. **Missed card matches are now visible.** The result footer names the matched pickup and drop-off zones, or says "no match", so a fall-through to custom pricing is obvious rather than silent.

### Dispatcher UI

Rebuilt on the dispatcher shell's own palette (`#1a1d21` / gold `#C9A227`), all styles scoped under `.qc` so nothing leaks into other pages. Internal tool, so it optimises for scanning while on the phone rather than guest-facing polish.

- **Click any vehicle row** to switch the headline price, image, gratuity, and the whole internal breakdown to that vehicle — no second API call, since `all_vehicles` already carries a full per-vehicle payload. The form's vehicle dropdown stays in sync. Ask a question about the Towncar while quoting an SUV without re-running anything.
- **Vehicle images** in the picker rows and above the headline figure, from the landing page's optimized `.webp` assets (not `Vehicle.image`, which isn't guaranteed to exist in every environment).
- **Route map** with the drive drawn in brand gold, via the Maps JS Directions service — the key already in use supports it, no extra enablement needed. It fills the previously dead left column. Labelled explicitly: distance and price come from the addresses, not the map, so nobody reads the map's own mileage as the quoted basis.
- **Copy button** on the hero — dispatchers paste the quote into email or SMS.
- **Swap** button on the route timeline, since return legs get quoted constantly.
- The rate STRUCTURE is not rendered anywhere: no standing sheet of base fees, per-mile rates, minimums, or hourly floors. Founder's call — dispatchers get the price and the per-quote reasoning, not the rate sheet.

**Deliberately not done**
- **Access is still superuser-only.** Founder: *"I do have it superusers on purpose since it is not ready yet."* Pricing is fixed and tested, but it should price real trips first. Opening it up = relax the two `is_superuser` checks in `views.py`, move the navbar link out of the Admin dropdown, and **update SOP-002 §D06**, which currently instructs dispatchers *not* to use this tool and lists it as founder-only. A test asserts staff are 403 today, so that flip is deliberate rather than accidental.

**Remaining backlog**
10. Toll estimation on the internal panel (D4 — internal visibility only).
11. Save each calculation to the existing `Quote` model, attributed to the dispatcher.
12. "Book this quote" → prefill the booking wizard.
13. Move rates from code into an editable settings model.
14. Show cost (in-house vs affiliate) and margin beside the sell price.
15. Surface from `LegStop.requires_manual_review` items.
16. Pass the **scheduled pickup time** to Distance Matrix instead of `departure_time=now` (C4). Cosmetic while mileage leads, but it matters more now that time can set a floor — and quotes should use *typical* time for the booked hour, not live traffic, or the same trip prices differently depending on when the dispatcher happens to ask.
