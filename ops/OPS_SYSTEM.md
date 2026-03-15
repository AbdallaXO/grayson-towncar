# Ops Task System — Summary

## What It Does

Automated operational task queue that scans every 30 minutes for issues that need staff attention. Creates, prioritizes, escalates, and auto-resolves tasks so nothing falls through the cracks.

---

## Scanners (run every 30 min)

| Scanner | What it catches | Window | Priority |
|---------|----------------|--------|----------|
| **Flight mismatch** | Arrival flight times shifted from booked pickup | 7 days | Tiered: severity × days out |
| **Driver conflict** (flight-triggered) | Same-day flight shift causes driver scheduling conflict | Today | CRITICAL |
| **Driver conflict** (overlap) | Two legs assigned to same in-house driver overlap | Today | CRITICAL |
| **Unassigned legs** | Today's legs with no driver | Today | CRITICAL |
| **Unpaid reservations** | Confirmed reservations with balance owed | 7 days | Tiered by days out |
| **Uncontacted forms** | Pending contact form submissions | All | HIGH |
| **Flight auto-refresh** | Pulls latest flight data from AeroAPI | Tiered: today/30min, 2d/4hr, 7d/daily | — |

### Priority Matrix (flight mismatches)

| Mismatch \ Days out | 0 (same-day) | 1–2 days | 3–5 days | 6–7 days |
|---------------------|-------------|----------|----------|----------|
| Minor (30–60 min) | MEDIUM* | MEDIUM | LOW | LOW |
| Moderate (1–2 hr) | HIGH* | HIGH | MEDIUM | LOW |
| Major (2+ hr) | HIGH* | HIGH | MEDIUM | LOW |

*Same-day without driver conflict → flight_verify task at these priorities.
Same-day WITH driver conflict → always CRITICAL driver_conflict task.

---

## Auto-Close Conditions

Tasks automatically close when:
- **Flight verify**: mismatch drops below 30 min
- **Driver conflict (flight)**: flight mismatch resolves OR conflict no longer exists
- **Driver conflict (overlap)**: scheduling conflict no longer exists
- **Driver assignment**: driver gets assigned
- **Contact form**: form marked contacted/closed
- **Payment chase**: reservation cancelled
- **All types**: reservation cancelled → task cancelled

---

## Driver Conflict Detail View

The task detail page for driver conflicts shows:

1. **Conflict Summary** — driver name, minutes late, flight info (if applicable)
2. **Flight Shift Detail** — original vs current arrival time, post-match analysis (if arrival leg)
3. **Driver's Day Schedule** — timeline of all legs with conflict highlighting
4. **"Why It Conflicts" Breakdown**:
   - Driver clears at X (location)
   - Travel/reposition time to next pickup (~Y min)
   - Driver at pickup: Z
   - For arrivals: guest at baggage claim ~W (gate + 15 min deplane)
   - How many minutes late
5. **Quick Actions** — Match Flight Time (arrival legs only), Open Dispatch Board, Call Driver

### Flight-Aware Logic

- **Arrival legs**: shows flight gate arrival, 15-min deplane/baggage grace, guest-ready time
- **Return legs**: no "Match Flight Time" (meaningless for departures), shows as "THIS LEG" not "DELAYED FLIGHT"
- **Same-airport reposition**: MCO→MCO shows "Reposition to pickup area" (~5 min) instead of "Travel"
- **Post-match analysis**: simulates "if we match flight time, does schedule still work?"

---

## Escalation

- Each priority has an escalation delay: CRITICAL=immediate, HIGH=4hr, MEDIUM=8hr, LOW=24hr
- Escalated tasks bump to CRITICAL priority + ntfy push notification
- Snoozed tasks reopen after snooze expires (1h, 4h, or next day 9 AM Eastern)

---

## Task Types

| Type | Icon | Created By |
|------|------|-----------|
| `payment_chase` | Credit card | Scanner |
| `flight_verify` | Airplane | Scanner |
| `driver_conflict` | Warning triangle | Scanner |
| `driver_assignment` | Person | Scanner |
| `contact_form` | Envelope | Scanner |
| `manual` | Pencil | Staff (UI) |

---

## Files

| File | Purpose |
|------|---------|
| `ops/models.py` | OperationalTask, CommunicationAttempt, StaffActivity models |
| `ops/services.py` | create_task (with dedup), close_task, cancel_task, log_communication |
| `ops/tasks.py` | All scanners, auto-close, flight refresh, conflict detection |
| `ops/signals.py` | Auto-close on reservation cancel, driver assign, payment, contact form |
| `ops/escalation.py` | Escalation engine + ntfy notifications |
| `ops/views.py` | Queue view, detail view, task actions (claim/complete/snooze/cancel), staff metrics |
| `dispatching/templates/dispatching/task_queue.html` | Queue UI |
| `dispatching/templates/dispatching/task_detail.html` | Detail UI with driver conflict visualization |
| `ops/management/commands/seed_ops_tasks.py` | Test data seeder |

---

## Access Control

All ops views (task queue, detail, actions, metrics) are **superuser-only** during testing phase. When ready to roll out to staff, remove `@user_passes_test(_is_superuser)` from views in `ops/views.py`.

---

## What's Next

### Should do before production

- [ ] **Test with real flight data** — verify AeroAPI refresh + mismatch detection + conflict creation end-to-end with actual flights
- [ ] **Test conflict auto-close** — reassign a driver on a conflict task, verify it auto-closes on next scan
- [ ] **Test "Match Flight Time" button** — click it on a flight-triggered conflict, verify pickup updates and task resolves
- [ ] **Verify escalation ntfy notifications** — confirm push alerts arrive for CRITICAL tasks
- [ ] **Add `driver_conflict` to escalation tags_map** — currently missing, defaults to generic tag
- [ ] **Remove superuser gate** — when testing complete, open task queue to all staff

### Staff Metrics Enhancements

Current metrics dashboard tracks task completions, communication volume, and response times. Next phase should add:

- [ ] **Reservations created per staff** — count of reservations created by each user, daily/weekly/monthly
- [ ] **Revenue per staff per day** — total `total_price` of reservations created by each user, grouped by day
- [ ] **Revenue trends** — daily revenue chart (total + per-staff breakdown)
- [ ] **Booking conversion rate** — leads converted to reservations per staff member
- [ ] **Average reservation value** — per staff member
- [ ] **Staff leaderboard** — ranked by reservations created, revenue generated, tasks completed
- [ ] **Daily/weekly summary emails** — automated performance digest to management

### Nice to have

- [ ] **Pagination on task queue** — not needed until 100+ open tasks, but good to add
- [ ] **Task assignment notifications** — notify staff when a task is assigned to them
- [ ] **Bulk actions** — select multiple tasks to snooze/close/reassign
- [ ] **SLA tracking** — track time-to-first-response per task type
- [ ] **Affiliate driver conflict detection** — currently only checks in-house drivers
- [ ] **Last-refreshed timestamp in legs dashboard** — show when flight data was last pulled (per the original plan)
