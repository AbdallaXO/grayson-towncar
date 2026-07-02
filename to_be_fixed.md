Remaining to be fixed (~34 open)
Payments, refunds & money integrity

# Finding I/E Pri Why it costs

47 Partially-paid reads "Paid" + excluded from reminder engine quick High A deposit shows "Paid" everywhere and the balance is never chased (~$13.6K leaked in the mirror).
27 No way to record an off-platform (cash/Zelle) payment ~ med Med Cash-settled trips read "unpaid" forever; receivables hide in a $421K haystack.
Flight tracking & day-of ops

# Finding I/E Pri Why it costs

51 Flight-number capture one-shot + staff-gated med Med Customer who books before buying flights (or whose flight changes) has no self-serve fix path.
53 Flight intake unvalidated free text; dead airline autocomplete ~ med Med A mistyped flight disables the AeroAPI/Samsara safety net for that trip.
Website conversion

# Finding I/E Pri Why it costs

52 Tipping is cash-or-call — no self-serve gratuity med Med ~$3K/mo driver income uncaptured per 100 untipped rides; most have a card on file.
33 5.2 MB sitewide og:image + multi-MB money-page imagery quick Med Broken SMS/social previews on every confirmation; slow mobile LCP on ad pages.
Customer self-service

# Finding I/E Pri Why it costs

21 No customer view/change/cancel page med High 11,489 non-cancelled reservations/yr; even 3-5% needing changes = 1-2 office interruptions/day.
22 Driver en-route/on-location notifies office, never the customer med High "Where's my driver?" calls cluster at peak dispatch; the SMS helper already exists.
36 Payment-chase ladder email-only; T-2h texts nothing quick Med Near-pickup unpaid customers (in transit) miss email; each is a dispatcher fire drill.
Dispatcher intake wizard

# Finding I/E Pri Why it costs

18 Customer dedup exact email+phone, no normalization, mutates step 2 (3 auditors) med High Dupes split saved cards/VIP/agent-attach; step-2 create leaves orphan customers.
23 Pricing step disconnected from rate engine; wrong fallback rates med High ~17 bookings/day priced from memory; 81 polluted rate rows since April.
39 Changing trip type mid-wizard wipes all entered data quick Med "Add the return trip" = full 6-step re-entry with the customer on the line.
37 Add/Remove-leg buttons dead ~ quick Med Wrong leg count at step 1 can't be fixed at step 4.
38 Ghost extra leg card on every booking (extra=1) quick Med A one-way shows an empty "Leg 2"; if filled, dispatches a phantom trip.
41 Round-trip return leg requires full re-typing quick Med 30-60s redundant typing per round trip, each a fresh fat-finger chance.
40 No cross-leg chronology check — return-before-outbound accepted quick Med Month/day transposition dispatches a driver on the wrong day.
30 Paid 3 AM booking for 4 AM pickup passes with no notice check/alert quick Med Overnight short-notice bookings land while nobody watches → missed pickup.
Dispatcher board & management UI

# Finding I/E Pri Why it costs

7 No leg search; reservation search can't match "50" #/locations/dates quick High "Smith pickup at the Ritz tomorrow" is unanswerable; the confirmation # returns nothing.
9 Driver-conflict check on only 1 of 3 assignment surfaces ~ quick High All Legs + reservation detail assign with zero checks → silent double-book.
24 Auto-assign preview runs the full engine in-request (~3s), re-run ~9× med High A 10-interaction tuning session = ~30s of site-wide freeze in the morning window.
25 Farming a residual is a dead end in the planner med Med The $70-230 decisions force a page switch + manual re-lookup (~20-30 min/busy day).
26 Every micro-action full-page-reloads a 1.0-1.7 MB planner med Med Each single assignment = ~1-2s round trip that also occupies the one worker.
44 Reservation-side edits leave planner cache stale (8+ endpoints) quick Med "I changed it — why does it still show 10:42?"; a stale suggestion gets one-click assigned.
48 Concurrent edits clobber silently; modify binds legs by index (2 auditors) med Med Two simultaneous calls on one round trip overwrite each other.
29 Cancelling a reservation just flips a status field quick Med Driver believes he still has the job; planner serves a stale board.
42 Trip-type filter applied after pagination quick Med "Tomorrow's arrivals" shows 3-10 rows/page with misleading counts.
43 All-Legs pagination drops driver + single-date filters quick Med Filter to one driver, click page 2, silently see everyone.
46 Week dashboard shows demand but not build state quick Med To know if tomorrow is built you load each 1 MB+ planner.
54 No bulk operations on legs ~ med Med A sick driver's day = touching every leg individually.
55 Schedule board read-only on phones ~ med Med Evening on-call dispatch from a phone can't reassign from the board.
56 Live same-day driver changes don't reach drivers (Phase-2 gap) ~ med Med A 4 PM reassignment the driver never sees = missed pickup.
45 DnD undo toast promises 8s but reload kills it at 1.2s quick Low Mis-drops become hunt-and-fix.
Data model & platform

# Finding I/E Pri Why it costs

17 Confirmation-email failures invisible (UI reports success before send) quick High A silently-failed confirmation = customer at MCO with no itinerary, dispatcher saw a green toast.
28 30% of legs have no Route FK (free-text substring match) ~ med Med 6,091 route-NULL legs → hand-typed driver pay + missing from metrics/farm-matching.
57 Driver payroll silently drops stale + $0-pay legs ~ med Med 401 past legs stuck in non-completed statuses fall out of the pay filter → drivers shorted.
— 5 legacy "canceled" (one-L) reservations vs "cancelled" queries quick Low Harmless today but a re-arming trap; 5-min migration + CheckConstraint.
The highest-value next batch would be the High-priority quick wins: #47 (partial-paid chasing), #7 (leg search), #9 (feasibility on all assign surfaces), and #17 (invisible email failures). Want me to take those next?
