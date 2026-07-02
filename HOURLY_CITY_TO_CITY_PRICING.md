# Hourly Charters & City-to-City — Pricing, Quote Engine & Pages

Two new public service lines, an instant-quote engine, and two on-brand pages.
Everything price-related is **admin-editable** — a non-engineer can change any
number at `/admin/` with no code change or redeploy.

- **Fleet page:** `/fleet/`
- **Hourly & City-to-City page (with the quote widget):** `/hourly-city-to-city/`
- **Quote API:** `POST /api/quote/`
- **Booking hand-off:** `/book-quote/<token>/` → existing Stripe checkout

Everything lives in the new `pricing` app.

---

## 1. Where to edit pricing (Django admin)

All under **Admin → Pricing & Quotes** (`/admin/pricing/`):

| What you want to change | Admin section | Notes |
|---|---|---|
| Gratuity %, peak multiplier, rounding, display copy | **Pricing configuration** | One row (singleton). Default gratuity 20%, peak 1.25×, round to whole dollars. |
| The 5 vehicle classes, their **customer-facing names** & capacities | **Vehicle classes** | `display_name` is shown to customers (e.g. the DB type `Van(14 Pax)` shows as **Sprinter Van**). `vehicle_type` links to the real `rates.Vehicle` and should not be changed casually. |
| Hourly $/hr and minimum hours | **Hourly rates** | Optional `peak_minimum_hours` (Sprinter = 4 hr on peak). |
| Per-mile fallback (base + per-mile + floor) for **unlisted** routes | **Fallback formulas** | Used only when a route is not in the named table. |
| The named **route table** (Daytona, Tampa, Miami, …) and per-class prices | **City routes** | Open a route to edit its per-class prices inline. Add a new route here. |
| Peak/holiday/OCCC dates | **Peak dates** | Inclusive date ranges. Optional per-event `multiplier` overrides the global one. |
| Cached miles for unlisted routes | **Route distances (cache)** | Pre-enter miles here to avoid any live Google call. |
| Log of quotes customers generated | **Instant quotes** | Read-only; shows which converted to a reservation. |

### Common edits
- **Change Miami's price:** Admin → City routes → *Miami (MIA)* → edit the inline
  per-class prices. (Miami and Fort Lauderdale are seeded **identical** on
  purpose — change either independently here.)
- **Add a new city route:** Admin → City routes → *Add*. Set name, `aliases`
  (comma-separated terms a customer might type), `approx_miles`, then add a
  price row per class. It appears in the widget dropdown automatically.
- **Raise the peak multiplier for one holiday:** Admin → Peak dates → set that
  row's `multiplier` (leave blank to use the global value).
- **Make an OCCC closeout week peak:** Admin → Peak dates → *Add* with the date
  range.

---

## 2. How a quote is priced

```
City-to-City (one-way, all-inclusive — tolls included):
  if (origin, destination) matches a named CityRoute:
      base = CityRoutePrice[route][class]          # authoritative, ZERO distance lookup
  else:
      miles = cached/precomputed distance (live Distance Matrix only as last resort)
      base  = formula.base + miles * formula.per_mile
      base  = max(base, formula.minimum)           # floor

Hourly (clock starts at pickup, spot-to-spot):
      base = hourly_rate * hours                    # hours >= minimum (3 hr; 4 hr Sprinter peak)
      overtime billed in 30-min increments at the hourly rate

All modes:
      if date is a peak date: base is multiplied by the peak multiplier
      gratuity = 20% of the (peak-adjusted) base, shown as a separate line
      total    = peak-adjusted base + gratuity
      all amounts rounded to whole dollars
```

Example (acceptance): **Miami SUV = $950 base + $190 gratuity = $1,140 total**,
`price_source = route_table`, no distance lookup.

---

## 3. Distance discipline (cost control)

We have had Google Distance Matrix billing spikes, so:

1. **Named routes never call Google** — they read the route table.
2. **Unlisted routes** first hit `RouteDistanceCache` (precomputed miles).
3. Only on a cache miss is a **single** live Distance Matrix call made — and the
   result is cached, so a given pair is billed **at most once**. Every live call
   logs the greppable tag `GTC-GOOGLE-LIVE-DISTANCE`.

Disable live calls entirely with `PRICING_ALLOW_LIVE_DISTANCE=0` (then an
uncached unlisted route returns a friendly "call us for a quote" message instead
of billing anything).

---

## 4. Quote API

`POST /api/quote/` (JSON). The widget calls this; you can too.

**Request**
```json
{
  "service_type": "city_to_city",   // or "hourly"
  "vehicle_class": "suv",            // towncar | mini_van | suv | van | sprinter
  "date": "2026-09-01",
  "origin": "Orlando",               // city_to_city
  "destination": "Miami",            // city_to_city
  "route_id": 9,                     // optional, from the dropdown
  "hours": 3                         // hourly
}
```

**Response (200)**
```json
{
  "ok": true,
  "token": "…uuid…",
  "book_url": "/book-quote/…uuid…/",
  "base_price": 950.0,
  "peak_adjustment": null,
  "gratuity": 190.0,
  "total": 1140.0,
  "all_inclusive": true,
  "price_source": "route_table",     // route_table | formula | hourly
  "vehicle_class": {"key": "suv", "name": "SUV"},
  "trip": {"origin": "Orlando", "destination": "Miami (MIA)", "hours": null, "minimum_hours": null, "loaded_miles": 235.0},
  "notes": ["All-inclusive — tolls included."]
}
```

**Response (400)** — validation errors return
`{"ok": false, "error": {"code": "...", "field": "...", "message": "..."}}`
(e.g. `below_minimum`, `missing_destination`, `unknown_class`,
`distance_unavailable`).

---

## 5. Quote → booking hand-off (no re-entry)

The API stores an `InstantQuote` and returns a `token` + `book_url`. The Reserve
button links to `/book-quote/<token>/`, which:

1. Shows the locked-in quote (vehicle, date, price breakdown).
2. Collects only contact + pickup details.
3. Creates a **rate-less** `Reservation` (`service_type` = hourly/city_to_city,
   `rate = NULL`, `base_price`/`gratuity_amount`/`total_price` copied from the
   stored quote — never re-priced) and a `Leg`, then redirects to the **existing**
   Stripe checkout (`create_checkout_session`). No new payment code.

The legacy rate-based booking flow (`/book-orlando-transportation/<rate_id>`) is
untouched.

> **Schema note:** `Reservation.rate` is now nullable to allow rate-less
> hourly/city-to-city bookings. New fields: `Reservation.service_type` (default
> `transfer`, so all existing reservations are unchanged) and
> `Reservation.quoted_hours`. Null-safe `route_label` / `vehicle_label`
> properties back the Stripe metadata and commission-statement email.

---

## 6. Environment / config

| Setting | Default | Purpose |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | "" | Distance Matrix key (already used by drivers). Empty disables live lookups. |
| `PRICING_ALLOW_LIVE_DISTANCE` | `1` | Set `0` to forbid any live Distance Matrix call from the quote engine. |

No new third-party packages. New app: add `pricing.apps.PricingConfig` to
`OUR_APPS` (done). Run `python manage.py migrate` — migration `0001_initial`
creates the tables and `0002_seed_pricing` seeds the launch config (idempotent;
safe to run on an already-seeded DB; never overwrites edited values).

---

## 7. Tests

`python manage.py test pricing` (23 tests) covers: the named-route quote
(Miami SUV = $1,140), the **named-route zero-Distance-Matrix guarantee**, the
formula path + floor, hourly rate × hours with the 3-hr minimum enforced, the
30-min increment rule, the Sprinter peak minimum, peak applied only on configured
dates, the API endpoint, and the quote→rate-less-reservation hand-off.
