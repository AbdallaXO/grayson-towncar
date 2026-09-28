---
date: 2026-09-28
audience: Dispatchers
title: The schedule board is tighter, keeps the hours in view, and shows where a job can drop
---

# The schedule board is tighter, keeps the hours in view, and shows where a job can drop

## Send this to the team

> Hey team — the schedule board got a clean-up. It's about a quarter shorter, so more drivers fit on screen.
>
> 1. The hours stay pinned at the top while you scroll, with the current time in a red tag above the "now" line.
> 2. Pickup times now show on most jobs, even the short ones.
> 3. Under each name: car, job count, and last night's finish with a moon. The moon turns amber when that finish was after 9 PM. Unavailable hours show once, in the red badge, not twice.
> 4. While you drag a job, the row you're over says "Fits", "Will ask first", or "Conflict".
>
> Drivers with no car yet take one line at the bottom. Nothing else changed: same colours, same flags, same drag and drop.

---

## Behind the scenes

**Where it lives:** the schedule board, both the In-House and Affiliate views (the name-column changes are In-House only).

**Why:** with 20+ drivers the board ran to about 2,500px. The hour labels were only at the very top, and most short jobs showed "1…" or nothing where the pickup time should be. Every row showed the same green "Flexible" and orange "Cleared" line, so the ones that mattered didn't stand out.

**Expect to be asked:**
- "Where did 'Cleared 5:36 PM · #001 Suv' go?" It's the moon and time on the second line now. Point at it for the full wording. The car number only shows when it's different from today's.
- "Why is the unit number grey now?" So the names line up. Only a shared car's number is filled (navy), so those pairs stand out.
- "Where did '(14h max)' go?" It already shows in the hours label, e.g. "Flexible (14h)". The (i) popup still has everything.
- "The In-House button looks different." It's the gold 'you are here' fill it was always meant to have.
