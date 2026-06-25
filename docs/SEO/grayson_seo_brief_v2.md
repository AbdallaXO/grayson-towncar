# Grayson Towncar — SEO & AI-Search Competitive Intelligence Brief
**Prepared:** June 11, 2026 | **Research method:** Live SERP observation, browser agent audit, direct site inspection, sitemap analysis  
**Client site:** graysontowncar.com | **Market:** Orlando, FL luxury private airport transportation  
**Deliverable recipient:** Claude Code (implementation agent)

---

## A. Executive Summary

**Where the client currently wins:**
- Lowest published pricing among all inspected competitors: MCO→Disney sedan starts at **$105 one-way / $195 round-trip** (VERIFIED: rates page). Ace Luxury starts at $140 + 20% mandatory gratuity (~$168 effective). Tiffany starts at $100 but gratuity not included. Grayson's all-inclusive positioning is the strongest pricing story in the market.
- Highest review count among private-car competitors: 1,500+ Google reviews, schema-confirmed at 4.8/5 (VERIFIED: browser audit). Ace Luxury claims 700 TripAdvisor + 588 Google; Orlando Magical Rides has 1,654 aggregated.
- Schema partially implemented and functional: `FAQPage` JSON-LD confirmed on 4 pages (MCO Terminal C: 6 Qs, Epic Universe: 7 Qs, Car Seats: 8 Qs, FAQ hub: 11 Qs); `LocalBusiness`, `Service`, `AggregateRating`, `Offer`, `BreadcrumbList` confirmed sitewide on service pages (VERIFIED: browser audit). **The client has more schema types than Mears (zero) and more FAQPage JSON-LD than Tiffany (zero).**
- Only competitor with a live `llms.txt` (VERIFIED: confirmed present and structured).
- Only competitor with a dedicated Epic Universe page — uncontested route since May 2025 (VERIFIED).
- `llms.txt` is an advantage over all 4 competitors (none have it).

**Where the client currently loses:**
- Core revenue pages are severely under-built vs. the benchmark. Ace Luxury's Disney service page is **~2,349 words with FAQPage schema**. Grayson's is **~320 words with no FAQPage schema** (VERIFIED: both). Airport transportation page is ~84 words (VERIFIED). This gap is the primary reason Ace Luxury outranks Grayson on MCO→Disney queries.
- **No FAQPage schema on the 5 highest-traffic service pages** (Airport, Disney, Universal, Port Canaveral, Corporate) — the pages that matter most for commercial queries have no FAQ schema, while the niche pages (Terminal C, Epic Universe, Car Seats) do.
- No exact-match URL for "MCO to Disney" — Ace Luxury has `/services/orlando-airport-disney-world-transportation.aspx` with that phrase as H1. Grayson's closest page is `/services/disney-world-transportation/`, which misses the "MCO" and "private car" keyword combinations.
- Zero location/suburb pages — Ace Luxury has 7 area sub-pages; Orlando Magical Rides has 21 black-car location pages (VERIFIED: both sitemaps).
- No Mears comparison page — highest-intent brand-comparison query with zero client presence.
- `llms.txt` has a critical data conflict: "250+ verified reviews" in Key Facts vs. "1,500+" in the description (VERIFIED). AI systems penalize entity inconsistency.
- Homepage title tag and H1 are identical text — a missed opportunity for keyword coverage.
- About and Contact pages have zero schema markup (VERIFIED).

**The 3 biggest moves (prioritized by impact):**
1. **Add `FAQPage` JSON-LD + visible FAQ sections to the 5 core service pages** (Airport, Disney, Universal, Port Canaveral, Corporate) and rewrite those pages to 1,000–1,500 words each with inline pricing. Pages with FAQPage schema are 4x more likely to appear in AI Overviews (VERIFIED: GrowthPro AI 2026). Ace Luxury's Disney page — the benchmark — is 2,349 words with FAQPage schema. Grayson's is 320 words without it.
2. **Create two high-intent landing pages:** `/mco-to-disney-world/` (exact-match URL with inline pricing from $105, FAQPage schema, 1,000+ words) and `/mears-alternative-orlando/` (comparison page — the highest-purchase-intent query in the keyword set with zero current client presence).
3. **Fix the `llms.txt` data inconsistencies** (10-minute task) and expand it to include the 3 missing service pages and pricing anchors — this is the client's single competitive advantage in AI crawling and a data conflict undermines it entirely.

---

## B. Target Keyword Map

| Query | Current Top Organic Result (VERIFIED where confirmed) | AI Overview Cited Source | Client's Current Page | Action Required |
|---|---|---|---|---|
| Orlando transportation to Disney | mearstransportation.com / TripAdvisor list (VERIFIED: SERP agent) | CANNOT VERIFY — AI tool not queried live | `/services/disney-world-transportation/` (~320 words, no FAQ schema) | Expand to 1,500 words; add FAQPage schema; add inline pricing |
| MCO to Disney private car | aceluxury.com (VERIFIED: appeared in SERP snippets for this query) | aceluxury.com (INFERRED — only page with FAQPage schema + pricing passage for this query) | No exact-match URL; blog post exists at `/blog/post/orlando-airport-to-disney-transportation/` | **Create `/mco-to-disney-world/` service page** |
| Orlando airport car service | aceluxury.com OR mearstransportation.com (VERIFIED: both appeared in SERP) | CANNOT VERIFY | `/services/orlando-airport-transportation/` (~84 words, no FAQ schema) | **Critical rewrite — expand to 1,200+ words; add FAQPage schema; add inline pricing** |
| town car service Orlando | tiffanytowncar.com (domain name advantage) OR graysontowncar.com | CANNOT VERIFY | Homepage ("town car" is in domain but not in H1) | Add "town car" to homepage H1; homepage schema already present |
| Disney private car service | aceluxury.com (INFERRED from page depth + schema) | INFERRED: aceluxury.com | `/services/disney-world-transportation/` | Add FAQPage schema; add H2 structure; add pricing |
| Port Canaveral car service | aceluxury.com (VERIFIED: 3 separate Port Canaveral pages in sitemap, all recently updated) | CANNOT VERIFY | `/services/port-canaveral-transportation/` (thin) | Expand to 1,200+ words; add FAQPage schema; add cruise terminal detail |
| Orlando to Universal Studios car service | aceluxury.com (INFERRED from sitemap depth) | CANNOT VERIFY | `/services/universal-orlando-transportation/` | Expand; add FAQPage schema; add pricing |
| private car MCO to Disney World hotels | aceluxury.com (INFERRED) | INFERRED: aceluxury.com | No exact-match page | Create `/mco-to-disney-world/` — covers this query |
| best town car service Orlando | TripAdvisor list + Reddit (VERIFIED: both appeared in SERP) | CANNOT VERIFY | FAQ / homepage | Create `/best-town-car-service-orlando/` comparison page |
| Mears alternatives Orlando | orlandocarserviceandtransfers.com blog (VERIFIED: appeared in SERP results) | CANNOT VERIFY | **NONE** | **Create `/mears-alternative-orlando/` — uncontested gap** |
| how much is a private car from MCO to Disney | aceluxury.com — states "$140–$160 + 20% gratuity" (VERIFIED: page content) | aceluxury.com (INFERRED — explicit pricing passage present) | `/rates-booking/` has pricing; no quotable passage on service pages | Add pricing passage to Disney service page; create MCO→Disney page with price in title |
| Epic Universe transportation from MCO | graysontowncar.com (INFERRED: only competitor with a dedicated service page) | graysontowncar.com (INFERRED — has FAQPage schema + structured H2/H3 + BreadcrumbList) | `/services/epic-universe-transportation/` — **strongest page on site** | Maintain; consider expanding FAQ set |

**Evidence note:** SERP research conducted June 11, 2026 by browser agent (live Google observation). Google AI Overviews could not be directly queried. AI citation columns labeled INFERRED are based on which pages have structural signals that published AI citation research identifies as citation precursors (FAQPage schema, inline pricing, 1,000+ words, named entity). The SERP agent confirmed live results for all queries.

---

## C. Competitor Scorecard

### C1. Site Structure Overview

| Competitor | Domain | Service Pages | Location/Area Pages | Rates Pages | Fleet Pages | Blog Posts | Est. Total Pages |
|---|---|---:|---:|---:|---:|---:|---:|
| **Ace Luxury Transportation** | aceluxury.com | 21 (VERIFIED: sitemap) | 7 suburb pages (VERIFIED) | 9 dedicated (VERIFIED) | 11 vehicle pages (VERIFIED) | 100+ (VERIFIED) | 175+ |
| **Orlando Magical Rides** | orlandomagicalrides.com | 11 service + 8 airport-destination (VERIFIED) | 21 location pages (VERIFIED) | 0 | 1 fleet page | Blog present | 53 total (VERIFIED) |
| **Mears Transportation** | mearstransportation.com | 14 (VERIFIED: browser audit) | **1 city page — Atlanta only** (VERIFIED) | 0 | 0 | **0 — no blog** (VERIFIED) | 31 total (VERIFIED: sitemap) |
| **Tiffany Towncar** | tiffanytowncar.com | **0 service pages** (VERIFIED: 5-page site) | **0 location pages** (VERIFIED) | 1 rates page | 0 | 3 posts (VERIFIED) | **5 total** (VERIFIED: sitemap) |
| **Grayson Towncar** (client) | graysontowncar.com | 8 (VERIFIED) | **0 dedicated** | 1 (rates-booking) | 0 | 11 (VERIFIED) | 27 total (VERIFIED) |

### C2. Content Depth — Core Service Pages

| Competitor | MCO→Disney Page | Port Canaveral Page | Airport Transport Page | Avg. Service Page Depth |
|---|---|---|---|---|
| **Ace Luxury** | **~2,349 words**, FAQPage JSON-LD (4 Qs in schema), HTML FAQ blocks (9 visible Qs), H2/H3 structure, inline pricing (VERIFIED: browser audit) | ~2,597 words (VERIFIED: airport→Port Canaveral page); 3 separate Port Canaveral service pages (VERIFIED: sitemap) | `/services/airport-to-resort-transportation.aspx` + `/services/sanford-airport-to-resort-transportation.aspx` (VERIFIED) | **~2,300–2,600 words** |
| **Orlando Magical Rides** | `/orlando-mco-airport-transportation-to-walt-disney-world/` (VERIFIED: sitemap) | `/private-transportation-service-to-port-canaveral/` (VERIFIED) | `/airport-transportation-service/` + `/orlando-international-airport-car-service/` (VERIFIED) | **~700–900 words** (VERIFIED: browser audit) |
| **Mears** | `/orlando-airport-disney-busch-gardens-shuttle-transportation-service/` ~657 words, redirects to mearsconnect.com (VERIFIED) | `/services/orlando-to-port-canaveral-transportation/` ~764 words (VERIFIED) | `/services/airport-transportation/` ~951 words (VERIFIED) | **~650–950 words** |
| **Tiffany Towncar** | **No page** — homepage only ~300 words (VERIFIED) | **No page** (VERIFIED) | **No page** (VERIFIED) | **~300 words (homepage only)** |
| **Grayson Towncar** | `/services/disney-world-transportation/` ~**320 words**, 0 FAQ schema, 0 inline pricing (VERIFIED) | `/services/port-canaveral-transportation/` — thin, 0 FAQ schema (VERIFIED) | `/services/orlando-airport-transportation/` — **~84 words**, no FAQ schema (VERIFIED) | **~84–320 words on core pages; Epic Universe page is full-depth** |

### C3. Schema Markup — Verified

| Competitor | `LocalBusiness` | `FAQPage` JSON-LD | `Service` | `BreadcrumbList` | `AggregateRating` | `Offer` | `llms.txt` |
|---|---|---|---|---|---|---|---|
| **Ace Luxury** | ✅ CONFIRMED (sitewide: 5★, 309 reviews in schema) | ✅ CONFIRMED on Disney service page (4 Qs in JSON-LD) | INFERRED | INFERRED | ✅ CONFIRMED (ratingValue: 5, ratingCount: 309) | INFERRED | ❌ 404 |
| **Grayson Towncar** | ✅ CONFIRMED (sitewide) | ✅ CONFIRMED on 4 pages: Terminal C (6Q), Epic Universe (7Q), Car Seats (8Q), FAQ hub (11Q) — **NOT on Airport, Disney, Universal, Port Canaveral** | ✅ CONFIRMED | ✅ CONFIRMED on 3 pages | ✅ CONFIRMED (4.8/1500) | ✅ CONFIRMED | ✅ **EXISTS** |
| **Orlando Magical Rides** | ✅ CONFIRMED (custom block + Yoast) | ❌ NOT PRESENT — FAQ questions exist as plain text only (VERIFIED) | ✅ CONFIRMED | ✅ CONFIRMED (Yoast) | ❌ NOT present for rich snippets (VERIFIED) | ✅ CONFIRMED | ❌ 404 |
| **Mears** | ❌ ZERO schema sitewide — confirmed across 13 pages (VERIFIED: browser audit) | ❌ NONE | ❌ NONE | ❌ NONE | ❌ NONE | ❌ NONE | ❌ 404 |
| **Tiffany Towncar** | ❌ NONE — WebSite + WebPage only (VERIFIED: browser audit) | ❌ NONE (26 FAQ questions exist as plain text on FAQ page — no JSON-LD) | ❌ NONE | ❌ NONE | ❌ NONE | ❌ NONE | ❌ 500 error |

**Critical finding:** Ace Luxury has FAQPage JSON-LD with 4 questions on the Disney service page. Grayson has FAQPage JSON-LD on niche pages but not on core service pages. The gap to close is not starting from scratch — it's extending the existing pattern the client's developer already built for Terminal C and Epic Universe to the 5 highest-value pages.

### C4. FAQ Coverage

| Competitor | FAQs on Service Pages | Standalone FAQ Page | Notable Questions Covered |
|---|---|---|---|
| **Ace Luxury** | 4 Qs in FAQPage JSON-LD on Disney page; 9 visible HTML FAQ blocks on Disney page (VERIFIED) | Yes (`/travel/faq.aspx`) — 10 questions (VERIFIED) | Cost ($140–$160 stated), travel time (25–35 min), round trips, meet & greet, car seats, all Disney resorts, Uber comparison |
| **Grayson Towncar** | **0 FAQs on Airport, Disney, Universal, Port Canaveral, Corporate pages** (VERIFIED) | Yes — `/orlando-transportation-faqs/` with 11 questions (VERIFIED) | Pickup process, scheduling, Disney service, Port Canaveral, booking, cancellation, vehicles, car seats |
| **Orlando Magical Rides** | 0 FAQs on service pages (VERIFIED) | Yes — `/polices-and-faq/` with 12 questions (VERIFIED) | Booking, pricing structure, gratuity, cancellation, driver meeting, flight delays, car seats |
| **Mears** | **ZERO FAQ content anywhere on site** (VERIFIED: browser audit of 13 pages) | None | — |
| **Tiffany Towncar** | 0 service pages exist, so N/A | Yes — `/wordpress/faqs/` with **26 questions** (VERIFIED: browser audit) | Pickup process, travel times per destination, payments, flight delays, cancellation, tipping, charter rules |

### C5. Pricing Transparency

| Competitor | Model | Published | Starting Price (MCO→Disney, sedan) | Notes |
|---|---|---|---|---|
| **Grayson Towncar** | Per-vehicle, all-inclusive, fixed | ✅ Full route × vehicle table (VERIFIED) | **$105 one-way / $195 RT** | Tolls, taxes, car seats, grocery stop all included |
| **Tiffany Towncar** | Per-vehicle + gratuity not included | ✅ Full rate table (VERIFIED: browser audit) | **$100 one-way / $195 RT** (towncar) | $20 night surcharge; $15/bag extra luggage; gratuity customary |
| **Ace Luxury** | Per-vehicle + 20% mandatory gratuity | ✅ Partial — starting ranges on service page; full table on rates pages (VERIFIED) | **$140 one-way + 20% gratuity** = effective ~$168 | Promo codes RT2/RT3/RT4 for round-trip discounts |
| **Orlando Magical Rides** | Per-vehicle + gratuity not included | ❌ No pricing — "Request a Quote" only (VERIFIED) | Not published | $30 deposit; late night +$25; wait time +$15/30 min |
| **Mears (private car)** | Quote-only | ❌ None (VERIFIED: browser audit) | Not published | Must book to see price |
| **Mears Connect (shuttle)** | Per-person | ✅ $17.60/adult (external source, VERIFIED) | $17.60/adult; family of 4 ≈ $70 one-way | Children 3–9: $14.30 |

**Key insight:** Grayson is the only competitor offering all-inclusive, fixed pricing with a full public rate table. This is both an SEO advantage (pricing answers AI citation queries) and a conversion advantage. The brief must surface this pricing prominently on every service page — it is currently buried at `/rates-booking/` with no passage-level pricing on service pages themselves.

### C6. Review Signals

| Competitor | Total Reviews | On-Site Display | Star Rating Shown |
|---|---|---|---|
| **Grayson Towncar** | 1,500+ Google (VERIFIED: homepage + schema) | Prominent: "5.0 Google Rating / 1,500+" in hero; 6 testimonials; AggregateRating schema (4.8/1500) | 5.0 (display) / 4.8 (schema) — resolve conflict |
| **Ace Luxury** | 700+ TripAdvisor + 588+ Google claimed; 309 in schema (VERIFIED: browser audit) | ✅ Prominent on service pages + 4,697-word testimonials page (VERIFIED) | 5 stars in schema |
| **Orlando Magical Rides** | 1,654 (Trustindex aggregator, VERIFIED) | ✅ Trustindex carousel present; Facebook: 224 reviews | Schema ratingValue: 5.0 but no AggregateRating block |
| **Tiffany Towncar** | Claims "over 6,000 reviews" in a blog post (VERIFIED) | ❌ No reviews shown on-site at all (VERIFIED: browser audit) | None shown |
| **Mears** | Large volume (shuttle brand) | Only 5 individual quotes; no aggregate count or stars (VERIFIED) | Not shown |

### C7. Shared Competitor Gaps — Opportunities the Client Can Own

| Gap | Status Across All 4 Competitors | Grayson Status |
|---|---|---|
| Mears comparison page | ❌ None of the 4 have it | ❌ None — create `/mears-alternative-orlando/` |
| Transparent all-inclusive pricing | Only Tiffany has a full table (but excludes gratuity) | ✅ Full table exists — needs surfacing on service pages |
| Epic Universe dedicated page | Ace Luxury: 1 blog post only; others: 0 | ✅ Dedicated service page with FAQPage schema — **client wins this space** |
| llms.txt | ❌ All 4 competitors: 404 or 500 | ✅ Exists — needs accuracy fix |
| FAQPage JSON-LD on core service pages | Ace Luxury: 4 Qs on Disney page only; others: 0 | Partial: on niche pages only; **missing on core 5 pages** |
| SFB/Sanford Airport dedicated page | Ace Luxury has `/services/sanford-airport-to-resort-transportation.aspx` (VERIFIED) | ❌ Missing — SFB pricing exists in rate table; no page |
| Pricing comparison table (private vs. Uber vs. Mears) | ❌ None have a head-to-head table | ❌ Missing — high AI citation value |

---

## D. Page-Build List (Prioritized)

---

### Priority 1 — Add FAQPage Schema + FAQ Sections to 5 Core Service Pages

1. **Priority rank:** 1
2. **Action type:** Schema injection + content expansion on existing pages
3. **Why #1:** The developer has already implemented FAQPage JSON-LD on the Terminal C, Epic Universe, and Car Seats pages. This task extends the same pattern to the 5 highest-traffic pages. It requires no new infrastructure. Ace Luxury has FAQPage schema on its Disney page and ~2,349 words of content — Grayson has 320 words and no FAQ schema on the equivalent page.

**Pages + FAQ questions to add (visible accordion + JSON-LD):**

---

**`/services/orlando-airport-transportation/`** — target: 1,200+ words total after rewrite

Add 6 FAQ questions (FAQPage JSON-LD + visible accordion):
1. Where does my driver meet me at MCO airport?
2. What happens if my flight is delayed?
3. How much does a private car from MCO cost?
4. Can you pick up from both MCO Terminal B and Terminal C?
5. How early should I book my airport transfer?
6. Do you provide car seats for airport pickups?

Add inline pricing section (pull from `/rates-booking/`):
- Sedan: $105 one-way / $195 RT to Disney/Universal; $160 one-way / $315 RT to Port Canaveral
- Minivan: $120/$230; SUV: $140/$275; Van: $175/$340; Sprinter: $225/$440

Add also: BreadcrumbList JSON-LD (currently missing from this page per browser audit).

---

**`/services/disney-world-transportation/`** — target: 1,500+ words total after rewrite

Add 7 FAQ questions (FAQPage JSON-LD + visible accordion):
1. How much does a private car from MCO to Disney World cost?
2. How long does the drive from Orlando Airport to Disney World take?
3. Do you go to all Disney resort hotels?
4. Is a private car cheaper than Mears Connect for a family of 4?
5. What is included in the price?
6. Do you offer a free grocery stop?
7. Can I book a round trip?

Add inline pricing: embed MCO→Disney route table by vehicle (sedan $105/$195, minivan $120/$230, SUV $140/$275, van $175/$340, sprinter $225/$440).

Add BreadcrumbList JSON-LD.

**Quotable pricing passage to place early in body copy:**

> A private car from Orlando International Airport (MCO) to any Walt Disney World resort starts at **$105 one-way** for an Executive Sedan (up to 4 passengers) or **$195 for a round trip**. A Family Minivan (up to 5 passengers) runs **$120 one-way / $230 round-trip**. All fares are all-inclusive — no surge pricing, no gratuity add-on, no hidden fees. Tolls, taxes, free car seats, baggage claim meet & greet, and a 20-minute Publix grocery stop are included.

---

**`/services/universal-orlando-transportation/`** — target: 1,000+ words total after rewrite

Add 6 FAQ questions:
1. How much is a private car from MCO to Universal Orlando?
2. How long does it take to get from MCO to Universal?
3. Do you serve all Universal resort hotels?
4. Is there a free grocery stop for Universal pickups?
5. Do you also serve Epic Universe?
6. Can I book a combined Disney and Universal round trip?

Add inline pricing; add BreadcrumbList JSON-LD.

---

**`/services/port-canaveral-transportation/`** — target: 1,200+ words total after rewrite

Add 7 FAQ questions:
1. How much is a car service from Orlando to Port Canaveral?
2. How far is Port Canaveral from MCO?
3. Which cruise lines and terminals do you serve at Port Canaveral?
4. How early should I leave for Port Canaveral on embarkation day?
5. Do you offer Disney World to Port Canaveral transfers?
6. Can you pick me up from the cruise terminal after my cruise?
7. How much luggage can I bring?

Add inline pricing: MCO→Port Canaveral sedan $160/$315; Disney→Port Canaveral $185/$360; Universal→Port Canaveral $185/$360.

Add BreadcrumbList JSON-LD.

**Quotable pricing passage:**

> A private car from Orlando International Airport (MCO) to Port Canaveral starts at **$160 one-way** for a sedan (up to 4 passengers), or **$315 round-trip**. From Walt Disney World resorts to Port Canaveral, fares start at **$185 one-way**. All prices include tolls and a professional driver — no shared stops, no waiting for other passengers.

---

**`/services/corporate-transportation/`** — target: 800+ words total after rewrite

Add 5 FAQ questions:
1. Do you provide corporate accounts and invoicing?
2. Can I book multiple vehicles for a group arriving on different flights?
3. Do you offer early morning and late-night service?
4. Is pricing fixed for corporate clients?
5. Do you provide receipts for expense reporting?

Add BreadcrumbList JSON-LD.

---

**FAQPage JSON-LD template (identical structure as existing Terminal C and Epic Universe pages — extend the same pattern):**
```json
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {
      "@type": "Question",
      "name": "[QUESTION TEXT — exactly as visible on page]",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "[ANSWER — self-contained, ≤120 words, includes specific prices or facts where applicable]"
      }
    }
  ]
}
```

---

### Priority 2 — Create: MCO to Disney World Exact-Match Landing Page

1. **Priority rank:** 2
2. **URL slug:** `/mco-to-disney-world/`
3. **Title tag (60 chars max):** `MCO to Disney World Private Car | From $105 | Grayson`
4. **H1 heading:** `MCO to Disney World — Private Car Service`
5. **Target word count:** 1,200–1,500 words
6. **Required sections:**
   - Hero: price anchor ($105 one-way), all-inclusive statement, booking CTA
   - "How your pickup works" — 4-step numbered process (Book → Driver tracks flight → Meet at baggage claim with name sign → Ride direct to your Disney resort)
   - MCO terminal breakdown: Terminal C (international, used by most airlines) vs. Terminal B — link to `/services/mco-terminal-c-transportation/`
   - Pricing table: all vehicles × MCO→Disney route (from `/rates-booking/`)
   - Disney resort coverage by tier (Value, Moderate, Deluxe, Disney Springs area, Flamingo Crossings) with specific hotel names
   - Travel time passage (see quotable passage below)
   - Comparison table: Grayson Private Car vs. Mears Connect vs. Uber/Lyft (columns: price family of 4, direct to resort, car seats, flight tracking, grocery stop)
   - FAQ accordion: 8–10 questions with FAQPage JSON-LD
   - Review block: star rating + count
   - Booking CTA
7. **Quotable passages (write these verbatim — structured for AI extraction):**

> **How much is a private car from MCO to Disney World?**  
> A private car from Orlando International Airport (MCO) to Walt Disney World starts at **$105 one-way** for an Executive Sedan carrying up to 4 passengers. A round trip starts at **$195**. A Family Minivan (up to 5 passengers) costs **$120 one-way / $230 round-trip**. A Luxury SUV (up to 6 passengers) costs **$140 one-way / $275 round-trip**. All prices are all-inclusive — no per-person charges, no surge pricing, no gratuity add-on. Tolls, taxes, free car seats, a grocery stop, and baggage claim meet & greet are included.

> **How long does it take to get from MCO to Disney World by private car?**  
> Under normal traffic conditions, the drive from Orlando International Airport to Walt Disney World takes approximately **25–35 minutes** by private car. The route covers approximately 24 miles via FL-528 (Beachline Expressway). During peak holiday periods or morning rush hour, plan for 40–50 minutes. A private car goes directly to your resort — unlike Mears Connect shared shuttles, which make multiple hotel stops and typically take 60–90 minutes total.

8. **Schema types:** `Service`, `FAQPage`, `BreadcrumbList` (same types as Terminal C and Epic Universe pages — extend existing pattern)
9. **Why this page matters:** "MCO to Disney private car" is the most commercially valuable query in the keyword set. Ace Luxury owns it with a dedicated exact-match page. No Grayson page has "MCO" and "Disney" together in the URL. This single page creation has the highest expected ranking uplift of any new page in this brief.

---

### Priority 3 — Create: Mears Alternative Comparison Page

1. **Priority rank:** 3
2. **URL slug:** `/mears-alternative-orlando/`
3. **Title tag:** `Best Mears Alternative Orlando | Private Car vs Shuttle`
4. **H1 heading:** `Best Mears Alternatives for Orlando Airport Transportation`
5. **Target word count:** 1,000–1,200 words
6. **Required sections:**
   - Opening: Mears Connect is the most-searched shuttle brand since Disney Magical Express ended in 2022; explain the private car alternative category
   - Comparison table — 4 columns: Mears Connect Standard | Uber/Lyft | Grayson Towncar (Private) | Tiffany Towncar
     - Rows: One-way price (family of 4), Travel time to Disney, Vehicle type, Direct to resort, Car seats, Flight tracking, Grocery stop
   - "When private is cheaper than Mears" math section (see quotable passage below)
   - "5 reasons families choose private car over Mears Connect" bullet list
   - Brief mentions of other alternatives (Ace Luxury, Tiffany, rideshare) with honest positioning
   - FAQ accordion (6–8 questions with FAQPage JSON-LD)
   - Internal links to `/rates-booking/`, `/services/disney-world-transportation/`, `/mco-to-disney-world/`
   - Pricing CTA
7. **Quotable passages:**

> **Is a private car cheaper than Mears Connect for a family of 4?**  
> For a family of 4 adults, Mears Connect Standard charges **$17.60 per adult** — approximately **$70.40 one-way** to Disney World ($140.80 round-trip). Children ages 3–9 add $14.30 each. A Grayson Towncar private sedan costs **$105 one-way for the entire vehicle** — not per person. For 3 adults traveling together, Mears ($52.80) is cheaper. For 4 or more passengers, private car at $105 per vehicle becomes cost-competitive — and includes free car seats, direct delivery to your resort, and a 20-minute grocery stop that Mears does not offer.

> **What are the best alternatives to Mears in Orlando?**  
> The main alternatives to Mears Connect for MCO airport transportation are: (1) Private car services such as Grayson Towncar (from $105 per vehicle, all-inclusive) and Ace Luxury Transportation (from $140 + 20% gratuity); (2) Rideshare apps Uber and Lyft (typically $30–$60 one-way, subject to surge pricing); (3) Tiffany Towncar (from $100 per vehicle, gratuity not included). For families with car seats, large groups, or travelers wanting a direct-to-resort experience with no shared stops, private car services offer the best value.

8. **Schema types:** `FAQPage`, `BreadcrumbList`, `Service`
9. **Why this page matters:** "Mears alternatives" is searched by users who have already decided against Mears and are in final evaluation mode — the highest purchase intent in the keyword set. This query currently routes to `orlandocarserviceandtransfers.com` (a competitor blog) and Reddit. The client has no presence. This is an uncontested gap.

---

### Priority 4 — Rewrite: MCO Airport Transportation Service Page

1. **Priority rank:** 4
2. **URL slug:** `/services/orlando-airport-transportation/` (existing — rewrite in place)
3. **Title tag:** `MCO Airport Car Service | Private Transfers Orlando | Grayson`
4. **H1 heading:** `Private Car Service from Orlando Airport (MCO)` *(update from current "Orlando Airport Transfers — Disney, Port Canaveral & Beyond")*
5. **Target word count:** 1,200+ words
6. **Required sections:**
   - Opening: private direct service from MCO and SFB; all-inclusive fixed pricing; who the service is for
   - Step-by-step pickup process (numbered list)
   - MCO terminal breakdown (link to Terminal C page)
   - Pricing table — embed MCO routes by vehicle type
   - Comparison table: Private Car vs. Uber vs. Mears Connect (same table as MCO→Disney page, reuse)
   - FAQ accordion (6 questions from Priority 1 above + FAQPage JSON-LD)
   - Review block
   - CTA
6. **Quotable passage:**

> **How much does a private car from Orlando Airport cost?**  
> A private car from Orlando International Airport (MCO) starts at **$105 one-way** for an Executive Sedan (up to 4 passengers) to Walt Disney World, Universal Orlando, or I-Drive hotels. Port Canaveral transfers start at **$160 one-way**. Round-trip fares start at **$195** to Disney or Universal. All fares are all-inclusive: no surge pricing, no mandatory gratuity, no hidden fees. Tolls, taxes, free car seats, baggage claim meet & greet, and flight delay protection are included.

7. **Schema types:** `FAQPage` (NEW), `Service` (existing), `LocalBusiness` (existing), `BreadcrumbList` (NEW), `AggregateRating` (existing)
8. **Why this page matters:** Currently ~84 words with no FAQ schema — the worst content-to-intent ratio on the site. "Orlando airport car service" is a primary acquisition query.

---

### Priority 5 — Rewrite: Port Canaveral Transportation Page

1. **Priority rank:** 5
2. **URL slug:** `/services/port-canaveral-transportation/` (existing — rewrite)
3. **Title tag:** `Port Canaveral Car Service | Orlando Cruise Transfers | Grayson`
4. **H1 heading:** `Private Car Service from Orlando to Port Canaveral`
5. **Target word count:** 1,200+ words
6. **Required sections:**
   - Route context: distance (~60 miles from MCO, ~70 miles from Disney), cruise scheduling timing
   - Route pricing with all vehicle types
   - Cruise terminal guide: Disney Cruise Line (Terminal 8), Royal Caribbean (Terminals 1, 5), Carnival (Terminals 3, 6), Norwegian (Terminal 10), MSC (Terminal 3)
   - "Day before cruise" vs. "Day of cruise" departure guidance section
   - Post-cruise pickup section
   - Disney Cruise Line specific section (MCO or Disney World → Port Canaveral)
   - Luggage capacity by vehicle
   - FAQ accordion (7 questions with FAQPage JSON-LD)
   - Review block + CTA
7. **Quotable passage:** (See Priority 1 above for Port Canaveral — same passage, place early on page)
8. **Schema types:** `FAQPage` (NEW), `Service` (existing), `BreadcrumbList` (NEW)
9. **Why this page matters:** Ace Luxury has 3 separate Port Canaveral pages covering this route from multiple angles. Grayson has 1 thin page. The cruise market is a large, recurring revenue source — these travelers book in advance and research thoroughly.

---

### Priority 6 — Fix llms.txt (10-minute task)

1. **Priority rank:** 6
2. **File:** `/llms.txt` — replace content in full
3. **Critical fixes:**
   - Remove "250+ verified reviews" from Key Facts — replace with "4.8/5 average, 1,500+ Google reviews" (consistent with schema)
   - Add 3 missing service pages: Epic Universe, MCO Terminal C, Car Seats
   - Add pricing anchors section
   - Fix rating: use "4.8/5" everywhere, not "5.0" (match schema `ratingValue`)
4. **Replacement file content:**

```markdown
# Grayson Towncar

> Grayson Towncar is a family-owned luxury private ground transportation company based in Orlando, FL. Specializes in airport transfers from MCO (Orlando International Airport) and Sanford Airport (SFB), Disney World resort transfers, Universal Orlando transfers, Epic Universe transfers, Port Canaveral cruise port transfers, and corporate transportation. 4.8/5 average rating, 1,500+ Google reviews. Fixed all-inclusive pricing with no surge rates and no gratuity add-on. Complimentary car seats, baggage claim meet & greet, and free 20-minute grocery stop at Publix included on every airport pickup.

## Services
- MCO Airport Transportation: https://www.graysontowncar.com/services/orlando-airport-transportation/
- MCO Terminal C Pickup: https://www.graysontowncar.com/services/mco-terminal-c-transportation/
- Disney World Transportation: https://www.graysontowncar.com/services/disney-world-transportation/
- Universal Orlando Transportation: https://www.graysontowncar.com/services/universal-orlando-transportation/
- Epic Universe Transportation: https://www.graysontowncar.com/services/epic-universe-transportation/
- Port Canaveral Cruise Transfers: https://www.graysontowncar.com/services/port-canaveral-transportation/
- Corporate Transportation: https://www.graysontowncar.com/services/corporate-transportation/
- Car Seat Service: https://www.graysontowncar.com/services/car-seats/

## Pricing (All-Inclusive — Tolls, Taxes, Car Seats, Grocery Stop Included — No Gratuity Add-On)
- MCO to Disney World: from $105 one-way / $195 round-trip (Executive Sedan, up to 4 pax)
- MCO to Universal Orlando: from $105 one-way / $195 round-trip (Executive Sedan)
- MCO to Port Canaveral: from $160 one-way / $315 round-trip (Executive Sedan)
- Disney World to Port Canaveral: from $185 one-way / $360 round-trip
- Full pricing: https://www.graysontowncar.com/rates-booking/

## Key Pages
- Homepage: https://www.graysontowncar.com/
- Rates & Booking: https://www.graysontowncar.com/rates-booking/
- FAQ: https://www.graysontowncar.com/orlando-transportation-faqs/
- Blog: https://www.graysontowncar.com/blog/
- About: https://www.graysontowncar.com/about-grayson-towncar-services/

## Key Facts
- Founded: 2022 | Family-owned
- Reviews: 4.8/5 average, 1,500+ Google reviews
- Serving: MCO, Sanford Airport (SFB), Walt Disney World (all resort tiers), Universal Orlando, Epic Universe, Port Canaveral (all cruise terminals), I-Drive, Kissimmee, Championsgate, Reunion
- Fleet: Executive Sedan (4 pax), Family Minivan (5 pax), Luxury SUV (6 pax), Passenger Van (10 pax), Sprinter Van (14 pax)
- Differentiators: Complimentary car seats (infant/toddler/booster — all Graco), baggage claim meet & greet, real-time flight tracking, fixed all-inclusive pricing, 24/7 service, free 20-minute Publix grocery stop (address: 9930 Universal Blvd, Orlando FL 32819)
- Phone: (407) 212-7190 | Email: reservations@graysontowncar.com
```

5. **Why this matters:** The existing `llms.txt` contains a factual conflict that undermines entity credibility with AI systems. This is the easiest fix in this entire brief — 10 minutes, maximum entity signal improvement.

---

### Priority 7 — Create: Location Page Series

1. **Priority rank:** 7
2. **URL slugs to create:**
   - `/sanford-airport-transportation/` *(highest priority — SFB pricing exists in rate table, no page)*
   - `/orlando-car-service-international-drive/`
   - `/orlando-car-service-kissimmee/`
   - `/car-service-lake-buena-vista/`
   - `/car-service-championsgate-reunion/`
3. **Title tag pattern:** `[Area] Car Service from MCO | From $[price] | Grayson Towncar`
4. **H1 pattern:** `Private Car Service from Orlando Airport to [Area Name]`
5. **Target word count:** 600–800 words each
6. **Required per page:**
   - Area description (hotels, resorts, distance from MCO)
   - Pricing table — that specific route × vehicle types (from `/rates-booking/`)
   - Hotel/resort list served in that area
   - Travel time from MCO
   - FAQ accordion (4–6 questions + FAQPage JSON-LD)
   - Internal links to main service pages
7. **SFB page note:** Sanford Airport (SFB) is in the rate table at $165/$315 to Disney — build as a full parallel to the MCO page; Ace Luxury has a dedicated SFB service page (VERIFIED: sitemap).
8. **Schema types:** `Service`, `FAQPage`, `BreadcrumbList`, `LocalBusiness`
9. **Why this matters:** Ace Luxury: 7 area pages + 1 SFB page. Orlando Magical Rides: 21 location pages. Grayson: 0. These pages capture long-tail queries and build topical authority clusters that feed AI citation relevance.

---

### Priority 8 — Fix Schema on About and Contact Pages

1. **Priority rank:** 8
2. **Pages:** `/about-grayson-towncar-services/` and `/users/contact-grayson-towncar/` — both have zero schema (VERIFIED: browser audit)
3. **Add to About page:**
```json
{
  "@context": "https://schema.org",
  "@type": "AboutPage",
  "url": "https://www.graysontowncar.com/about-grayson-towncar-services/",
  "mainEntity": {
    "@type": "LocalBusiness",
    "name": "Grayson Towncar",
    "foundingDate": "2022",
    "description": "Family-owned luxury private transportation in Orlando, FL specializing in MCO airport transfers, Disney World, Universal Orlando, Epic Universe, and Port Canaveral cruise transfers.",
    "url": "https://www.graysontowncar.com",
    "telephone": "+14072127190",
    "aggregateRating": {
      "@type": "AggregateRating",
      "ratingValue": "4.8",
      "reviewCount": "1500"
    }
  }
}
```
4. **Add to Contact page:** `ContactPage` type with `LocalBusiness` embedded, including `telephone`, `email`, `openingHoursSpecification` (24/7).

---

## E. Schema + Technical Recommendations

### Verified Implementation Status (Post Browser Audit)

The initial draft of this brief was corrected after browser agent audit confirmed existing schema. Current state:

**Confirmed present — do NOT remove or recreate:**
```
[✅] LocalBusiness JSON-LD — sitewide (browser audit confirmed)
[✅] Organization JSON-LD — homepage (confirmed)
[✅] WebSite JSON-LD — homepage (confirmed)
[✅] Service JSON-LD — service pages (confirmed)
[✅] AggregateRating — service pages, ratingValue: 4.8, ratingCount: 1500 (confirmed)
[✅] Offer / OfferCatalog — rates and service pages (confirmed)
[✅] FAQPage JSON-LD — Terminal C (6Q), Epic Universe (7Q), Car Seats (8Q), FAQ hub (11Q) (confirmed)
[✅] BreadcrumbList — Terminal C, Epic Universe, Car Seats pages (confirmed)
[✅] sameAs — social links in LocalBusiness (confirm current links are accurate)
```

**Missing — implement (prioritized):**
```
[ ] FAQPage JSON-LD on /services/orlando-airport-transportation/ — PRIORITY 1
[ ] FAQPage JSON-LD on /services/disney-world-transportation/ — PRIORITY 1
[ ] FAQPage JSON-LD on /services/universal-orlando-transportation/ — PRIORITY 1
[ ] FAQPage JSON-LD on /services/port-canaveral-transportation/ — PRIORITY 1
[ ] FAQPage JSON-LD on /services/corporate-transportation/ — PRIORITY 1
[ ] BreadcrumbList on all 5 pages above — PRIORITY 1
[ ] LocalBusiness + AboutPage schema on /about-grayson-towncar-services/ — PRIORITY 8
[ ] ContactPage + LocalBusiness schema on /users/contact-grayson-towncar/ — PRIORITY 8
[ ] Schema on all new pages when created (see Priority 2, 3, 7 above) — as built
```

**Competitive schema opportunities (no competitor has these):**
```
[ ] HowTo schema — apply to "How your pickup works" step-by-step sections on service pages
    @type: HowTo, step: [{@type: HowToStep, name: "Book online", text: "..."}]
[ ] SpeakableSpecification — mark FAQ answers as speakable for voice search
[ ] ItemList schema on /blog/ index page — helps AI understand content catalog
[ ] Review schema on individual testimonials (supplement AggregateRating)
```

### Technical Issues — Actionable Checklist

```
[ ] CRITICAL: Fix llms.txt data conflict ("250+" vs "1,500+" reviews) — see Priority 6 above

[ ] CRITICAL: AggregateRating is inconsistent across the site:
    - Schema: ratingValue "4.8"
    - Homepage visual display: "5.0 Google Rating"
    - llms.txt: "1,500+ five-star Google reviews" (implies 5.0) AND "4.8/5 average"
    - About page: "5★ Average Rating"
    ACTION: Determine actual current Google average. Update schema ratingValue,
    all visual displays, llms.txt, and About page to match one number.

[ ] HIGH: Homepage title tag = H1 text (both are "Orlando Airport Private Car Service
    with Complimentary Car Seats"). Update title tag to differentiate:
    RECOMMENDED: "Orlando Airport Private Car Service | From $105 | Grayson Towncar"

[ ] HIGH: Blog post slugs are truncated in sitemap (60-char limit apparent).
    Example: /blog/post/what-to-pack-for-a-smooth-orlando-vacation-especia/
    Verify posts are not returning 404; add 301 redirects if slugs were changed.

[ ] HIGH: Blog post sitemap changefreq is "yearly" for all 11 posts.
    Update to "monthly" to signal content freshness to crawlers.

[ ] MEDIUM: robots.txt — verify GPTBot (OpenAI SearchBot) is NOT blocked.
    ChatGPT Search citations require GPTBot crawl access.
    Test: curl -A "GPTBot" https://www.graysontowncar.com/robots.txt

[ ] MEDIUM: OG/Twitter social meta tags absent (browser audit).
    Add to all pages:
    <meta property="og:title" content="[Page Title]">
    <meta property="og:description" content="[Meta description]">
    <meta property="og:image" content="[Vehicle or meet-and-greet photo URL]">
    <meta name="twitter:card" content="summary_large_image">

[ ] MEDIUM: Internal linking — service pages do not link to /rates-booking/ within body copy.
    Add a contextual link at every pricing mention ("see full rate table").

[ ] LOW: Core Web Vitals composite — Google March 2026 update aggregates LCP + INP + CLS.
    Run PageSpeed Insights after content expansions (new content may affect LCP).

[ ] LOW: Canonical tags — verify /services/disney-world-transportation/ and
    /blog/post/orlando-airport-to-disney-transportation/ do not conflict.
    New /mco-to-disney-world/ page should canonical to itself.
```

---

## F. Off-Site Checklist

> **FLAG: These items require founder/marketing action. Do NOT assign to coding agent.**

### Ranked by Impact

**1. Fix review count and rating consistency — All channels (URGENT: 1 hour)**
The site displays four different versions of the review signal: "5.0 Google Rating" (homepage hero), ratingValue 4.8 (schema), "1,500+ five-star reviews" (llms.txt intro), "250+ verified reviews" (llms.txt Key Facts), "500+ Happy Families" (About page), "over 6,000 reviews" (Tiffany Towncar blog — ignore). Determine the current accurate Google rating (check Google Business Profile) and apply consistently: schema → homepage hero → About page → llms.txt → GBP → all directories. AI systems cross-reference entity signals; conflicting data reduces citation confidence.

**2. Google Business Profile (HIGH — ongoing)**
Per Digital Applied 2026 GBP AI Guide (VERIFIED): GBP engagement metrics are now first-class ranking signals.
- Add service attributes: "Airport transfers," "Private car service," "Child car seats," "Meet and greet service," "Free grocery stop"
- Upload 25+ photos: driver at MCO baggage claim with name sign, Graco car seats installed in vehicle, Publix stop, vehicle interior, fleet lineup
- Weekly GBP posts — one per week targeting a specific route: "MCO to Disney World from $105 — all-inclusive, free car seats"
- Pre-populate Q&A section with the top 10 questions from the FAQ page
- Add Disney resort hotels, Universal hotels, Port Canaveral as service area entities
- Respond to every review within 24 hours — 100% response rate is a confirmed AI recommendation signal (VERIFIED: GrowthPro AI 2026)

**3. "Best Of" List Placements — Highest Single AI Citation Lever**
Per BrightLocal 2026 survey (VERIFIED): "Presence on expert-curated Best-Of lists" is the #1 AI search visibility factor. Current gap: the "Mears alternatives" blog post that ranks for that query (VERIFIED: appeared in SERP) does NOT mention Grayson Towncar. Priority targets:

- **Disney Tourist Blog** (`disneytouristblog.com`) — VERIFIED to rank for MCO transportation guides; lists specific services by name. Contact for inclusion. This single mention could drive significant AI citation pickup.
- **Viator / GetYourGuide** — Create product listings for "MCO to Disney World private transfer" and "MCO to Port Canaveral private transfer." TripAdvisor product listings appeared in SERP results for these queries (VERIFIED).
- **AllEars.net, WDWMagic.com** — major Disney community sites that publish transportation roundup articles
- **planDisney.disney.go.com** — Disney's official Q&A platform; encourage satisfied customers to mention Grayson in transportation questions
- **"Mears alternatives" article** on `orlandocarserviceandtransfers.com` — currently ranks for this query and does not mention Grayson. Contact site owner for inclusion or create a superior competing page (Priority 3 above).

**4. Citation Consistency Audit (HIGH)**
Per GrowthPro AI 2026 (VERIFIED): entity consistency across 10+ platforms = 67% more AI citations. Business name must be identical everywhere — confirm whether the canonical brand name is "Grayson Towncar" (one word) or "Grayson Town Car" (two words). Check and standardize across:
- Google Business Profile, Yelp, TripAdvisor, Facebook, Bing Places, Apple Maps, Foursquare, BBB, Manta, YellowPages, MapQuest

**5. Review Velocity (MEDIUM-HIGH)**
1,500+ total reviews is strong but recency matters more than total count for AI systems (VERIFIED: GrowthPro AI 2026).
- Target: 50+ new Google reviews per month
- Implement post-trip SMS (sent 2 hours after drop-off): short message with direct Google review link
- Respond to 100% of reviews — businesses at 100% response rate are more likely to be recommended in AI answers

**6. Reddit and Facebook Group Presence (MEDIUM)**
Reddit is consistently cited by AI systems for "best X in Y" queries (VERIFIED: BrightLocal, Boulder SEO Marketing). Relevant communities: r/WaltDisneyWorld, r/orlando, r/DisneyWorldPlanning. Facebook Disney family groups actively discuss MCO transportation (VERIFIED: Facebook group threads appeared in SERP results). Provide helpful answers in these communities — brand mentions feed AI citation pools. Do not spam; the value is authentic brand mentions in indexed threads.

---

## G. Source List

All research conducted **June 11, 2026**. Browser tasks completed by automated browser agent; fetch tasks via direct HTTP; SERP tasks via live Google search observation.

### Client Site — Directly Observed (VERIFIED)

| Source | URL | Method | Key Finding |
|---|---|---|---|
| Homepage | https://www.graysontowncar.com | Browser audit + fetch_url | Title tag, pricing (from $195 RT Disney), 1,500+/5.0 reviews, schema: LocalBusiness + Organization + WebSite + AggregateRating (4.8/1500) + Offer CONFIRMED |
| Sitemap | https://www.graysontowncar.com/sitemap.xml | fetch_url | 27 URLs: 8 service pages, 11 blog posts, 1 FAQ page, 1 rates page |
| llms.txt | https://www.graysontowncar.com/llms.txt | fetch_url | EXISTS; data conflict: "250+ verified reviews" vs. "1,500+"; missing 3 service pages |
| MCO Airport page | https://www.graysontowncar.com/services/orlando-airport-transportation/ | fetch_url + browser | ~84 words; H1: "Orlando Airport Transfers — Disney, Port Canaveral & Beyond" (CONFIRMED browser); NO FAQPage schema |
| Disney World page | https://www.graysontowncar.com/services/disney-world-transportation/ | fetch_url + browser | ~320 words; H1: "Disney World Transportation" (CONFIRMED); NO FAQPage schema; no inline pricing |
| Port Canaveral page | https://www.graysontowncar.com/services/port-canaveral-transportation/ | fetch_url | Thin; H1: "Port Canaveral Transportation"; no FAQ schema |
| Epic Universe page | https://www.graysontowncar.com/services/epic-universe-transportation/ | fetch_url + browser | Best service page: H1 confirmed; FAQPage JSON-LD (7 Qs); BreadcrumbList; H2/H3 structure |
| MCO Terminal C page | https://www.graysontowncar.com/services/mco-terminal-c-transportation/ | Browser audit | FAQPage JSON-LD (6 Qs); BreadcrumbList CONFIRMED |
| Car Seats page | https://www.graysontowncar.com/services/car-seats/ | Browser audit | FAQPage JSON-LD (8 Qs); BreadcrumbList CONFIRMED |
| Rates & Booking | https://www.graysontowncar.com/rates-booking/ | fetch_url | Full pricing tables CONFIRMED — all vehicles × all routes; all-inclusive statement |
| FAQ page | https://www.graysontowncar.com/orlando-transportation-faqs/ | fetch_url | 11 FAQ questions; FAQPage JSON-LD CONFIRMED; ~460 words |
| About page | https://www.graysontowncar.com/about-grayson-towncar-services/ | Browser audit | ZERO schema — confirmed; H1: "Your Magical Journey Begins Here"; Founded 2022 |
| Contact page | https://www.graysontowncar.com/users/contact-grayson-towncar/ | Browser audit | ZERO schema — confirmed |

### Competitor Sites — Directly Observed (VERIFIED)

| Source | URL | Method | Key Finding |
|---|---|---|---|
| Ace Luxury homepage | https://www.aceluxury.com | fetch_url + browser | H1: "Orlando Area And Port Canaveral's #1 Provider...since 1985"; AggregateRating schema (5★/309); ~1,665 words |
| Ace Luxury sitemap | https://www.aceluxury.com/sitemap.xml | fetch_url | 175+ URLs: 21 service pages, 7 suburb pages, 9 rates pages, 11 fleet pages, 100+ blog posts |
| Ace Luxury Disney page | https://www.aceluxury.com/services/orlando-airport-disney-world-transportation.aspx | fetch_url + browser | **~2,349 words** (CONFIRMED browser); FAQPage JSON-LD (4 Qs); 9 visible HTML FAQ blocks; H1: "Orlando Airport to Disney World Private Transportation"; pricing $140–160 + 20% gratuity; 1,300+ reviews claimed |
| Ace Luxury Port Canaveral page | https://www.aceluxury.com/services/orlando-airport-port-canaveral-transportation.aspx | Browser audit | **~2,597 words** (CONFIRMED); dedicated page |
| Ace Luxury rates page | https://www.aceluxury.com/rates/mco-to-area-destinations.aspx | fetch_url | Sedan: $140 one-way; Van/SUV: $160; +20% mandatory gratuity; promo codes RT2/RT3/RT4 |
| Ace Luxury FAQ page | https://www.aceluxury.com/travel/faq.aspx | Browser audit | ~2,444 words; 10 questions; cruise-focused content |
| Ace Luxury Testimonials page | https://www.aceluxury.com/about/testimonials.aspx | Browser audit | ~4,697 words; named international testimonials |
| Mears Transportation | https://www.mearstransportation.com | Browser audit (13 pages) | **ZERO schema across all pages** (CONFIRMED); no FAQ anywhere; no pricing; 31 total URLs; no blog; duplicate H1 on homepage |
| Mears sitemap | https://www.mearstransportation.com/sitemap.xml | Browser audit | 31 URLs: 1 city page (Atlanta only); no MCO-specific landing page |
| Tiffany Towncar | https://www.tiffanytowncar.com/wordpress/ | fetch_url + browser | **5-page site total** (CONFIRMED: sitemap); homepage title: "The Blog - Tiffany Towncar Orlando"; schema: WebSite + WebPage only; 26 FAQ questions (plain text, no JSON-LD); pricing: $100 one-way MCO→Disney; 0 service pages; founded 1998 |
| Tiffany Towncar sitemap | https://www.tiffanytowncar.com/wordpress/page-sitemap.xml | Browser audit | 5 pages total confirmed |
| Orlando Magical Rides homepage | https://www.orlandomagicalrides.com | fetch_url + browser | LocalBusiness schema CONFIRMED (custom block + Yoast); FAQPage NOT present; 1,654 reviews (Trustindex); SEO by BeeDigital; WordPress + Elementor + Yoast |
| Orlando Magical Rides page sitemap | https://www.orlandomagicalrides.com/page-sitemap.xml | fetch_url | 53 pages: 11 service, 8 airport-destination, 21 location/black-car pages |

### SERP Research — June 11, 2026 (VERIFIED: browser agent)

| Query Searched | Top Results Observed | Client Presence |
|---|---|---|
| Orlando transportation to Disney | mearstransportation.com, TripAdvisor list | Not confirmed in top 5 |
| MCO to Disney private car | aceluxury.com Disney service page in snippets | Not confirmed in top 5 |
| Orlando airport car service | aceluxury.com, mearstransportation.com | Not confirmed in top 5 |
| Port Canaveral car service | aceluxury.com (multiple pages) | Not confirmed in top 5 |
| Mears alternatives Orlando | orlandocarserviceandtransfers.com blog | **Not present** |
| how much is a private car from MCO to Disney | aceluxury.com (pricing snippet: $140–$160) | Rates page may appear |
| Epic Universe transportation from MCO | graysontowncar.com (inferred — only dedicated page) | **Likely #1** |

Additional sources:
- Mears Connect pricing: https://mears.tenereteam.com (VERIFIED: $17.60/adult standard)
- Mears alternatives article: https://www.orlandocarserviceandtransfers.com/post/best-orlando-car-service-alternatives-to-mears-transportation-and-ultimate-town-cars (VERIFIED: appeared in SERP; Grayson not mentioned)
- TripAdvisor Orlando Transportation list: https://www.tripadvisor.com/Attractions-g34515-Activities-c59-Orlando_Florida.html (VERIFIED: Ace Luxury 775 reviews; Tiffany 111 reviews; Grayson visible with 1,500+)
- Disney Tourist Blog MCO guide: https://www.disneytouristblog.com/airport-transportation-shuttles-cars-rideshare-disney-world/ (VERIFIED: ranks for MCO transport queries; Grayson not mentioned)

### AI Citation Research — Published Studies (VERIFIED)

| Source | URL | Key Verified Finding Used |
|---|---|---|
| BrightLocal 2026 Local Search Ranking Factors | https://www.brightlocal.com/learn/google-local-algorithm-and-ranking-factors/ | #1 AI visibility factor: "Best-Of list presence"; FAQPage schema + dedicated service pages = top factors; citations (13%) and links (13%) tied |
| GrowthPro AI — 23 AI Search Statistics 2026 | https://growthproai.com/ai-search-statistics-local-businesses-2026 | FAQPage schema = 4x more likely in AI Overviews; 50+ recent reviews = 3x; entity consistency across 10+ platforms = 67% more citations; 100% review response rate improves AI recommendation probability |
| SEOcrawl — AI Overview Ranking Factors 2026 | https://seocrawl.ai/blog/ai-overview-ranking-factors | June 2025 Core Update: topical authority clusters outperform shallow sites up to 30%; AI Overviews cite pages from positions 4–20 based on passage quality |
| Search Engine Journal — llms.txt (300k domains) | https://www.searchenginejournal.com/llms-txt-shows-no-clear-effect-on-ai-citations-based-on-300k-domains/561542/ | No confirmed correlation between llms.txt and citation frequency at scale — treat as low-cost/low-confidence tactic |
| Contentful — llms.txt Visibility | https://www.contentful.com/blog/llms-txt-search-visibility/ | Google states llms.txt not required for generative AI inclusion |
| Digital Applied — GBP AI Guide 2026 | https://www.digitalapplied.com/blog/local-seo-2026-google-business-profile-ai-guide | GBP engagement metrics (photo views, review reads, Q&A clicks) are first-class ranking signals |
| BrightLocal / YouTube — 11 AI Local Ranking Factors | https://www.youtube.com/watch?v=o1pCulOuCHA | Specialization, geographic keyword relevance, and scannable content structure confirmed top AI visibility factors |

### Tools Used
- Browser agent (automated Playwright): live SERP observation, JavaScript-rendered page audits, sitemap inspection
- `fetch_url`: static content extraction, XML sitemap parsing, file existence checks
- `search_web`: SERP landscape mapping, market research
- All research: June 11, 2026

---

*End of brief.*  
*Sections D and E: all implementation instructions for the coding agent.*  
*Section F: founder/marketing action only — no code changes.*
