# Full SEO Audit Report — graysontowncar.com

**Audit Date:** 2026-03-04
**Pages Crawled:** 14
**Business Type Detected:** Local Service Business — Luxury Ground Transportation (Orlando, FL)

---

## Executive Summary

### Overall SEO Health Score: 62 / 100

| Category | Score | Weight | Weighted |
|---|---|---|---|
| Technical SEO | 48/100 | 25% | 12.0 |
| Content Quality | 78/100 | 25% | 19.5 |
| On-Page SEO | 65/100 | 20% | 13.0 |
| Schema / Structured Data | 55/100 | 10% | 5.5 |
| Performance (CWV) | 60/100 | 10% | 6.0 |
| Images | 75/100 | 5% | 3.75 |
| AI Search Readiness | 45/100 | 5% | 2.25 |
| **TOTAL** | | | **62.0** |

### Top 5 Critical Issues
1. **No XML sitemap** — sitemap.xml returns 404. Google cannot efficiently discover pages.
2. **No robots.txt** — no clear directives for crawlers; default "allow all" assumed.
3. **Missing canonical tags** on 12 of 14 pages — duplicate content risk from URL parameters (UTM, etc.).
4. **No meta descriptions** on 5+ pages (FAQ, Blog index, About, service pages rely on schema fallback).
5. **Multiple H1 tags** on Orlando Airport and Universal pages — confuses heading hierarchy.

### Top 5 Quick Wins
1. Add `sitemap.xml` with all 25+ discoverable URLs — immediate crawl improvement.
2. Add canonical tags to every page — 15 minutes of template work.
3. Add FAQPage schema to FAQ page — instant rich result eligibility.
4. Add unique meta descriptions to all pages missing them.
5. Add `robots.txt` with sitemap reference.

---

## Pages Crawled

| # | URL | Title |
|---|---|---|
| 1 | `/` | Orlando Airport Transportation to Disney World & Port Canaveral \| Free Car Seats |
| 2 | `/services/` | Private Orlando Transportation \| Disney, Universal & Cruise Port Service |
| 3 | `/rates-booking/` | Orlando Airport Transportation Rates \| MCO Shuttle Pricing \| Grayson Towncar |
| 4 | `/services/orlando-airport-transportation/` | Orlando Airport Transportation \| MCO to Disney, Universal & More \| Free Car Seats |
| 5 | `/services/disney-world-transportation/` | Disney World Car Service \| Free Car Seats - Grayson Town Car |
| 6 | `/services/universal-orlando-transportation/` | Universal Orlando Transportation \| Orlando Private Car Service |
| 7 | `/services/port-canaveral-transportation/` | Port Canaveral Transportation \| Orlando Cruise Transfer Service |
| 8 | `/services/corporate-transportation/` | Corporate Transportation Orlando \| Business Travel & Events \| Grayson Towncar |
| 9 | `/about-grayson-towncar-services/` | About Grayson Towncar \| Family-Owned Orlando Transportation |
| 10 | `/orlando-transportation-faqs/` | Orlando Car Service FAQ \| Transportation Questions Answered |
| 11 | `/blog/` | Orlando Transportation Tips \| Travel Guides & Airport Advice |
| 12 | `/blog/post/the-guide-to-disney-world-airport-transportation/` | The Ultimate Guide to Disney World Airport Transportation... |
| 13 | `/tos` | TERMS OF SERVICE |
| 14 | `/privacy` | PRIVACY POLICY |

---

## 1. Technical SEO (Score: 48/100)

### 1.1 Crawlability

#### Robots.txt — MISSING
- `https://www.graysontowncar.com/robots.txt` does not return a valid robots.txt file.
- **Impact:** Crawlers have no directives. No sitemap reference for discovery.
- **Fix:** Create a proper robots.txt:
```
User-agent: *
Allow: /

Sitemap: https://www.graysontowncar.com/sitemap.xml
```

#### XML Sitemap — MISSING (Critical)
- `https://www.graysontowncar.com/sitemap.xml` returns **404 Not Found**.
- **Impact:** Google relies on link crawling only. New pages (blog posts, service pages) may be discovered slowly or missed.
- **Fix:** Generate sitemap.xml with all public URLs. Django has `django.contrib.sitemaps` built in.

### 1.2 Indexability

#### Canonical Tags — MOSTLY MISSING (Critical)
| Page | Canonical Tag |
|---|---|
| `/` | Missing |
| `/services/` | Missing |
| `/rates-booking/` | Missing |
| `/services/orlando-airport-transportation/` | Missing |
| `/services/disney-world-transportation/` | Missing |
| `/services/universal-orlando-transportation/` | Missing |
| `/services/port-canaveral-transportation/` | **Present** ✓ |
| `/services/corporate-transportation/` | Missing |
| `/about-grayson-towncar-services/` | Missing |
| `/orlando-transportation-faqs/` | Missing |
| `/blog/` | Missing |
| `/blog/post/...` | Missing |
| `/tos` | Missing |
| `/privacy` | Missing |

- Only **1 of 14** pages has a canonical tag.
- **Risk:** UTM parameters, session IDs, or trailing slashes could create duplicate index entries.

#### Meta Robots — ABSENT
- No `<meta name="robots">` tag found on any page.
- Default behavior (index, follow) is fine for public pages, but TOS/Privacy may benefit from `noindex`.

### 1.3 URL Structure
- Clean, descriptive URLs throughout ✓
- Consistent trailing slashes ✓ (except `/tos` and `/privacy` — inconsistent)
- Good hierarchy: `/services/disney-world-transportation/` ✓
- Blog structure: `/blog/post/slug/` ✓

### 1.4 Security & Headers
- HTTPS enforced ✓
- SSL certificate valid ✓
- HSTS not verified (would need header inspection)

### 1.5 Tracking Scripts
- Google Tag Manager (GTM-PQC5M3M3) ✓
- Google Analytics 4 (G-E999VVT9CE) ✓
- Facebook Pixel (1261740178962298) ✓
- UTM parameter cookie tracking ✓

---

## 2. Content Quality (Score: 78/100)

### 2.1 E-E-A-T Assessment

**Experience:** Strong ✓
- Real customer testimonials with specific details
- Photo gallery of actual service
- Google Reviews integration (5.0 rating displayed)

**Expertise:** Strong ✓
- Deep destination knowledge (Disney resort breakdowns, cruise terminal specifics)
- Professional service descriptions with operational details
- Blog content demonstrates transportation expertise

**Authoritativeness:** Moderate
- "Trusted by Leading Organizations" section with partner logos (Carnival, Disney Cruise Line, Universal, etc.)
- Aggregate rating: 4.8/5 (250 reviews)
- Missing: No industry certifications displayed, no press mentions

**Trustworthiness:** Strong ✓
- Transparent pricing displayed publicly
- Clear cancellation policy
- Privacy policy and terms of service present
- Physical address and phone number visible

### 2.2 Content Depth

| Page | Est. Word Count | Assessment |
|---|---|---|
| Homepage | 2,000+ | Excellent — comprehensive landing page |
| Services hub | 1,500+ | Good — covers all service areas |
| Service pages | 1,200-1,500 each | Good — detailed destination info |
| Blog posts | 3,000-3,500 | Excellent — long-form guides |
| FAQ page | 800-1,000 | Adequate — 10 questions |
| About page | 1,200+ | Good — brand story + values |
| Rates page | 1,000+ | Good — transparent pricing |

### 2.3 Thin Content Pages
- **TOS** — legal boilerplate, acceptable
- **Privacy** — legal boilerplate, acceptable
- No thin content issues detected on main pages ✓

### 2.4 Duplicate Content Risks
- Meta descriptions appear to be **shared/duplicated** across service pages (same schema description)
- The Organization schema `description` is identical on every page — not a direct SEO risk but a missed opportunity

### 2.5 Content Freshness
- Blog posts dated 2025 — may need 2026 updates for "2025 Guide" titles
- Service pages have evergreen content ✓

---

## 3. On-Page SEO (Score: 65/100)

### 3.1 Title Tags

| Page | Title | Length | Issues |
|---|---|---|---|
| `/` | Orlando Airport Transportation to Disney World & Port Canaveral \| Free Car Seats | ~78 chars | Slightly long (>60 ideal) |
| `/services/` | Private Orlando Transportation \| Disney, Universal & Cruise Port Service | ~72 chars | Slightly long |
| `/rates-booking/` | Orlando Airport Transportation Rates \| MCO Shuttle Pricing \| Grayson Towncar | ~77 chars | Long — may truncate |
| `/services/orlando-airport-transportation/` | Orlando Airport Transportation \| MCO to Disney, Universal & More \| Free Car Seats | ~82 chars | **Too long** — will truncate |
| `/services/disney-world-transportation/` | Disney World Car Service \| Free Car Seats - Grayson Town Car | ~60 chars | Good ✓ |
| `/services/universal-orlando-transportation/` | Universal Orlando Transportation \| Orlando Private Car Service | ~62 chars | Good ✓ |
| `/services/port-canaveral-transportation/` | Port Canaveral Transportation \| Orlando Cruise Transfer Service | ~63 chars | Good ✓ |
| `/services/corporate-transportation/` | Corporate Transportation Orlando \| Business Travel & Events \| Grayson Towncar | ~77 chars | Long |
| `/about-grayson-towncar-services/` | About Grayson Towncar \| Family-Owned Orlando Transportation | ~59 chars | Good ✓ |
| `/orlando-transportation-faqs/` | Orlando Car Service FAQ \| Transportation Questions Answered | ~58 chars | Good ✓ |
| `/blog/` | Orlando Transportation Tips \| Travel Guides & Airport Advice | ~60 chars | Good ✓ |

**Issues:** 4 of 11 titles exceed 65-character ideal. Google will truncate these in SERPs.

### 3.2 Meta Descriptions

| Page | Has Explicit Meta Description? |
|---|---|
| `/` | Possibly (via schema fallback only) |
| `/services/` | Same schema description as homepage |
| `/rates-booking/` | Same schema description |
| `/services/orlando-airport-transportation/` | Missing |
| `/services/disney-world-transportation/` | Missing |
| `/services/universal-orlando-transportation/` | Missing |
| `/services/port-canaveral-transportation/` | Via schema only |
| `/services/corporate-transportation/` | Same generic schema description |
| `/about-grayson-towncar-services/` | Present ✓ (unique) |
| `/orlando-transportation-faqs/` | **Missing** |
| `/blog/` | **Missing** |

**Issues:** Most pages lack unique, explicit meta descriptions. Google may auto-generate snippets from page content, reducing CTR control.

### 3.3 Heading Structure

| Page | H1 Count | Issue |
|---|---|---|
| `/` | 1 | ✓ |
| `/services/` | 1 | ✓ |
| `/rates-booking/` | 1 | ✓ |
| `/services/orlando-airport-transportation/` | **2** | Two H1 tags — fix to single H1 |
| `/services/disney-world-transportation/` | 1 | ✓ |
| `/services/universal-orlando-transportation/` | **2** | Two H1 tags — fix to single H1 |
| `/services/port-canaveral-transportation/` | 1 | ✓ |
| `/services/corporate-transportation/` | 1 | ✓ |
| `/about-grayson-towncar-services/` | 1 | ✓ |
| `/orlando-transportation-faqs/` | 1 | ✓ |
| `/blog/` | 1 | ✓ |

### 3.4 Internal Linking
- **Navigation consistency:** All pages share the same nav structure ✓
- **Footer links:** Quick Links, Services, Contact present ✓
- **Cross-linking between service pages:** Good — Popular Routes sections link between services ✓
- **Blog-to-service linking:** Present in blog posts ✓
- **Missing:** No breadcrumb navigation on any page
- **Missing:** Blog posts don't link to each other systematically (only "related" links)

---

## 4. Schema / Structured Data (Score: 55/100)

### 4.1 What's Present

| Schema Type | Pages | Status |
|---|---|---|
| Organization / LocalBusiness | Most pages | ✓ Present |
| AggregateRating | Most pages | ✓ 4.8/5 (250 reviews) |
| Service | Universal, Port Canaveral | ✓ Partial |
| BlogPosting | Blog posts | ✓ Present |
| Blog | Blog index | ✓ Present |

### 4.2 What's Missing (Opportunities)

| Schema Type | Page(s) | Impact |
|---|---|---|
| **FAQPage** | `/orlando-transportation-faqs/` | **High** — FAQ rich results in SERPs |
| **BreadcrumbList** | All pages | **High** — breadcrumb rich results |
| **Service** | Homepage, Services hub, Disney, Airport, Corporate | **Medium** — service rich results |
| **WebSite** (with SearchAction) | Homepage | **Medium** — sitelinks search box |
| **LocalBusiness** (enhanced) | Homepage | **Medium** — add `openingHours`, `paymentAccepted`, `priceRange` |
| **Review** (individual) | Service pages | **Low** — individual review snippets |
| **Vehicle** | Rates page | **Low** — vehicle details |

### 4.3 Schema Validation Issues
1. **Blog schema logo URL** has double slash: `https://www.graysontowncar.com/blog//static/images/logo.png` — should be single `/`
2. **Same Organization schema** repeated identically across pages — consider consolidating with `@id` references
3. **AggregateRating** shows 250 reviews — ensure this updates dynamically or is manually kept current
4. Service pages missing `areaServed` specific to each service (they all use the generic Orlando area)

---

## 5. Performance (Score: 60/100)

### 5.1 Resource Loading
- **3 tracking scripts:** GTM, GA4, Facebook Pixel — moderate JS overhead
- **3 custom font families:** Cormorant Garamond, Outfit, Montserrat — potential render-blocking
- **Cookie-based UTM tracking** — additional JS execution

### 5.2 Estimated Impact
- Custom fonts likely cause **FOIT (Flash of Invisible Text)** without `font-display: swap`
- Multiple third-party scripts may delay **Largest Contentful Paint (LCP)**
- Interactive booking forms with JavaScript may affect **Interaction to Next Paint (INP)**

### 5.3 Recommendations
1. Add `font-display: swap` to all @font-face declarations
2. Defer non-critical JS (Facebook Pixel, UTM tracking)
3. Preload critical fonts
4. Consider loading GTM asynchronously (verify it's not render-blocking)

*Note: Actual Core Web Vitals measurements require PageSpeed Insights / Lighthouse testing for precise scores.*

---

## 6. Images (Score: 75/100)

### 6.1 Positive Findings
- Modern image formats used: `.webp`, `.avif` ✓
- Most images have descriptive alt text ✓
- Partner/trust logos have alt text ✓
- Vehicle photos have alt text ✓

### 6.2 Issues
- **Logo alt text** is generic ("Grayson Town Car") across all pages — could include "Orlando luxury transportation" for keyword value
- **Some decorative/social icons** lack alt text
- **Blog post images** may not all have unique alt text
- **No `loading="lazy"`** detected on below-fold images (would need HTML inspection)
- **No explicit width/height** attributes detected — potential CLS contribution

---

## 7. AI Search Readiness (Score: 45/100)

### 7.1 Citability Assessment
- **Structured content:** Good — clear headings, logical sections ✓
- **Factual claims:** Good — specific pricing, specific locations, real ratings ✓
- **Unique data points:** Good — exact routes, pricing, fleet details ✓

### 7.2 Missing for AI Optimization
- **No `llms.txt`** file — AI crawlers have no guidance
- **FAQ page lacks FAQPage schema** — AI assistants can't easily parse Q&A pairs
- **No clear "snippet-worthy" summary paragraphs** at the top of service pages
- **Blog posts** could use more definitive answer-style formatting (bold key answers)
- **No comparison tables** (e.g., "Grayson vs. Uber/Lyft/Mears") — highly citable for AI

### 7.3 AI Crawler Accessibility
- No known blocks against AI crawlers (no robots.txt to block them)
- Content is server-rendered (Django templates) — good for crawlability ✓
- No heavy client-side rendering requirements ✓

---

## Appendix: Tracking & Analytics

| Tool | ID | Status |
|---|---|---|
| Google Tag Manager | GTM-PQC5M3M3 | Active |
| Google Analytics 4 | G-E999VVT9CE | Active |
| Facebook Pixel | 1261740178962298 | Active |
| UTM Cookie Tracking | Custom JS | Active |
