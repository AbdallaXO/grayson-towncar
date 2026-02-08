# CLAUDE.md — Grayson Towncar Frontend Design Standards

## Brand Context
Grayson Towncar is a luxury private transportation company serving the Orlando tourism market — airport transfers (MCO to Disney/Universal resorts), Port Canaveral cruise transportation, and resort-to-resort transfers. Target audience: vacationing families, travel agents, and guests expecting a premium experience. The brand should feel like Blacklane or Ritz-Carlton — not Uber or a budget shuttle.

## Frontend Design Rules

When working on ANY frontend file (HTML, CSS, JS, templates), follow these standards:

### Design Thinking (Do This First)
Before writing any code, pause and consider:
- **Purpose**: What does this page/component need to accomplish? Who's using it?
- **Tone**: Luxury/refined — clean, elegant, confident. Not flashy or cluttered.
- **Differentiation**: This should feel like booking a premium experience, not a commodity ride.

Choose a clear aesthetic direction and execute it with precision. Elegance comes from restraint, not excess.

### Typography
- Choose fonts that feel premium and distinctive — avoid generic fonts like Arial, Inter, Roboto, or system defaults.
- Pair a refined display/heading font with a clean body font (e.g., a serif or elegant sans-serif for headings, a readable sans-serif for body).
- Establish clear hierarchy: headings, subheadings, body text, captions should all feel intentional.
- Never use more than 2-3 font families per page.

### Color & Theme
- Use CSS variables for consistency across the site.
- Commit to a cohesive luxury palette — dark tones with gold/champagne accents, or clean whites with rich accent colors.
- Dominant colors with sharp accents outperform timid, evenly-distributed palettes.
- Avoid cliché AI color schemes (especially purple gradients on white backgrounds).

### Spacing & Layout
- Generous whitespace — let content breathe. Cramped layouts feel budget.
- Clean visual hierarchy — the eye should flow naturally through the page.
- Asymmetry and intentional grid-breaking can add sophistication when done carefully.
- Mobile-first, fully responsive. Every page must look great on phones.

### Motion & Interactions
- Subtle, smooth transitions on hovers, button states, and page elements.
- Staggered fade-in reveals on page load create a feeling of polish.
- CSS-only animations preferred for performance.
- Focus on high-impact moments — one elegant page load animation beats scattered micro-interactions.
- Hover states should feel intentional and satisfying.

### Buttons & CTAs
- Buttons should feel premium — refined shapes, subtle shadows, smooth hover transitions.
- Primary CTAs must stand out clearly without being garish or aggressive.
- Use size, color contrast, and spacing to draw attention naturally.

### Backgrounds & Visual Details
- Create atmosphere and depth — don't default to flat solid color backgrounds.
- Subtle gradients, gentle textures, layered transparencies, or dramatic shadows add richness.
- Decorative details should support the luxury feel, not distract from content.

### Forms & Booking Elements
- Forms should feel spacious and easy to use — generous input padding, clear labels.
- Validation feedback should be smooth and helpful, not jarring.
- Multi-step forms should have clear progress indicators.
- On mobile, form inputs must be properly sized for touch.

## What to NEVER Do
- Never use generic AI-generated aesthetics (overused fonts, purple-on-white gradients, cookie-cutter layouts).
- Never make the page feel cluttered or overwhelming with too much text/info above the fold.
- Never use garish colors, aggressive animations, or cheap-feeling UI patterns.
- Never break existing functionality when redesigning — preserve all backend logic, form submissions, links, and dynamic content.
- Never converge on the same generic design choices across pages — each page should feel cohesive with the brand but contextually appropriate.

## When Redesigning an Existing Page
1. Read and understand all existing functionality before changing anything.
2. Preserve all backend integrations, form actions, links, and dynamic template logic.
3. Focus on visual/UX improvements — layout, spacing, typography, color, interactions.
4. Test responsiveness on mobile after changes.
5. If unsure whether something is functional vs decorative, ask before removing it.

## Reference Aesthetic
Think: Blacklane, Ritz-Carlton, luxury hotel booking sites — clean, confident, premium. The user should feel like they're booking something special, not just getting a ride.