# SEO Action Plan — graysontowncar.com

**Generated:** 2026-03-04
**Current Score:** 62/100
**Target Score:** 85+ /100

---

## Critical Priority (Fix Immediately)

### 1. Create XML Sitemap
**Impact:** Crawlability +15 | Effort: Low
**Current:** `sitemap.xml` returns 404
**Action:**
- Add `django.contrib.sitemaps` to your Django project
- Include all public URLs (~25-30 pages): homepage, services (5), rates, about, FAQ, blog index, blog posts (10), contact
- Set `changefreq` and `priority` appropriately
- Submit to Google Search Console

```python
# Example Django sitemap setup
from django.contrib.sitemaps import Sitemap
from blog.models import BlogPost

class StaticSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.8

    def items(self):
        return [
            '/', '/services/', '/rates-booking/',
            '/services/orlando-airport-transportation/',
            '/services/disney-world-transportation/',
            '/services/universal-orlando-transportation/',
            '/services/port-canaveral-transportation/',
            '/services/corporate-transportation/',
            '/about-grayson-towncar-services/',
            '/orlando-transportation-faqs/',
            '/blog/',
        ]

    def location(self, item):
        return item
```

### 2. Create robots.txt
**Impact:** Crawl guidance | Effort: Very Low
**Action:** Create `robots.txt` at site root:
```
User-agent: *
Allow: /
Disallow: /users/
Disallow: /admin/

Sitemap: https://www.graysontowncar.com/sitemap.xml
```

### 3. Add Canonical Tags to All Pages
**Impact:** Indexability +10 | Effort: Low
**Current:** Only 1 of 14 pages has a canonical tag
**Action:** Add to your base template `<head>`:
```html
<link rel="canonical" href="{{ request.build_absolute_uri }}" />
```
Strip UTM parameters and force trailing slash consistency.

---

## High Priority (Fix Within 1 Week)

### 4. Add Unique Meta Descriptions to All Pages
**Impact:** CTR improvement +5-15% | Effort: Low-Medium
**Missing on:** FAQ, Blog index, Orlando Airport, Disney, Universal, Corporate pages
**Action:** Write unique 150-160 character descriptions for each page:

| Page | Suggested Meta Description |
|---|---|
| `/orlando-transportation-faqs/` | "Get answers about Orlando airport pickup, car seats, cancellation policy, and vehicle options. Everything you need to know about Grayson Towncar services." |
| `/blog/` | "Orlando travel tips, Disney transportation guides, and airport advice from Grayson Towncar. Expert insights for families visiting Central Florida." |
| `/services/orlando-airport-transportation/` | "Private MCO airport transportation with meet & greet, free car seats, and flight tracking. Luxury transfers to Disney, Universal & Orlando hotels from $105." |
| `/services/disney-world-transportation/` | "Premium Disney World transportation from MCO airport. Free car seats, flight tracking, and professional drivers. All Disney resort transfers available." |
| `/services/universal-orlando-transportation/` | "Private Universal Orlando transportation from MCO & SFB airports. Luxury transfers to all Universal resorts with complimentary car seats and meet & greet." |
| `/services/corporate-transportation/` | "Professional corporate transportation in Orlando. Executive sedans, SUVs & vans for business travel, events, and airport transfers. Reliable 24/7 service." |

### 5. Add FAQPage Schema to FAQ Page
**Impact:** Rich results in Google SERPs | Effort: Very Low
**Action:** Add JSON-LD to `/orlando-transportation-faqs/`:
```json
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {
      "@type": "Question",
      "name": "How does the Orlando airport pickup process work?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "Your answer text here..."
      }
    }
  ]
}
```
Include all 10 FAQ questions.

### 6. Fix Multiple H1 Tags
**Impact:** Heading hierarchy clarity | Effort: Very Low
**Pages affected:**
- `/services/orlando-airport-transportation/` — has 2 H1s. Change subtitle to H2.
- `/services/universal-orlando-transportation/` — has 2 H1s. Change subtitle to H2.

### 7. Add BreadcrumbList Schema
**Impact:** Breadcrumb rich results in SERPs | Effort: Low
**Action:** Add to all pages:
```json
{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://www.graysontowncar.com/"},
    {"@type": "ListItem", "position": 2, "name": "Services", "item": "https://www.graysontowncar.com/services/"},
    {"@type": "ListItem", "position": 3, "name": "Disney World Transportation"}
  ]
}
```

---

## Medium Priority (Fix Within 1 Month)

### 8. Shorten Over-Long Title Tags
**Impact:** SERP display | Effort: Very Low
**Pages:** Homepage, Rates, Orlando Airport, Corporate
**Target:** Under 60 characters each. Examples:
- Homepage: `Orlando Airport Transportation | Free Car Seats | Grayson Towncar`
- Rates: `Orlando Transportation Rates & Pricing | Grayson Towncar`
- Airport: `MCO Airport Transportation | Disney & Orlando Transfers`

### 9. Add Service Schema to All Service Pages
**Impact:** Service rich results | Effort: Medium
**Currently missing on:** Homepage, Services hub, Disney, Airport, Corporate
**Action:** Add `Service` schema with `name`, `description`, `provider`, `areaServed`, `offers`

### 10. Add Visible Breadcrumb Navigation
**Impact:** UX + internal linking + schema eligibility | Effort: Medium
**Action:** Add breadcrumb trail to all interior pages:
```
Home > Services > Disney World Transportation
```

### 11. Fix Blog Schema Logo URL
**Impact:** Schema validation | Effort: Very Low
**Current:** `https://www.graysontowncar.com/blog//static/images/logo.png` (double slash)
**Fix:** `https://www.graysontowncar.com/static/images/logo.png`

### 12. Standardize URL Trailing Slashes
**Impact:** Consistency | Effort: Low
**Issue:** `/tos` and `/privacy` lack trailing slashes while all other pages have them
**Fix:** Add trailing slashes or redirect to consistent format

### 13. Optimize Font Loading
**Impact:** LCP/CLS improvement | Effort: Low
**Action:**
- Add `font-display: swap` to all `@font-face` declarations
- Preload critical font files with `<link rel="preload">`
- Consider reducing to 2 font families instead of 3

### 14. Update Blog Post Dates
**Impact:** Content freshness signals | Effort: Medium
**Issue:** Multiple posts titled "2025 Guide" — update to 2026 where applicable
**Action:** Review and update content, change titles/dates, update BlogPosting `dateModified`

### 15. Defer Non-Critical JavaScript
**Impact:** Performance / LCP | Effort: Low
**Action:**
- Add `defer` to Facebook Pixel script
- Load UTM tracking script after page load
- Verify GTM loads asynchronously

---

## Low Priority (Backlog)

### 16. Add `llms.txt` File
**Impact:** AI search visibility | Effort: Very Low
**Action:** Create `/llms.txt` describing your business for AI crawlers.

### 17. Add WebSite Schema with SearchAction
**Impact:** Sitelinks search box potential | Effort: Very Low

### 18. Add `noindex` to TOS and Privacy Pages
**Impact:** Crawl budget optimization (minor) | Effort: Very Low

### 19. Create Comparison Content
**Impact:** AI citability + long-tail keywords | Effort: High
**Action:** Create pages like "Grayson Towncar vs Uber/Lyft to Disney World" or "Best MCO Airport Transportation Options Compared"

### 20. Enhance LocalBusiness Schema
**Impact:** Local SEO | Effort: Low
**Add:** `openingHours`, `paymentAccepted`, `priceRange`, `currenciesAccepted`

### 21. Add Individual Review Schema
**Impact:** Review rich results | Effort: Medium
**Action:** Mark up individual customer testimonials with `Review` schema

### 22. Systematic Blog Cross-Linking
**Impact:** Internal link equity | Effort: Medium
**Action:** Add "Related Articles" section to each blog post linking to 2-3 related posts

---

## Implementation Roadmap

| Week | Tasks | Expected Score Impact |
|---|---|---|
| Week 1 | Items 1-3 (sitemap, robots.txt, canonical tags) | 62 → 72 |
| Week 2 | Items 4-7 (meta descriptions, FAQ schema, H1 fix, breadcrumbs) | 72 → 80 |
| Week 3 | Items 8-12 (titles, service schema, breadcrumb nav, blog fix, URLs) | 80 → 85 |
| Week 4 | Items 13-15 (fonts, blog dates, JS defer) | 85 → 88 |
| Backlog | Items 16-22 | 88 → 92+ |

---

## Summary

The site has **strong content quality and E-E-A-T signals** — the transportation expertise, transparent pricing, customer reviews, and destination knowledge are excellent. The main gaps are **technical SEO fundamentals** (sitemap, robots.txt, canonicals, meta descriptions) that are straightforward to fix in a Django project. Implementing weeks 1-2 of this plan should push the score from 62 to ~80, which represents a significant improvement in search visibility.
