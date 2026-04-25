# Website Audit Action Plan

**Context.** A concrete, execution-ready audit of graysontowncar.com — a Florida luxury transportation business built on Django 5.1 and hosted on Railway. The audit is grounded in the actual codebase (templates, views, settings, static assets) and produced by parallel deep-dive passes on SEO, performance/Django, and UX/accessibility. Verifications spot-checked: `business/settings.py`, `content/templates/main.html`, raw image sizes on disk, and canonical/template patterns.

---

## 1. Executive Summary

### Biggest strengths
- **Solid on-page SEO primitives:** every major page has a unique, keyword-rich title + meta description and a single, intent-matching H1 (see [reservations/templates/reservations/index.html:5](reservations/templates/reservations/index.html#L5), [rates/templates/rates/index.html:8](rates/templates/rates/index.html#L8), [services/templates/services/disney-world-transportation.html:46](services/templates/services/disney-world-transportation.html#L46)).
- **Strong structured-data baseline:** `{% structured_data %}` tag ([reservations/templatetags/seo_tags.py](reservations/templatetags/seo_tags.py)) emits Organization+LocalBusiness with NAP, areaServed, ratings, and 24/7 hours. FAQPage schema is in place on the FAQs page. Service and BlogPosting schemas exist.
- **Hero is well-designed:** H1 matches search intent, embedded quote widget above the fold, trust badges (5.0 Google, Licensed & Insured, 24/7), brand partner logos (MCO, Disney, Universal, Carnival, Port Canaveral).
- **Django foundation is clean:** Whitenoise + Brotli, Redis cache in prod, custom `SlowRequestMiddleware`, correct `SECURE_PROXY_SSL_HEADER`, prefetch/select_related used in the main queryset in [reservations/views.py:57-73](reservations/views.py#L57-L73).
- **Sitemap coverage is good:** three sitemaps (static, services, blog) wired in [business/urls.py:27-36](business/urls.py#L27-L36) and referenced in `robots.txt`.

### Biggest weaknesses
- **A 9.5 MB hero JPG is shipping in production** (`content/static/images/disney-grand-floridian.jpg` — confirmed 9,994,191 bytes). This alone will tank mobile LCP and PSI scores. Several other 3–5 MB images exist (see §5).
- **Bootstrap CSS + Bootstrap Icons + Font Awesome all load render-blocking from 3 CDNs** in [content/templates/main.html:49-57](content/templates/main.html#L49-L57). No critical CSS inlined.
- **Canonical URLs only render if a view explicitly passes `canonical_url`** ([content/templates/main.html:39](content/templates/main.html#L39)); most pages don't → many pages currently have **no canonical tag**.
- **No Open Graph / Twitter Card tags on the homepage, service pages, rates, contact, about** — only the blog has them. Link previews on Facebook/iMessage/Slack will look broken.
- **No BreadcrumbList schema anywhere**, and breadcrumbs aren't in the visible UI either — a free local-SEO win being left on the table.
- **No sticky mobile "Book" / "Call" CTA** in the navbar; the homepage has sticky icon buttons but interior pages don't. Mobile users must scroll to find the booking path.
- **No view-level or template-fragment caching** on the static public pages (rates, service pages, blog) despite Redis being configured.
- **Template loader is not cached** ([business/settings.py:114-128](business/settings.py#L114-L128)) — every request re-parses templates.
- **Homepage service cards all link to `/rates-booking/` instead of to the matching service landing page**, diluting internal link equity and conversion specificity.
- **Booking form collects car-seat types inside a free-text "Special Requests" field** and gratuity as an unconstrained number input — both hurt conversion and data quality.

### Top 10 highest-value actions (ordered by ROI)
1. **Compress `disney-grand-floridian.jpg` (9.5 MB → <300 KB)** and ship AVIF/WebP variants — single biggest LCP fix. Do the same for `services-bg.webp` (5 MB), `contact-us-bg.webp` (3.3 MB), `universal.jpg` (1.7 MB), `universal-grand-helios.png` (1.2 MB).
2. **Add canonical fallback** to [content/templates/main.html:39](content/templates/main.html#L39) so every page has a self-referential canonical by default.
3. **Add Open Graph + Twitter Card blocks to `main.html`** with sensible defaults and per-page overrides; use the preloaded `full-fleet.webp` as the default OG image.
4. **Add a sticky mobile "Book / Call" bar** in [content/templates/navbar.html](content/templates/navbar.html) (visible `d-lg-none`). Expected +10–15% mobile conversions.
5. **Re-point homepage service card CTAs** to `/services/…/` pages (not all to `/rates-booking/`) in [reservations/templates/reservations/index.html](reservations/templates/reservations/index.html) — improves topical relevance, internal equity, and destination-intent conversion.
6. **Turn on the cached template loader** and add `@cache_page` / `{% cache %}` to rates, services, blog list, blog post, navbar, footer.
7. **Swap `SECURE_SSL_REDIRECT=False` → `True`, raise `SECURE_HSTS_SECONDS` to 31536000, swap `CompressedStaticFilesStorage` → `CompressedManifestStaticFilesStorage`** so static files get immutable 1-year cache headers.
8. **Defer Bootstrap, Bootstrap Icons, Font Awesome** with the `media="print" onload` pattern already used for Montserrat; inline ~2–3 KB of critical above-the-fold CSS.
9. **Add BreadcrumbList JSON-LD** to all service pages and blog posts (plus a visible breadcrumb UI above each H1).
10. **Rework booking form** ([reservations/forms.py:82-140](reservations/forms.py#L82-L140), [reservations/templates/reservations/book_form.html](reservations/templates/reservations/book_form.html)): dedicated car-seat-type fields instead of free text, gratuity as preset % buttons with live $ amount, and a proper post-submit confirmation page.

---

## 2. Critical Issues (High Impact, High Priority)

### C1. 9.5 MB hero image in production
- **Category:** Speed
- **Why it matters:** This is the single heaviest asset on the site. On a 4G connection it takes ~40 seconds to transfer — LCP will be catastrophic on mobile, PSI will score poorly, and it directly hurts both rankings (Core Web Vitals is a ranking signal) and conversion (bounce on slow pages). Verified size: 9,994,191 bytes.
- **Evidence:** `content/static/images/disney-grand-floridian.jpg` (9.5 MB), plus `services-bg.webp` 5.0 MB, `contact-us-bg.webp` 3.3 MB, `universal.jpg` 1.7 MB, `universal-grand-helios.png` 1.2 MB.
- **Recommended fix:** Re-encode all hero images as AVIF + WebP + compressed JPG fallback, target <300 KB per format for hero, use `<picture>` with `type` fallback, set explicit `width`/`height` to prevent CLS, `loading="eager"` + `fetchpriority="high"` for LCP heroes, `loading="lazy"` for everything else. Replace any `.png` photographic images with WebP.
- **Impact:** High · **Effort:** Low (one pass with `squoosh`/`sharp`/`cwebp`/`avifenc`)

### C2. Render-blocking third-party CSS on every page
- **Category:** Speed
- **Why it matters:** Bootstrap + Bootstrap Icons + Font Awesome load as synchronous `<link>` tags from three different CDNs in `<head>`. That's 3 DNS lookups + 3 TCP/TLS handshakes + 3 CSS parses before first paint.
- **Evidence:** [content/templates/main.html:49-57](content/templates/main.html#L49-L57). Montserrat correctly uses `media="print" onload="this.media='all'"` — the pattern just wasn't applied to the others.
- **Recommended fix:** Apply the same deferred-load pattern to Bootstrap Icons and Font Awesome. For Bootstrap CSS itself, consider self-hosting a trimmed build (you're on Bootstrap 5.3 and only use a subset of utilities) and inline ~2–3 KB of above-the-fold styles in a `<style>` block at the top of `<head>`.
- **Impact:** High · **Effort:** Medium

### C3. Canonicals missing on most pages
- **Category:** SEO
- **Why it matters:** [content/templates/main.html:39](content/templates/main.html#L39) renders the canonical tag only when `canonical_url` is in the template context. The base template has no fallback to `request.build_absolute_uri`. Spot-checked pages like the homepage, services list, and book form don't pass this variable. Without a canonical, duplicate-URL variants (trailing slash, UTM'd paid URLs, `/?fbclid=…`) fragment authority.
- **Evidence:** `content/templates/main.html:39`; individual page templates that don't set `canonical_url` in views.
- **Recommended fix:** Change the base template to always render a canonical, defaulting to the current absolute URL with querystring stripped:
  ```django
  <link rel="canonical" href="{{ canonical_url|default:request.build_absolute_uri|cut:'?' }}" />
  ```
  Or better, add a context processor that computes the canonical for every request and strips tracking params.
- **Impact:** High · **Effort:** Low

### C4. No Open Graph / Twitter metadata outside the blog
- **Category:** SEO / Conversion
- **Why it matters:** Facebook ads, iMessage previews, Slack shares, WhatsApp shares, Pinterest pins all use OG tags. Without them, your homepage and service pages preview as bare links, hurting social CTR and referral traffic. Blog pages have full OG/Twitter; nothing else does.
- **Evidence:** [content/templates/main.html](content/templates/main.html) has no default OG tags. Blog templates have them in [blog/templates/blog/blog_list.html:18-30](blog/templates/blog/blog_list.html#L18-L30) and [blog/templates/blog/blog_post.html:18-30](blog/templates/blog/blog_post.html#L18-L30) but that pattern wasn't extended.
- **Recommended fix:** Add a `{% block og_meta %}` to `main.html` with sensible defaults (brand OG image, homepage title/desc) and override in each major page template. Fields: `og:title`, `og:description`, `og:url`, `og:image` (absolute URL), `og:type`, `twitter:card=summary_large_image`, `twitter:title`, `twitter:description`, `twitter:image`.
- **Impact:** High · **Effort:** Low

### C5. No sticky mobile CTA on interior pages
- **Category:** UX / Conversion
- **Why it matters:** A Florida airport-transportation site lives and dies on phone calls and quick bookings from travelers on a phone. The navbar has no mobile CTA ([content/templates/navbar.html](content/templates/navbar.html)). The homepage has sticky icon buttons ([reservations/templates/reservations/index.html:1026-1040](reservations/templates/reservations/index.html#L1026-L1040)) — but service pages, rates, about, blog don't. Mobile users have to find the CTA themselves.
- **Evidence:** No sticky CTA bar component shared across templates.
- **Recommended fix:** Add a persistent mobile-only bottom bar (`d-lg-none position-fixed bottom-0`) with two buttons: `tel:+14072127190` (phone) and link to `/rates-booking/` (Book). Include in `main.html` so every page inherits. Give it `padding-bottom: env(safe-area-inset-bottom)` to avoid iOS notch overlap.
- **Impact:** High · **Effort:** Low

### C6. Homepage service cards all link to `/rates-booking/`
- **Category:** SEO + UX + Conversion
- **Why it matters:** The three service cards (Disney, Universal, Port Canaveral) on the homepage ([reservations/templates/reservations/index.html:334-442](reservations/templates/reservations/index.html#L334-L442)) all link to the rates page. That (a) wastes the strongest internal-link-equity hops on the site, (b) skips over the service landing pages you've already built, and (c) loses intent — a user who clicked "Disney" wants Disney content, not a generic pricing table.
- **Evidence:** Same page lines 364, 401, 438, plus "See Our Prices" CTA.
- **Recommended fix:** Point each service card to its `/services/<slug>/` page; keep a secondary "View pricing" link to rates. Every service page already has its own booking CTA.
- **Impact:** High · **Effort:** Low

### C7. No view-level or fragment caching on stable public content
- **Category:** Speed / Backend
- **Why it matters:** Rates, services, blog list, and blog posts are near-static but fully recomputed on every hit. Redis is already configured in [business/settings.py:159-172](business/settings.py#L159-L172); it's being left idle for page caching.
- **Evidence:** No `@cache_page` or `{% cache %}` usage detected in views/templates.
- **Recommended fix:**
  - `@cache_page(60*60)` on `rates:index`, `services:*`, `blog:blog_list`, `blog:blog_post` (cache-key-vary by path).
  - `{% cache 3600 navbar_{{ request.user.id|default:'anon' }} %}` around navbar dropdown menus.
  - `{% cache 7200 footer %}` around footer.
  - Use cache-aware bust on model save via `post_save` signals.
- **Impact:** Medium-High · **Effort:** Low

### C8. Static files aren't manifest-hashed → no long-cache header
- **Category:** Speed
- **Why it matters:** [business/settings.py:294](business/settings.py#L294) uses `whitenoise.storage.CompressedStaticFilesStorage`, not `CompressedManifestStaticFilesStorage`. Without hashed filenames, you can't safely set `Cache-Control: public, max-age=31536000, immutable`. Every return visitor re-validates every static asset.
- **Evidence:** `settings.py:294`.
- **Recommended fix:** Swap to `whitenoise.storage.CompressedManifestStaticFilesStorage`. Run `collectstatic` as part of your Railway build (confirm in `build.sh`). Whitenoise will then serve hashed assets with immutable cache headers automatically.
- **Impact:** Medium-High · **Effort:** Low (1-line change + verify build)

### C9. No BreadcrumbList schema or UI breadcrumbs
- **Category:** SEO
- **Why it matters:** Breadcrumbs generate rich-snippet trails in SERPs and strengthen topical hierarchy. Every service page, blog post, and legal page has an obvious parent path; zero of them emit BreadcrumbList JSON-LD or show a breadcrumb trail.
- **Evidence:** Grep returns no BreadcrumbList schema in the codebase.
- **Recommended fix:** Create a shared `breadcrumbs.html` include + a `{% breadcrumbs %}` tag (or just a `seo_tags.py` helper) that takes a list of `(name, url)` tuples and emits both the UI breadcrumb and the matching JSON-LD. Call it in every service, blog, and legal template.
- **Impact:** Medium · **Effort:** Low

### C10. Booking form friction: car seats buried in free text, gratuity as raw number
- **Category:** UX / Conversion
- **Why it matters:** Free-text fields for car-seat types mean (a) dispatch has to hand-parse orders, (b) customers forget to specify age ranges, and (c) families with kids (the core customer) lose confidence. Raw-number gratuity input skips a well-known conversion pattern (preset % buttons lift tipping by 25–40%).
- **Evidence:** [reservations/forms.py:108-140](reservations/forms.py#L108-L140), [reservations/templates/reservations/book_form.html](reservations/templates/reservations/book_form.html).
- **Recommended fix:**
  - Add three explicit fields with numeric steppers: "Rear-facing (0–2 yrs)", "Forward-facing (2–7 yrs)", "Booster (4–12 yrs)". Show only if "Traveling with children" toggle is on.
  - Replace gratuity number input with radio chips: `15% ($X)`, `18% ($Y)`, `20% ($Z)`, `Custom` — live-calculated via JS.
  - Add a dedicated confirmation page (or replace the form with a server-rendered success block) that echoes confirmation number, expected contact time, and a CTA to text dispatch.
- **Impact:** High · **Effort:** Medium

---

## 3. Quick Wins

Each item below is < 1 hour of work with immediate payoff.

| # | Change | File | Why |
|---|--------|------|-----|
| 1 | Raise `SECURE_HSTS_SECONDS` to `31536000` (1 year) | [business/settings.py:274](business/settings.py#L274) | HSTS preload meaningful only at ≥1 year |
| 2 | Set `SECURE_SSL_REDIRECT = True` | [business/settings.py:273](business/settings.py#L273) | You already trust `X-Forwarded-Proto` from Railway |
| 3 | Set `SESSION_SAVE_EVERY_REQUEST = False` | [business/settings.py:313](business/settings.py#L313) | Kills a DB/Redis write on every request |
| 4 | Replace `CompressedStaticFilesStorage` with `CompressedManifestStaticFilesStorage` | [business/settings.py:294](business/settings.py#L294) | Unlocks `Cache-Control: immutable` |
| 5 | Add `media="print" onload="this.media='all'"` to Bootstrap Icons + Font Awesome | [content/templates/main.html:51, 56](content/templates/main.html#L51) | Non-blocking CSS |
| 6 | Add canonical fallback to `request.build_absolute_uri` | [content/templates/main.html:39](content/templates/main.html#L39) | Every page gets a canonical |
| 7 | Add OG/Twitter meta block with defaults | [content/templates/main.html](content/templates/main.html) `<head>` | Proper social previews |
| 8 | Point homepage service cards to `/services/<slug>/` | [reservations/templates/reservations/index.html:364, 401, 438](reservations/templates/reservations/index.html#L364) | Internal link equity + intent match |
| 9 | Add `fetchpriority="high"` to hero `<img>` + `width`/`height` | [reservations/templates/reservations/index.html:24, hero img](reservations/templates/reservations/index.html#L24) | Faster LCP + no CLS |
| 10 | Point `404.html` "Return home" link to `home` not `rates` | [content/templates/404.html](content/templates/404.html) | It's the expected target |
| 11 | Change contact page canonical from `/users/contact-grayson-towncar/` to match the actual navbar URL | [reservations/templates/reservations/contact.html:11](reservations/templates/reservations/contact.html#L11) | Avoid canonical/route mismatch |
| 12 | Add `aria-label` (not just `title`) to icon-only sticky buttons | [reservations/templates/reservations/index.html:1027-1040](reservations/templates/reservations/index.html#L1027-L1040) | Screen readers |
| 13 | Enable Django cached template loader | [business/settings.py TEMPLATES](business/settings.py#L114) | Free speed on every request |
| 14 | Delete either `rates_page.css` or `rates_test.css` (both currently load) | `content/static/css/` | ~14 KB shaved |
| 15 | Add `loading="lazy"` + `decoding="async"` to below-fold `<img>` tags | service templates, about.html | Mobile LCP |
| 16 | Fix "Our Commitment" typo `" providing"` (lowercase after period) | [reservations/templates/reservations/about.html meta desc](reservations/templates/reservations/about.html#L10) | Looks amateur in SERP |
| 17 | Standardize "Free" vs "Complimentary" everywhere | homepage + FAQ + service pages | Clearer value prop |
| 18 | Disallow `/users/` in `robots.txt` (agent portal) | [content/templates/robots.txt](content/templates/robots.txt) | Prevent accidental indexing |

---

## 4. SEO Findings

### 4.1 On-page SEO
- **Titles:** Generally strong and intent-matched — homepage, services, rates, blog all unique. Exceptions: privacy + TOS likely use defaults; Contact page includes phone in title (real estate waste, phone already in schema).
- **Meta descriptions:** Mostly 130–165 chars and well-written; two issues — capitalization typo on About (`" providing"` after period), and Contact/Rates both embed the phone number needlessly.
- **H1s:** Every page has exactly one H1 and no level skips. (Confirmed on homepage, about, blog post, services sampled.) Good.
- **Image alts:** Uniformly descriptive; dynamic alts on blog via `{{ post.title }}`. Minor nit: the three "photo1/2/3" images on About all share the same alt — vary them.
- **Copy friction:** "Meet & Greet" is ambiguous (inside vs curbside). Change to "Inside-terminal meet & greet — driver waits at baggage claim with your name sign."

### 4.2 Technical SEO
- **Canonicals:** Broken by omission — see §2 C3. Base-template fallback is the fix.
- **Robots.txt:** Correctly disallows admin/dispatching/payment/drivers and advertises sitemap. Add `/users/` to disallow (agent portal isn't for search).
- **Sitemap:** Three-sitemap structure is solid; verify `privacy`, `tos`, and `guest_quote` are either included or explicitly non-indexable. `BlogPostSitemap` uses `changefreq="yearly"` — raise to `"monthly"` if you ever edit posts.
- **URL structure:** Clean and keyword-rich except `/users/contact-grayson-towncar/` which exposes an internal app prefix. Move the route to `/contact/` or add a 301.
- **Indexability:** No stray `noindex` found on public pages. Confirm booking confirmation / thank-you pages ship with `<meta name="robots" content="noindex,nofollow">` once you build them.
- **Contact URL canonical mismatch:** The canonical on `contact.html` ([L11](reservations/templates/reservations/contact.html#L11)) disagrees with the navbar's `{% url 'contact' %}`. Pick one and 301 the other.

### 4.3 Internal linking
- **Service cards → rates** (see §2 C6) is the biggest internal-linking waste on the site.
- **Cross-service linking is thin.** Disney page should link to Universal + Port Canaveral ("combine trips"); Port Canaveral should cross-link to Disney ("add a Magic Kingdom day before your cruise"). Add a small "Popular combos" block per service page.
- **Footer + navbar link coverage is complete.** Good.
- **Fleet cards** link to `/book-orlando-transportation/<pk>?round=2` — opaque numeric URLs. Consider `/book/<vehicle-slug>/` for both SEO and human readability.

### 4.4 Content opportunities
Missing location/service pages with clear search demand:
- **Sanford Airport (SFB) Transportation** — already mentioned in `llms.txt` ([content/templates/llms.txt](content/templates/llms.txt)) but no page.
- **Kissimmee Transportation** (hotel/vacation-rental hub, high intent).
- **International Drive Transportation** (I-Drive shuttle service).
- **Lake Buena Vista Transportation** (Disney-adjacent hotels).
- **SeaWorld / Aquatica Transportation** (separate attraction, currently bundled).
- **Long-distance Florida** (Kennedy Space Center, Daytona, St Augustine day trips).
- **Hourly / Chauffeur Service**.
- **Wedding / Special Event Transportation**.

Blog topic gaps that match high-intent searches:
- "MCO to Disney: Uber vs. Private Car Service (Real Cost Breakdown 2026)" — comparison content ranks.
- "What Disney Resort You're Staying At Changes Your Pickup — Here's the Map."
- "How Early to Leave MCO Before a Port Canaveral Cruise Departure."
- "Do You Tip a Town-Car Driver in Orlando? What's Customary."
- "Flying into MCO Terminal C: What's Different About Pickup."

### 4.5 Local SEO
- **Schema NAP:** Uses `addressLocality: "Orlando"`, `addressRegion: "FL"`, `postalCode: "32827"` in [reservations/templatetags/seo_tags.py](reservations/templatetags/seo_tags.py). Verify 32827 is your actual GBP-registered address; if not, update. Inconsistent NAP hurts local pack rankings.
- **Service area markup:** `areaServed` lists five places — good. Add Kissimmee, Sanford, and International Drive as you build those pages.
- **GBP alignment:** Not verifiable from code, but make sure your GBP profile, schema, and footer show *identical* name/address/phone — character for character.
- **Reviews:** You claim `ratingCount: 250` + `ratingValue: 4.8` in schema. Google requires that number to match aggregated reviews on a publicly-visible page. Either show a review count on-page or link to a reviews source, otherwise Google can strip the rich result.

### 4.6 Structured data
**Present and good:** Organization+LocalBusiness, FAQPage (FAQ page), BlogPosting (post template), Service (each service page), Blog (list).

**Missing (ordered by ROI):**
- **BreadcrumbList** everywhere — see §2 C9.
- **FAQPage schema on the homepage** (homepage already has an FAQ section at lines 699–822; duplicate the schema JSON-LD there to catch the homepage FAQ snippet).
- **AggregateRating on each Service schema** (inherits well from LocalBusiness but search engines treat per-Service ratings separately).
- **Product schema for each vehicle class** in the fleet section (capacity + `priceRange`).
- **Review / Testimonial schema** for the homepage review carousel (lines 150–318).
- **WebSite + SearchAction** on homepage (blog has a search box; declaring `SearchAction` lets Google show a site-search box for brand queries).

**Fix a schema bug:** `BlogPosting.dateModified` is hard-coded to `post.created` in [blog/templates/blog/blog_post.html:38-39](blog/templates/blog/blog_post.html#L38). Add an `updated` field to the Blog model (`auto_now=True`) and use it — otherwise Google can down-weight stale content.

### 4.7 Metadata issues
- Base template has no OG/Twitter block — §2 C4.
- Canonical fallback missing — §2 C3.
- Contact + Rates meta descriptions waste chars on phone numbers.

### 4.8 Indexation / crawl
- Good: admin + staff sections disallowed in `robots.txt`.
- Fix: add `/users/` disallow.
- Fix: confirm booking confirmation pages emit `noindex` once you build them.
- Potential thin/near-duplicate: [services/templates/services/mco-terminal-c-transportation.html](services/templates/services/mco-terminal-c-transportation.html) versus [services/templates/services/orlando-airport-transportation.html](services/templates/services/orlando-airport-transportation.html) — either expand Terminal C with genuinely terminal-specific content (gate map, curbside lane, shortest walk) or fold it into the MCO page and 301.

---

## 5. Performance Findings

### 5.1 Frontend loading issues
- Render-blocking CSS — §2 C2.
- No critical CSS inlined.
- GTM inline script ([main.html:6-21](content/templates/main.html#L6-L21)) runs before `<meta charset>` and before the viewport meta. The injected `gtm.js` is `async` so the real blocking cost is minor, but order-of-head matters for SEO/PSI audits. Move GTM below the primary meta/title block.
- Montserrat is correctly deferred. Bootstrap Icons + Font Awesome are not.

### 5.2 Backend / template / query issues
- **Cached template loader disabled** (§3 quick-win #13).
- **Blog list lacks select_related** — [blog/views.py blog_list](blog/views.py) fetches `Blog.objects.all()` with no `select_related('author')` / `prefetch_related('categories')`; related-posts loop creates a separate query per render. Low traffic today but becomes a problem as post count grows.
- **Signals run heavy work inline via `threading.Thread`** ([reservations/signals.py:29-47](reservations/signals.py#L29-L47), [reservations/utils.py:22-30](reservations/utils.py#L22-L30)). On Gunicorn the thread can be killed mid-flight; there's no retry. You already have Celery installed (`django-celery-beat`) — move these to Celery tasks.
- **Context processor** in [ops/context_processors.py:15-27](ops/context_processors.py#L15-L27) already caches for 60s — good. Audit other processors similarly.
- **Dispatching views** are staff-only; complex but instrumented via `SlowRequestMiddleware` at 500 ms. Keep an eye on logs rather than preemptively rewriting.

### 5.3 Images / media
Verified file sizes on disk (biggest offenders):

| File | Size | Format | Action |
|------|------|--------|--------|
| `disney-grand-floridian.jpg` | 9.5 MB | JPG | Re-encode to AVIF (~100 KB) + WebP (~200 KB) + JPG fallback (~350 KB) |
| `services-bg.webp` | 5.0 MB | WebP | Re-encode to ~400 KB, AVIF variant |
| `contact-us-bg.webp` | 3.3 MB | WebP | Re-encode to ~300 KB, AVIF variant |
| `universal.jpg` | 1.7 MB | JPG | Convert to WebP + AVIF, ~180 KB |
| `universalstudios.webp` | 1.3 MB | WebP | Re-encode to ~250 KB |
| `universal-grand-helios.png` | 1.2 MB | PNG (photo in PNG!) | Convert to WebP/AVIF, ~200 KB |

Also: add explicit `width`/`height` to every `<img>` (esp. hero) and convert heroes to `<picture>` with `type="image/avif"`/`type="image/webp"` sources.

### 5.4 JS / CSS
- `book_form.css` ~35 KB, `services_styles.css` ~32 KB, `premium-landing.css` ~28 KB — each loads as a full file on its respective page. Extract common `.lux-*` utilities into a ~10 KB shared bundle. Inline the ~3 KB above-the-fold critical CSS per page template.
- Two rates stylesheets (`rates_page.css` + `rates_test.css`) both load — drop one.
- JS is reasonably scoped per page. `quote_form.js` (17 KB) is included on homepage and most service pages; ensure it's `defer`'d.
- No webpack / vite build pipeline detected — you're hand-maintaining static files. Acceptable at this size but consider a build step (esbuild is 5 minutes to set up) when you hit ~30 static JS files.

### 5.5 Caching
- Redis is configured (prod) / LocMemCache (dev) — but nothing caches. See §2 C7 and §3 quick-win #13.
- Add HTTP cache headers via a middleware or per-view decorators (`Cache-Control: public, max-age=300, s-maxage=3600` for static pages).
- Immutable static cache requires ManifestStaticFilesStorage — §2 C8.

### 5.6 Third-party scripts
- GTM + GA4 + Facebook Pixel — all deferred or injected async. Good.
- Google Maps — only on staff pages. Good.
- No reCAPTCHA on public forms. Add hCaptcha or reCAPTCHA v3 if spam bookings appear; currently low risk because the form requires contact info and manual dispatch follows up.

### 5.7 Mobile performance
- Viewport tag correct.
- Hero image size is the dominant mobile-perf problem (§2 C1).
- `form-control-sm` on quote-form inputs is under the 44×44 px touch-target minimum — use full-size on mobile via a `@media (max-width: 576px)` rule.
- Sticky icon buttons bottom offset may collide with iOS Safari bottom bar — set `bottom: calc(15px + env(safe-area-inset-bottom))`.

### 5.8 Core Web Vitals risk
- **LCP — FAIL likely** today (~3.5–5s mobile). Fixing §2 C1 + §2 C2 should pull LCP under 2.5s.
- **INP — Marginal.** Main risks are the quote-form validation JS on every keystroke. Debounce at 200–300 ms if not already.
- **CLS — Marginal.** Missing `width`/`height` on large images is the main driver. Enforce explicit dimensions site-wide.

---

## 6. UX / Conversion Findings

### 6.1 Homepage
- Above the fold is well-executed: clear H1, embedded quote widget, trust badges, review count, partner logos. Keep.
- The three hero value-prop chips use "Free" and "Complimentary" inconsistently. Standardize on "Free" — shorter and higher-converting.
- Reviews carousel (lines 150–318) is strong; consider segmenting into a "Repeat customers" mini-carousel to prove loyalty.

### 6.2 Key landing pages (services)
- Add **service-specific pricing callouts** at the top of each page (e.g., "MCO → Disney Resorts, starting $195 round-trip"). Removes a click.
- Add **destination-specific FAQ** (5 Qs) per service page — also feeds FAQPage schema and long-tail rankings.
- Add **cross-service internal links** ("Booking a Disney trip around a cruise? See Port Canaveral transportation"). Lifts AOV and internal PageRank flow.
- Consider templatizing the service page into a `service_base.html` with blocks for hero title, subtitle, pricing, FAQ, and destination-specific copy — reduces the 600–1,000-line duplication across 8 service templates.

### 6.3 Booking flow
- Add a real **post-submit confirmation** (page or inline block) with confirmation number, SLA ("You'll hear from us within 30 minutes"), email/SMS confirmation details, and a "text dispatch" deeplink.
- Add a **sticky trip-summary / live total** that updates as the user changes vehicle, trip type, date — currently static until submit.
- Consider a **one-way upsell banner** ("Booking round-trip saves 15% — view round-trip pricing") on the one-way flow.

### 6.4 Quote flow
- Missing **time picker** for arrival/departure — add `<input type="time">` plus a `flight_number` field. Today the form can capture a date with no time, which forces a support call.
- Vehicle-type select should show **inline images** once an option is picked — resolves the "what does a Minivan look like?" question.

### 6.5 Forms (all)
- Make required vs optional visually explicit (asterisk on required, "(optional)" on optional).
- Car-seat fields: replace free-text special-requests capture with dedicated `rf_carseats` / `ff_carseats` / `booster_seats` steppers (already exist as model fields — wire them to the UI).
- Gratuity: preset % chips + live $ amount (§2 C10).
- Error UX: ensure `.invalid-feedback` always renders with a red icon and red border on the field; today the CSS exists but is weakly styled.

### 6.6 Navigation
- Add **sticky mobile CTA bar** (§2 C5).
- Reorder Services dropdown — put "All Services" at the bottom (below the specific services), not at the top where it can be mis-clicked when users are looking for a specific destination.
- Consider a top-bar strip: `📞 (407) 212-7190  ·  24/7  ·  Free Car Seats` — local-business standard, lifts calls.

### 6.7 Mobile UX
- `form-control-sm` on quote form → mobile touch-target issue (§5.7).
- Sticky buttons need iOS safe-area padding (§5.7).
- Date/time picker feedback: native `<input type="date">` doesn't re-show the chosen date in the placeholder once collapsed — either render a small "Chosen: Apr 22" helper via JS or use a visible label line.

### 6.8 Trust & reassurance
- **Response-time SLA** missing site-wide — add "We reply within X minutes" on contact + quote pages.
- **Licensed / Insured** badge isn't clickable — link to a dedicated "Credentials" page showing USDOT, FL PSC / PUC license, COI (Certificate of Insurance) PDF. This also lifts local SEO E-E-A-T signals.
- **Live chat / GHL widget** — not embedded publicly; consider adding one for instant questions ("Do you have infant car seats?").
- **Photos of drivers + vehicles** — real photos beat stock for trust. You likely have these; audit usage.

---

## 7. Accessibility Findings

**Critical**
- `.input-group input { outline: 0; }` in [content/static/css/login_register.css:39](content/static/css/login_register.css#L39) removes focus outline with no replacement. Add `outline: 2px solid #0d6efd; outline-offset: 2px;` on `:focus-visible`.
- Icon-only sticky buttons on the homepage need `aria-label` ([index.html:1027-1040](reservations/templates/reservations/index.html#L1027-L1040)); `title` alone is not a substitute for assistive tech.

**Major**
- No skip-to-content link at the top of `<body>` — add one (`href="#main"`, visible on focus only).
- Gold accent (#C9A227/#D4AF37) on light backgrounds likely fails 4.5:1 — audit in `navbar.html:249`, `footer.html:152, 239`. Run WebAIM contrast checker and adjust.
- `form-control-sm` on mobile falls under the 44×44 px WCAG touch-target guideline — §5.7.

**Moderate**
- Some images have repeated `alt="Grayson Towncar Experience"` — vary per image so AT users get distinct descriptions.
- Ensure every `<button>` used for nav (hamburger, dropdown toggle) has `aria-expanded` state wired — Bootstrap 5 handles this if you use the right data attributes, but verify on custom dropdowns.

**Form accessibility**
- Quote + booking forms are generally good (`<label for>` present, `aria-describedby` on many inputs). Keep.
- Ensure `.is-invalid` always receives `aria-invalid="true"` when server validation fails.

**Heading hierarchy:** No level skips detected on pages sampled.

---

## 8. File-Level Recommendations

| Path | Problem | Recommended change | Priority |
|------|---------|--------------------|----------|
| [content/templates/main.html](content/templates/main.html) | No canonical fallback; no OG/Twitter defaults; Bootstrap+FA+Bootstrap-Icons render-block; GTM before meta | Add `{% block og %}`, canonical fallback, defer Bootstrap Icons + FA with print-onload pattern, move GTM below core meta | P0 |
| [business/settings.py](business/settings.py) | `SECURE_SSL_REDIRECT=False`, HSTS only 1 h, `SESSION_SAVE_EVERY_REQUEST=True`, non-manifest static storage, no cached template loader | Flip SSL redirect; HSTS → 1 year; session save → False; swap to `CompressedManifestStaticFilesStorage`; add cached template loader | P0 |
| `content/static/images/disney-grand-floridian.jpg` (9.5 MB) | Unoptimized hero | Re-encode AVIF+WebP+JPG, <300 KB | P0 |
| `content/static/images/services-bg.webp` (5 MB), `contact-us-bg.webp` (3.3 MB), `universal.jpg` (1.7 MB), `universal-grand-helios.png` (1.2 MB) | Oversized backgrounds | Re-encode; PNG→WebP for photos | P0 |
| [content/templates/navbar.html](content/templates/navbar.html) | No sticky mobile CTA; massive dropdown duplication (admin/staff/agency/agent) | Add mobile `d-lg-none` sticky bar with Call + Book; DRY the role dropdowns with a `{% for %}` over a list | P0 / P2 |
| [reservations/templates/reservations/index.html](reservations/templates/reservations/index.html) | Service cards all link to `/rates-booking/`; icon-only sticky buttons need aria-labels; hero image lacks width/height/fetchpriority | Fix each linked CTA; add `aria-label`; add dimensions + `fetchpriority="high"` | P0 |
| [reservations/templates/reservations/book_form.html](reservations/templates/reservations/book_form.html) + [reservations/forms.py](reservations/forms.py) | Car-seat types in free text; gratuity raw number; no confirmation page | Dedicated car-seat steppers; preset % gratuity chips; add a confirmation view | P1 |
| [reservations/templates/reservations/quote_form.html](reservations/templates/reservations/quote_form.html) + [reservations/templates/reservations/guest_quote.html](reservations/templates/reservations/guest_quote.html) | No time picker; `form-control-sm` on mobile; phone placeholder missing format hint | Add `<input type="time">` + `flight_number`; use full-size inputs on mobile; add `(407) 555-1234` placeholder | P1 |
| [reservations/templates/reservations/contact.html:11](reservations/templates/reservations/contact.html#L11) | Canonical URL (`/users/contact-grayson-towncar/`) mismatches navbar route | Verify route, canonicalize one URL, 301 the other | P0 |
| [services/templates/services/](services/templates/services/) | No per-service pricing, no per-service FAQ, no cross-service links, ~600-line template duplication | Extract `service_base.html` with blocks; add pricing callout + 5-Q FAQ + cross-links | P1 |
| [blog/views.py](blog/views.py) | N+1 risk on author, related-posts loop queries every render | Add `select_related('author')`; fetch related-posts once with `prefetch_related` | P2 |
| [reservations/signals.py](reservations/signals.py) + [reservations/utils.py](reservations/utils.py) | Background work via raw `threading.Thread` | Move to Celery tasks with retry | P2 |
| [content/templates/robots.txt](content/templates/robots.txt) | Missing `/users/` disallow | Add `Disallow: /users/` | P1 |
| [content/sitemaps.py](content/sitemaps.py) | `BlogPostSitemap` `changefreq="yearly"`; missing `guest_quote`, `privacy`, `tos`; no verification | Bump blog to `"monthly"`; confirm legal pages present; add guest-quote if public | P2 |
| [blog/templates/blog/blog_post.html:38-39](blog/templates/blog/blog_post.html#L38) | `dateModified` hard-wired to `datePublished` | Add `updated` field to Blog model (`auto_now=True`) and reference it | P2 |
| [content/templates/404.html](content/templates/404.html) | "Return" link points to rates page | Point to `home` | P3 |
| [content/static/css/login_register.css:39](content/static/css/login_register.css#L39) | Removes focus outline (a11y) | Provide `:focus-visible` outline replacement | P1 |
| `content/static/css/rates_page.css` + `rates_test.css` | Both load on rates page | Delete whichever isn't active | P2 |

---

## 9. Recommended Roadmap

### Phase 1 — Immediate (this week, target <1 dev-day)
- [ ] Compress hero images (§2 C1) — single highest PSI win.
- [ ] Flip `SECURE_SSL_REDIRECT=True`, raise HSTS to 1 year, `SESSION_SAVE_EVERY_REQUEST=False`, swap to ManifestStaticFilesStorage, enable cached template loader (§3).
- [ ] Add canonical fallback + OG/Twitter block to `main.html` (§2 C3, C4).
- [ ] Defer Bootstrap Icons + Font Awesome (§2 C2).
- [ ] Re-point homepage service card CTAs to `/services/…/` (§2 C6).
- [ ] Add sticky mobile Call + Book bar (§2 C5).
- [ ] Fix contact-page canonical/route mismatch.
- [ ] Fix About page meta-description typo; standardize "Free" vs "Complimentary".

### Phase 2 — This month
- [ ] Rework booking form (car seats, gratuity, confirmation page) (§2 C10).
- [ ] Add time picker + flight-number to quote form (§6.4).
- [ ] Add BreadcrumbList schema + UI breadcrumbs to services and blog (§2 C9).
- [ ] Add per-service pricing callouts + per-service 5-Q FAQ + cross-service links (§6.2).
- [ ] Apply `@cache_page` / `{% cache %}` to rates, services, blog, navbar, footer (§2 C7).
- [ ] Add homepage FAQPage schema duplicate.
- [ ] Swap `.input-group input { outline: 0 }` for a proper `:focus-visible` outline.
- [ ] Add a skip-to-content link.
- [ ] Add response-time SLA and clickable credentials ("Licensed & Insured" → credentials page).
- [ ] Replace free `<img>` dimensions site-wide (prevent CLS).

### Phase 3 — Later improvements
- [ ] Build location pages (Sanford/SFB, Kissimmee, International Drive, SeaWorld, Long-distance, Hourly, Weddings).
- [ ] DRY service templates into `service_base.html`.
- [ ] Move signal-driven work to Celery tasks with retry.
- [ ] Add live chat (GHL widget).
- [ ] Set up an esbuild / vite bundling step for JS + CSS.
- [ ] Consider replacing Font Awesome with a smaller SVG icon sprite (likely saves ~35 KB minified).
- [ ] Add Product schema for fleet vehicles.
- [ ] Add reviews schema + visible review count to satisfy AggregateRating requirements.
- [ ] Audit and, if applicable, rewrite `/book-orlando-transportation/<pk>` to a slug-based URL.

---

## 10. Highest ROI Fixes (the real 80/20)

1. **Compress `disney-grand-floridian.jpg` and the other 3–5 MB images** → single biggest LCP/PSI/rankings win. ~2 hours of work.
2. **Canonical fallback + OG/Twitter defaults in `main.html`** → entire site becomes properly canonicalized and socially-shareable. ~30 minutes.
3. **Sticky mobile Call + Book CTA** → mobile conversions. ~1 hour.
4. **Re-point homepage service CTAs to service pages** → internal linking + conversion specificity. ~15 minutes.
5. **Booking form fix (car seats, gratuity chips, confirmation page)** → upper-funnel drop-off reduction + data quality for dispatch. ~4 hours.
6. **Flip four settings.py values** (SSL redirect / HSTS / session save / manifest static storage) → security + caching + free performance. ~15 minutes.
7. **Per-service pricing callouts + mini-FAQ** → organic traffic for long-tail destination queries + fewer rates-page click-throughs. ~3 hours across 8 templates.
8. **BreadcrumbList schema everywhere** → SERP real estate + crawl hierarchy. ~2 hours with a reusable template tag.
9. **Defer Bootstrap Icons + Font Awesome** → critical-path shortening, easy PSI score lift. ~15 minutes.
10. **Cached template loader + `@cache_page` on rates/services/blog** → TTFB improvement with zero content risk. ~1 hour.

Items 1–10 together are roughly two full working days of focused effort and would realistically lift mobile PSI from ~50–65 to ~80–90, fix the biggest on-site SEO gaps, and materially improve mobile conversions.

---

## 11. Open Questions / Things to Verify

- **Actual business address / postal code** — schema claims `32827`; is that the GBP-listed address, or a placeholder? NAP must match GBP exactly.
- **`/users/contact-grayson-towncar/` vs `/contact/`** — which is the real route? Where does the navbar `{% url 'contact' %}` resolve to? Pick the canonical and 301 the other.
- **USDOT / FL license / COI** — are you comfortable publishing them? Recommended yes for trust + local SEO E-E-A-T.
- **Review count (`250`) in schema** — matches what's publicly visible on the site? Google may strip AggregateRating if they can't verify the number against on-page reviews.
- **`rates_page.css` vs `rates_test.css`** — which is active? Which can be deleted?
- **Booking confirmation page** — does one exist server-rendered, or does the form submit redirect to an arbitrary template? Confirm and ensure it's `noindex`.
- **Brotli / gzip in production** — Whitenoise is configured; verify with `curl -I -H 'Accept-Encoding: br, gzip' https://www.graysontowncar.com/static/css/styles.css` that `Content-Encoding: br` is actually being returned.
- **Celery vs. scheduler thread** — the project ships `django-celery-beat` but also runs a background scheduler thread (referenced in `settings.py:234` comment). Which is canonical? Consolidate to one.
- **Thank-you / booking-confirmation pages** — do they exist and are they correctly `noindex,nofollow`?
- **Facebook Pixel + GA consent** — any cookie-consent banner in place for EU/CA traffic? Not visible in the code; verify legal posture.
- **Testimonials source** — are they Google Reviews? If so, consider linking to the actual Google Business Profile listing for credibility.
- **Sanford airport (SFB) service** — is it actually offered (llms.txt says yes)? If yes, build the page. If no, remove from llms.txt.
