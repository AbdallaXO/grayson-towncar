---
date: 2026-10-04
audience: Dispatchers
title: Linking agents to agencies works again
---

# Linking agents to agencies works again

## Send this to the team

> Hey team — the Agency links screen was showing an error page when you ticked agents and clicked to link them. That's fixed.
>
> Same steps as before: tick the agents, click the link button, done.
>
> Nobody was linked by the failed clicks, so just do them again.
>
> Nothing else about the screen or how agents get paid changed.

---

## Behind the scenes

**Where it lives:** Affiliate Payments → Agency links

**Why:** the live database refused to lock an agent's row while also looking up their (often empty) agency, so every link failed.

**Expect to be asked:**
- "Did my earlier clicks half-work?" No. Each failed click changed nothing.
