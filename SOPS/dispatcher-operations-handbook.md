# Dispatcher Operations Handbook

**SOP:** SOP-002

**Audience:** Ordinary staff dispatchers (not founders or superusers)

**Last updated:** July 29, 2026

This handbook covers the tools a dispatcher uses or must respond to during a normal shift. It intentionally excludes landing-page quotes, public booking and marketing pages, SEO, sales-funnel internals, payroll, commissions, executive reporting, and guest-only features that do not create dispatcher work.

In this handbook, **live** means the feature is enabled and reachable in the audited repository. It does not prove that production credentials, email delivery, Twilio, Stripe, AeroAPI, Google Maps, or Samsara are healthy. **Partial** means the dispatcher-facing workflow exists but depends on configuration, a special permission, a founder handoff, or an incomplete subsystem. **Built but switched off** means code exists but its controlling flag is off. **Gap** means there is no supported dispatcher workflow.

## Dispatcher-facing inventory

| System/Feature | What it does (plain language, non-technical) | Who interacts with it (dispatcher / founder-only / driver / guest / fully automated, no human) | Current status (live / built but switched off / partial / gap) | Needs a dispatcher SOP? (yes/no) | Key edge cases or failure states worth documenting |
|---|---|---|---|---|---|
| D01. Dispatcher navigation and daily dashboard | Gives dispatchers the main work area for trips, legs, confirmations, tasks, drivers, schedules, planning, and the dispatch board. | dispatcher | live | yes | A dispatcher can change the displayed date or filters and accidentally review the wrong service day; warning badges are signals, not proof that a problem has been resolved. |
| D02. New reservation wizard | Creates one-way, round-trip, or multi-leg reservations and collects the customer, trip, vehicle, flight, and price information. | dispatcher | live | yes | The session can expire; missing required leg data stops creation; late-night Publix stops are blocked; date, AM/PM, and flight warnings require acknowledgement; a created reservation starts as confirmed. |
| D03. Reservation search, detail, edit, cancellation, deletion, and history | Finds reservations and lets a dispatcher review or change customer details, prices, statuses, notes, legs, assignments, payments, and change history, or remove an invalid unpaid record. | dispatcher | live | yes | The default list window is limited; reservation/customer changes can save even if a later leg edit fails; changing a driver can return a nonterminal leg to in-progress; deletion is irreversible and blocked by payment records; pending refunds are warnings and must be checked before assignment. |
| D04. Legs, stops, and multiple flights | Adds or removes legs and stops, records stop fees, links multiple flights, and chooses the flight that controls the leg. | dispatcher | live | yes | The final leg cannot be deleted; a stop fee changes the reservation total; deleting the controlling flight promotes another flight or leaves none; flight refresh reviews all linked flights but dispatcher decisions normally follow the controlling flight. |
| D05. Reservation duplication or copy | Covers repeat-service recreation and accidental duplicate handling. There is no dispatcher copy/clone function. | dispatcher, founder-only | gap | yes | A repeat trip must be re-entered and re-priced through the booking wizard; accidental duplicates need manual review, while the duplicate-cleanup tool is founder-only. |
| D06. Manual and suggested pricing | Shows a configured rate suggestion while allowing the dispatcher to enter the actual base price, extras, gratuity, and total. | dispatcher, founder-only | partial | yes | Suggestions use the first leg and may fall back to a less-specific vehicle rate or show no match; they are not a final quote; stop and after-hours fees can change totals later. |
| D06a. Quote Calculator | Prices a trip that is not on the published rate card. Published card price wins automatically when the route matches; otherwise it prices a local custom or out-of-town fare. | dispatcher | partial | yes | **Marked "Demo — still in progress"; rates are still being calibrated.** Gratuity is suggested on top locally but billed on out-of-town work — read the hint under the price. Airport pickups carry a built-in lane/tunnel fee. If a zone shows "no match" the price is a custom estimate, so check the address spelling. Double-check anything unusual with Ab & Ray before quoting a guest. |
| D07. Daily dispatch and legs views | Shows the service day, assignments, trip statuses, payments, flights, conflicts, timeline warnings, and KEOI/VIP indicators. | dispatcher | live | yes | Stale filters or the wrong date can hide work; a badge can remain until a refresh or acknowledgement; late, unpaid, conflict, and refund indicators do not resolve themselves. |
| D08. Schedule board and direct assignment | Lets dispatchers drag trips between unassigned, in-house driver, and affiliate rows and update live assignments. | dispatcher | live | yes | Cancelled work cannot be assigned; assigning a new driver resets a nonterminal leg to in-progress; the board can show a held draft to some users while others without sandbox permission still change the live schedule. |
| D09. Affiliate board and farmouts | Places trips with active affiliate drivers and displays affiliate-specific board rows. | dispatcher, founder-only | partial | yes | The board does not supply fleet vehicles; an affiliate without a pay-rate card is flagged; contact, acceptance, and rate confirmation are not a complete automated workflow; affiliate setup, optimization, and payment management are founder handoffs. |
| D10. Capacity planner, suggestions, auto-assignment, and swaps | Previews driver capacity, conflicts, suggested assignments, and schedule changes before applying them. | dispatcher | partial | yes | Auto-assignment only considers eligible active in-house drivers and can skip unpaid work; driver windows still use a stub-backed guard and live distance is off by default; external configuration and the entered hours affect results; preview is not an applied schedule. |
| D11. Daily fleet vehicle assignments | Assigns a fleet vehicle to an in-house driver for a service date and can copy or prepare a day's vehicle setup. | dispatcher | live | yes | Restricted vehicle certifications can hard-block an assignment; duplicate vehicle use is rejected; vehicle changes are live even while trip assignments are being staged in a schedule draft. |
| D12. Schedule draft, review, and publish | Lets a permitted dispatcher privately stage driver assignments for a held day and submit the draft for review. | dispatcher, founder-only | partial | yes | Ordinary staff need the schedule-sandbox permission; without it they edit live even on a held day; live changes can create draft conflicts; vehicle assignments are never staged; only a founder can reject, force-publish, publish, or send the post-publish driver release. |
| D13. Schedule snapshots and reset | Saves, restores, or deletes schedule snapshots and can reset a day's driver assignments after making an automatic safety snapshot. | dispatcher | live | yes | Reset unassigns the date's legs and returns noncompleted work to in-progress; restore can replace newer work; on a permitted held day the action may affect the draft instead of live assignments; always verify the target date and result. |
| D14. Driver roster, weekly availability, and date overrides | Shows active drivers and lets dispatchers maintain normal work windows, preferences, maximum hours, and one-day or ranged exceptions. | dispatcher, driver | live | yes | Dispatcher-created overrides are approved immediately; an off or shortened window does not automatically remove trips already assigned; incorrect start/end windows distort planner and conflict results. |
| D15. Driver time-off requests | Receives driver requests and lets dispatchers approve or deny them after reviewing coverage and assigned work. | dispatcher, driver | live | yes | Pending requests do not affect availability; duplicate request rows can be grouped and decided together; approving time off does not automatically reassign existing legs; SMS failure does not roll back the saved decision. |
| D16. Driver portal and schedule-change visibility | Lets drivers see assigned work, accept it, update trip status, add notes, and submit time off; dispatchers troubleshoot what drivers can see. | dispatcher, driver | partial | yes | The portal polls for changes instead of relying on automatic push; a driver may need to refocus or reload; trip visibility depends on date, assignment, and status; completing every active leg can complete the reservation; phone-based GPS tracking has been removed. |
| D17. Flight refresh, matching, and review | Refreshes flight data, highlights missing or mismatched flights, and can move pickup time to the best known arrival time. | dispatcher, fully automated, no human | partial | yes | AeroAPI credentials, limits, and availability are external; cancelled, diverted, wrong-airport, and wrong-day results need review; matching changes pickup time but not pickup date; bulk refresh finishes in the background and can create tasks after the page first reports progress. |
| D18. Guest flight and overnight-date verification | Sends a signed correction link when a flight cannot be verified and records whether an early-morning arrival belongs to the previous or same calendar day. | dispatcher, guest | partial | yes | Do not send guest verification for a temporary provider error; links can expire; choosing same-day for an overnight arrival may move pickup forward one day; a corrected flight can change pickup time and create new tasks or conflicts. |
| D19. Confirmation SMS workspace | Builds, customizes, exports, and sends next-day confirmation texts, singly or in bulk. | dispatcher | partial | yes | Twilio must be configured; bulk sends run in the background and the page must be refreshed; already-sent rows are normally skipped; unpaid and unverified-flight badges warn but do not block sending; SWBF/VIP communication may require a separate RingCentral group. |
| D20. Confirmation and payment-reminder email | Sends reservation confirmations and checkout reminders from the reservation workspace. | dispatcher | partial | yes | A confirmation request reports success when its background retry thread is queued, before delivery finishes; only an actual successful send creates the email log; payment reminders send synchronously and report failure; a communication failure does not undo the reservation action. |
| D21. Dispatcher payment portal and saved cards | Opens customer checkout, saves a card, or charges a selected saved card for an amount tied to the reservation. | dispatcher, guest | partial | yes | Stripe and the customer's payment profile must be healthy; no card, a declined card, a stale payment method, or a failed customer lookup stops the charge; verify the amount, description, customer, and successful payment record before treating the trip as paid. |
| D22. Unpaid-trip reminders and payment chase | Sends staged reminder emails and creates dispatcher work when an upcoming trip remains unpaid. | dispatcher, fully automated, no human | partial | yes | The system does not automatically cancel unpaid trips; travel-agent reservations are excluded; recent staff contact suppresses some reminders; suspected duplicate reservations stop reminders and require founder cleanup; the T-2-hour stage escalates the task but still requires a human decision. |
| D23. Refund request handoff | Lets a dispatcher request a price adjustment, partial cancellation, or full cancellation for founder review. | dispatcher, founder-only | partial | yes | A reason and valid amount are required; ordinary staff cannot open another active request; selected or full-cancellation legs are unassigned immediately even before approval; only a founder can approve, reject, correct, or process the Stripe refund. |
| D24. Dispatcher task queue and communication log | Organizes operational work into lanes and lets dispatchers claim, assign, snooze, complete, dismiss, or document contact attempts. | dispatcher, fully automated, no human | live | yes | A blocked-by link is advisory and does not prevent action; snoozed work moves to Waiting; Future Blockers may not be due yet; claiming a task does not complete it; calls, texts, emails, outcomes, and useful notes must be logged manually. |
| D25. Automatic task escalation and NTFY alerts | Contains an escalation path intended to raise overdue work and notify an owner outside the task queue. | dispatcher, fully automated, no human | built but switched off | yes | The active task generator returns no automatic escalations, and NTFY is disabled; critical or overdue tasks can still exist, but dispatchers must monitor the queue and use the real on-call handoff instead of expecting an alert. |
| D26. KEOI and VIP alerts | Marks trips needing special operational attention and makes those alerts visible across dispatcher views. | dispatcher, founder-only | partial | yes | KEOI details must be specific and current; completed or cancelled legs auto-close KEOI; reopening can reactivate it; removing KEOI requires a separate permission; SWBF can create VIP treatment automatically while dispatchers can also toggle VIP manually. |
| D27. After-hours fee review and charge | Flags a trip that moves into the 10 PM–6 AM window and lets a dispatcher charge the saved card and send a notice. | dispatcher, fully automated, no human | partial | yes | Automatic charging is off; the dispatcher action rechecks that the fee is still owed; no saved card, a decline, or Stripe failure leaves work unresolved; the notice email is background work and can fail after the charge succeeds; batches are capped and may need another run. |
| D28. Samsara GPS, ETA, and risk signals | Shows dispatcher-facing vehicle location, freshness, ETA, and late-risk information for mapped fleet vehicles. | dispatcher, fully automated, no human | partial | yes | No token or vehicle mapping means no signal; data older than the freshness threshold becomes unknown; a parked vehicle can retain older data; the integration does not contact drivers, reassign trips, or change trip status; use it as a clue, not an automatic decision. |
| D29. Driver browser push and wake-up escalation | Contains automatic driver browser notices and an early-morning SMS/call/owner-alert ladder. | driver, fully automated, no human | built but switched off | yes | Automatic browser notices default off and require VAPID keys; the wake-up ladder defaults off and also needs Twilio and notification phones; NTFY calls are no-ops; dispatchers must not assume any of these channels acknowledged a schedule change or woke a driver. |
| D30. Dispatcher time clock, personal coverage, and on-call view | Lets a dispatcher clock in/out, start/end breaks, and see their own schedule, teammates on duty, and tonight's on-call coverage. | dispatcher, founder-only | live | yes | Double-clicks and invalid state changes return a soft error and the page resynchronizes; ordinary dispatchers cannot edit staffing, on-call assignments, or time records; an incorrect or open shift requires a founder correction. |

### Inventory source notes

1. **D01:** `dispatching/templates/dispatching/dispatcher_navbar.html`, `dispatching/urls.py`, and the `index` view in `dispatching/views.py`.
2. **D02:** Dispatcher booking views in `dispatching/views.py`, forms in `dispatching/forms.py`, guards in `dispatching/booking_guards.py`, and `dispatching/templates/dispatching/booking/`.
3. **D03:** `ReservationListView`, `reservation_details`, `modify_reservation`, `delete_reservation`, and `reservation_history` in `dispatching/views.py`, plus `reservations/models.py`.
4. **D04:** Inline stop and flight routes in `dispatching/urls.py`, their handlers in `dispatching/views.py`, and leg/flight models in `reservations/models.py`.
5. **D05:** The superuser-only duplicate views in `dispatching/views.py`, with menu permissions in `dispatching/templates/dispatching/dispatcher_navbar.html`; no dispatcher clone route exists in `dispatching/urls.py`.
6. **D06:** Booking pricing and rate matching in `dispatching/views.py` and `dispatching/forms.py`, stop-fee adjustments in `reservations/utils.py`.
7. **D06a:** `dispatching/quote_engine.py` (all pricing rules), the `quote_calculator` views in `dispatching/views.py`, and `dispatching/templates/dispatching/quote_calculator.html`. Rationale and calibration history: `docs/quote-calculator-audit.md`.
7. **D07:** `index` and leg filtering in `dispatching/views.py`, with `dispatching/templates/dispatching/legs_filter.html`.
8. **D08:** `schedule_board` and assignment endpoints in `dispatching/views.py`, the write rules in `dispatching/assignment.py`, and `dispatching/templates/dispatching/schedule_board.html`.
9. **D09:** Affiliate rows in `schedule_board` in `dispatching/views.py`, affiliate models in `drivers/models.py`, and founder tools in `dispatching/farmout_actions.py` and `dispatching/farmout_optimizer.py`.
10. **D10:** Planner and auto-assignment views in `dispatching/views.py`, scheduling logic in `dispatching/scheduler.py`, and window guards in `dispatching/feasibility_guards.py`.
11. **D11:** Vehicle assignment and day-setup endpoints in `dispatching/views.py`, with `DriverVehicleAssignment` and vehicle restrictions in `drivers/models.py`.
12. **D12:** Draft endpoints in `dispatching/views.py`, sandbox authorization in `dispatching/assignment.py`, draft models and permission in `reservations/models.py`, and release SMS in `dispatching/confirmation_sms.py`.
13. **D13:** Snapshot and reset routes in `dispatching/urls.py` and implementations in `dispatching/views.py`.
14. **D14:** Driver schedule views in `dispatching/views.py`, roster/profile views in `drivers/views.py`, and availability resolution in `drivers/models.py` and `drivers/availability.py`.
15. **D15:** Driver request handlers in `drivers/views.py`, dispatcher decision handlers in `dispatching/views.py`, models in `drivers/models.py`, and SMS handling in `drivers/timeoff_notifications.py`.
16. **D16:** Portal views in `drivers/views.py`, portal templates under `drivers/templates/drivers/`, status behavior in `reservations/models.py`, and push gating in `drivers/push.py`.
17. **D17:** Flight refresh and time matching in `dispatching/views.py`, classifications in `dispatching/flight_refresh_review.py`, and flight tasks in `ops/tasks.py`.
18. **D18:** `dispatching/flight_verify_views.py`, `dispatching/overnight_views.py`, and `dispatching/overnight_arrival.py`.
19. **D19:** Confirmation views in `dispatching/views.py`, send logic in `dispatching/confirmation_sms.py`, and `dispatching/templates/dispatching/confirmations.html`.
20. **D20:** `send_reservation_confirmation_ajax`, `send_reservation_confirmation`, and `send_payment_reminder_ajax` in `users/emails.py`.
21. **D21:** `dispatcher_payment_portal`, `process_payment`, and `save_card` in `dispatching/views.py`, with Stripe payment models in `payment/models.py`.
22. **D22:** `ops/unpaid_reminders.py`, task creation/scans in `ops/tasks.py`, and unpaid task models in `ops/models.py`.
23. **D23:** `request_refund`, `refund_management`, and `process_refund` in `dispatching/views.py`, with `RefundRequest` in `reservations/models.py`.
24. **D24:** Task queue routes in `dispatching/urls.py`, actions in `ops/views.py`, and task/communication models in `ops/models.py`.
25. **D25:** Disabled escalation behavior in `ops/tasks.py` and `ops/escalation.py`, plus `NTFY_ENABLED` in `business/settings.py`.
26. **D26:** `dispatching/keoi_views.py`, KEOI rules in `reservations/keoi.py`, VIP toggling in `dispatching/views.py`, and flags in `reservations/models.py`.
27. **D27:** After-hours charge handlers in `dispatching/views.py`, fee flags in `reservations/utils.py`, and notice email in `users/emails.py`.
28. **D28:** `dispatching/samsara_service.py`, `dispatching/samsara_scheduler.py`, `dispatching/samsara_risk.py`, GPS fields in `drivers/models.py`, and `SAMSARA_API_TOKEN` in `business/settings.py`.
29. **D29:** Push gating in `drivers/push.py` and `business/settings.py`, the wake-up ladder in `drivers/wakeup.py` and `drivers/wakeup_scheduler.py`, and disabled NTFY behavior in `reservations/utils.py`.
30. **D30:** Dispatcher clock and coverage views in `ops/views.py`, time-clock services/models in `ops/services.py` and `ops/models.py`, and routes in `dispatching/urls.py`.

## Shift-start checklist

- [ ] Open **Clock** and clock in. Confirm the page shows the correct state.
- [ ] Open **My Schedule**. Confirm today's actual hours, teammates on duty, handoffs, and tonight's on-call coverage.
- [ ] Open **Tasks**. Review **Mine**, **Unclaimed**, overdue or critical work, **Waiting**, and **Future Blockers**.
- [ ] Open the daily dashboard for **today** and then **tomorrow**. Confirm the displayed dates before acting.
- [ ] Scan for unassigned legs, driver conflicts, unpaid trips, pending refunds, flight warnings, confirmation work, KEOI/VIP trips, and late-risk signals.
- [ ] Review new driver time-off requests and date overrides. Check already-assigned work before approving time off.
- [ ] Confirm today's in-house drivers have the intended fleet vehicles.
- [ ] Refresh flights due for review before matching times or sending next-day confirmations.
- [ ] If a schedule draft or held day is shown, confirm whether your actions are **Draft** or **Live** before moving any trip.

## During-shift checklist

- [ ] Work claimed tasks from highest urgency and nearest service time first.
- [ ] Keep each active task assigned, correctly snoozed, or completed; do not leave work only in personal notes.
- [ ] Log customer, driver, affiliate, and founder contact attempts in the task communication log.
- [ ] Watch trip statuses, driver conflicts, flight changes, unpaid trips, KEOI/VIP flags, and Samsara freshness/risk throughout the shift.
- [ ] After every material reservation, payment, refund-request, assignment, or flight change, reopen or refresh the record and verify the saved result.
- [ ] Treat “queued,” “sending,” or an immediate email success message as a request to send—not proof of delivery.
- [ ] Manually contact the responsible person when a disabled or missing notification channel would otherwise be the only alert.

## Shift-handoff checklist

- [ ] Assign unresolved tasks to the next responsible dispatcher or leave a clear owner and due time.
- [ ] Add a short note stating what happened, what was tried, the last contact result, and the next action.
- [ ] Identify any background bulk confirmation or flight refresh still running and tell the next dispatcher to refresh for the final result.
- [ ] Identify held days and schedule drafts, including whether the draft is open, submitted, conflicted, or waiting for founder publication.
- [ ] List unassigned work, unresolved conflicts, unpaid trips near service, after-hours charges not completed, flight/date mismatches, KEOI/VIP risks, and Samsara signals that need follow-up.
- [ ] Identify founder handoffs: refund approvals, duplicate cleanup, staffing/time-clock corrections, affiliate setup/rates, KEOI removal permission, or protected schedule actions.
- [ ] Confirm tonight's on-call person and hand off urgent late/overnight issues through the real agreed contact channel.
- [ ] End any open break and clock out. If the time record is wrong, record the correct times and send them to a founder for correction.

## Reservations

### Create a reservation

1. From **Reservations**, start **New Reservation**.
2. Choose one-way, round trip, or multi-leg. Confirm the number and order of legs before continuing.
3. Search for and select the correct existing customer when possible. Otherwise enter the customer details carefully, especially mobile number and email.
4. Enter the reservation-level vehicle, passenger/luggage counts, car seats, store stop, and special requests.
5. Complete every leg's date, time, pickup, drop-off, and any leg-specific overrides.
6. Add flight information where applicable. Check that the airport direction, flight date, and pickup date make sense together.
7. Enter the final manual price. Treat the suggested price as a reference only.
8. Review every leg and the total. Resolve any hard block and read each warning before acknowledging it.
9. Create the reservation, open its detail page, and verify customer, dates, times, locations, price, and confirmed status.
10. Send confirmation only after the saved record is correct.

Do not work around the late-night Publix block. For date, AM/PM, or flight warnings, correct the trip if the warning is valid; acknowledge only after checking the source information.

### Locate a reservation

1. Open **Reservations**.
2. Search by the information available, such as customer or reservation details.
3. Adjust the date/status filters if the trip is outside the default list window.
4. Open the reservation and verify the customer, service date, and legs before changing anything.
5. Use **History** when you need to understand who changed a reservation or leg and when.

### Modify a reservation

1. Open the correct reservation and choose the edit action.
2. Change reservation/customer information and the required leg fields.
3. Save once, then read every success or error message.
4. Reopen the reservation and verify the exact fields changed.
5. If the page says reservation/customer changes saved but leg changes did not, treat that as a partial save. Correct the leg errors and save again; do not assume the first values were rolled back.
6. If changing a driver, verify the leg status afterward because a nonterminal leg can return to **in-progress**.

### Change statuses and notes

1. Use reservation status for the overall booking and leg status for each movement.
2. Use private notes for internal operational facts, not as a substitute for a task when follow-up is still required.
3. Verify driver assignment before changing a live trip status.
4. Remember that driver portal status changes create status history, and completing every active leg can complete the reservation.

### Cancel or delete

1. To stop service but preserve the operational record, change the appropriate leg/reservation status to **cancelled** and follow the refund process if money is involved.
2. Use permanent **Delete** only for a reservation that truly should not exist.
3. Confirm there are no payment records; the system blocks deletion if payments exist.
4. Read the confirmation carefully. Deletion is irreversible and removes related legs.
5. Recheck the schedule board and task queue after cancellation or deletion.

### Duplicate a reservation

There is no supported dispatcher copy/clone action.

1. Open the original reservation in one tab.
2. Create a new reservation through the normal wizard.
3. Re-enter and re-verify the customer, every leg, flights, vehicle, notes, and current price. Do not blindly carry an old price or date.
4. If the issue is an accidental duplicate rather than a repeat booking, do not use the founder duplicate-cleanup page. Add a clear task/note and hand it to a founder.

### Add or remove legs

1. From the reservation, choose **Add Leg**.
2. Enter the required date, time, pickup, and drop-off, then save.
3. Verify pricing, status, and driver assignment after the leg is added.
4. To remove a leg, confirm it is the intended leg and that cancellation/refund requirements have been handled.
5. The system will not delete the reservation's last remaining leg.

### Add and manage stops

1. Add the stop on the correct leg.
2. Enter a location unless the stop type permits otherwise.
3. Check duration, optional start time, notes, and any extra fee.
4. Save and verify both the stop order and reservation total.
5. When editing or removing a paid stop, verify that the total changed by the expected difference.

### Add and control flights

1. Add each flight to the correct leg.
2. Mark the flight that should drive arrival review and pickup decisions as **controlling**.
3. Refresh flight data and confirm airline/number, airports, date, and result.
4. Before deleting a controlling flight, decide which remaining flight should control the leg.
5. After deletion, verify the promoted controlling flight—or confirm that the leg intentionally has none.

## Pricing, payments, and refunds

### Use manual and suggested pricing

1. Read the suggested price, route, and vehicle context.
2. Confirm it matches the actual trip. The matcher begins with the first leg and can fall back to a less-specific vehicle rate.
3. Enter the approved base price, extras, gratuity, and total manually.
4. Verify the total after adding or changing stop fees.
5. Record the reason for an unusual manual price in the appropriate internal note.
6. If no suggestion appears or it appears wrong, calculate/confirm the rate using the approved business process and escalate uncertainty to a founder.

Do not use public landing-page quotes as the dispatcher source of truth.

### Use the Quote Calculator (custom, non-standard trips)

This tool is for trips that are **not** standard transfers: residential pickups, out-of-town runs, cruise ports, and other one-offs. For a normal airport or park transfer, keep using the published rate card as you do today — you do not need this page for those.

1. Open **Quote** in the navbar. Enter the pickup and drop-off, pick a vehicle and trip type, and calculate.
2. Read the badge above the price. **Local custom** or **Custom estimate** means the tool worked the price out — that is the case this page exists for. A green **Published rate card** badge means the trip turned out to be a standard route after all: quote that published price as-is and do not add to it.
3. Read the line under the price. It tells you whether gratuity is **suggested on top** (local trips) or **billed automatically** (out-of-town trips). Say the fare and the gratuity as two numbers on out-of-town work.
4. Click any vehicle row to see that vehicle's price and its own breakdown.
5. Open **Internal breakdown** if a guest questions the price. It is for your understanding only — never read it to a guest, and never discuss how a gratuity is split.
6. Check the zone line at the bottom. If a zone says **no match**, the price is a custom estimate; confirm the address spelling before quoting in case it should have matched a published route.
7. The quote is an estimate, not a booking. Enter the agreed price manually in the booking wizard.

**The page is marked "Demo — still in progress" and the rates are still being calibrated. If a number looks off, or the trip is unusual, double-check with Ab & Ray before quoting a guest.**

### Collect payment or use a saved card

1. Open the reservation's dispatcher payment actions.
2. Confirm the customer and current amount due.
3. Choose the intended action: give the customer checkout, save a card, or charge a saved card.
4. For a saved-card charge, verify the selected payment method, amount, and description before submitting.
5. Wait for the charge result. A decline or provider error is not payment.
6. Reopen the reservation and confirm a successful payment record and updated balance before marking the payment task complete.
7. If the payment profile belongs to the wrong customer, no card exists, or Stripe returns an error, stop and resolve the customer/payment record; do not repeatedly charge.

### Work an unpaid trip

1. Open the unpaid task and reservation.
2. Check service time, amount due, prior automated reminders, recent staff contact, travel-agent involvement, and possible duplicates.
3. Contact the customer or responsible agent using the approved channel.
4. Send a manual payment reminder when appropriate. This send reports an immediate error if it fails.
5. Log the communication attempt and outcome.
6. Keep the task open, assign it, or snooze it to a deliberate follow-up time.
7. Near service, make the manual operate/cancel/escalate decision with the responsible person. The system does not automatically cancel unpaid trips.
8. If the reservation is flagged as a duplicate, stop reminder work and hand duplicate cleanup to a founder.

### Request a refund

1. Open the reservation and verify payment history, service status, and the affected legs.
2. Choose the right request:
   - **Price adjustment:** money changes but no legs are cancelled.
   - **Partial cancellation:** selected legs are affected.
   - **Full cancellation:** all legs and the reservation are intended to be cancelled after approval.
3. Select the exact legs for a partial cancellation.
4. Enter a clear reason and review the suggested and requested amounts. The amount cannot exceed what the system allows.
5. Submit once and verify that the request shows **requested**.
6. Immediately inspect the board: selected legs—or every leg for a full-cancellation request—are unassigned at request time, before founder approval.
7. Add any urgent coverage or customer follow-up as a task.
8. Hand the request to a founder for approval, rejection, correction, and Stripe processing.

Do not submit a full cancellation when the intent is only a price adjustment or selected-leg cancellation. Ordinary dispatchers cannot finish, correct, or approve the refund.

### Review and charge an after-hours fee

1. Open the flagged leg and confirm its current pickup is in the 10 PM–6 AM fee window.
2. Check whether the fee has already been charged and whether the time recently changed.
3. Use the dispatcher charge action only after review.
4. Confirm the saved-card charge succeeded and the fee appears in reservation totals/notes.
5. Remember that the customer email is queued after the charge. A successful charge does not prove that email arrived.
6. If there is no card, a decline, or a Stripe failure, keep or create the task, contact the customer as required, and hand off clearly.
7. For a batch, read the per-leg failures and rerun only if the page says more eligible legs remain.

Automatic after-hours charging is off. The dispatcher must review and trigger the charge.

## Daily dispatch, planning, and affiliates

### Work the daily dispatch board

1. Select the service date and confirm it before making changes.
2. Review the unassigned lane first.
3. Scan each in-house and affiliate row for overlaps, late starts, overruns, unpaid or refund warnings, flight changes, KEOI/VIP, and missing vehicle information.
4. Drag a trip only after checking availability, trip geography, preceding/following work, vehicle, and special requirements.
5. After dropping, verify the driver and status on the trip card and reservation.
6. If the new driver is correct but the status returned to **in-progress**, set the intended operational status only after confirming the current trip state.
7. Contact the driver or affiliate and log the communication when acceptance or awareness matters.

### Assign an affiliate

1. Use the affiliate side of the board and choose an active affiliate with the needed capability.
2. Check for a missing pay-rate warning.
3. Confirm availability, price/rate, contact, vehicle/capacity, and acceptance outside the board as required.
4. Assign the trip and verify the card moved to the correct affiliate.
5. Log the acceptance or failed contact in the task/notes.
6. Hand affiliate creation, rate-card setup, optimization, and payment changes to a founder.

The board assignment is not proof of affiliate acceptance.

### Review planner conflicts and suggestions

1. Set the correct service date.
2. Confirm driver availability, date overrides, hours, and fleet assignments are current.
3. Review unassigned work, conflicts, maximum-hours warnings, exclusions, and unpaid-trip settings.
4. Run **Preview** first.
5. Read every skipped trip and warning. Treat suggestions as assistance, not approval.
6. Remember that driver-window safeguards are stub-backed and live distance is off by default. Manually check tight or distant movements.
7. Apply only after the preview matches the intended plan.
8. Reopen the board and verify live or draft results.

### Assign fleet vehicles

1. Select the service date.
2. Assign one appropriate fleet vehicle to each working in-house driver.
3. Resolve certification/restriction errors; do not bypass them.
4. Resolve any “same vehicle assigned twice” error.
5. Verify the final driver-to-vehicle list.
6. If a schedule draft is open, remember that vehicle changes are still live.

### Use a schedule draft

1. Before editing, read the page state: **Live**, **Held**, **Draft**, or **Submitted**.
2. If you do not have the `use_schedule_sandbox` permission, assume assignment changes are live. Ask a founder before testing changes.
3. If permitted, open a draft for the service date and verify your changes are labeled as staged.
4. Build assignments and review draft conflicts and live-change warnings.
5. Do not assume vehicle assignments are staged; they write live.
6. Submit the completed draft for founder review.
7. After submission, do not continue changing it unless it is returned through the supported workflow.
8. A founder must publish or force-publish and may then trigger the separate driver-release text.

### Save, restore, or reset a schedule

1. Confirm the exact service date.
2. Save a named snapshot before a large live change.
3. To restore, review the snapshot date/time and understand that newer assignments may be replaced.
4. Refresh the board and verify every restored assignment.
5. Use **Reset Schedule** only when the entire day's assignment plan should be cleared.
6. Confirm whether the action will affect a permitted draft or the live schedule.
7. After reset, verify all unassigned trips and statuses; completed work stays completed, while other affected work returns to in-progress.

## Drivers and availability

### Maintain weekly availability or a date override

1. Open the driver roster/schedule and select the correct active in-house driver.
2. For a recurring schedule, enter working days, start/end hours, flexibility, maximum hours, preferences, and useful scheduling notes.
3. For one date or range, choose the right override: off, available until/after, available window, unavailable window, flexible, or note-only.
4. Save and verify the effective date and window.
5. Review that driver's existing assignments for the affected dates.
6. Reassign or create tasks for work now outside availability; saving the override does not move trips.

### Decide a time-off request

1. Open **Time-Off Requests** and review pending requests.
2. Check full-day/range or partial-window details, duplicate grouping, affected coverage, and already-assigned trips.
3. Approve or deny with a clear operational basis.
4. Verify the decision saved and inspect the schedule/board.
5. Reassign affected work manually.
6. If the notification text fails, the decision is still saved. Contact the driver manually and document it.

Pending time off does not remove the driver from availability until approved.

### Troubleshoot the driver portal

1. Verify the driver is active and the trip is assigned to that exact driver.
2. Verify the service date and that the trip/reservation is not cancelled.
3. Check the current leg status and notes.
4. Ask the driver to refocus the portal tab or reload; the portal polls for assignment changes.
5. Do not promise a browser notification. Automatic schedule-change push is off by default.
6. If the driver changed a status unexpectedly, check leg status history before correcting it.
7. If the issue is location, use Samsara only for a mapped fleet vehicle; phone GPS no longer supplies a dispatcher location.

## Flights and confirmations

### Refresh and review a flight

1. Confirm the flight belongs to the correct leg and identify the controlling flight.
2. Refresh flight data.
3. Compare airline/flight number, origin, destination, pickup airport, flight date, and pickup date.
4. If the provider is temporarily unavailable or rate-limited, wait and retry. Do not ask the guest to correct a provider outage.
5. If the flight is genuinely missing, wrong-airport, or unverifiable, use the flight-verification task/workflow.
6. For a bulk refresh, wait for completion and refresh the page to review each classified result.
7. Mark reviewed/dismiss only after the underlying mismatch has been resolved or deliberately accepted.

### Match pickup time to flight

1. Review the best available actual, estimated, or scheduled arrival time.
2. Confirm the result is not cancelled, diverted, wrong-day, or wrong-airport.
3. Use the single or bulk match action.
4. Remember that matching changes pickup **time**, not pickup **date**.
5. Recheck driver conflicts, after-hours exposure, confirmation text, and downstream tasks.
6. Correct the date separately when necessary.

### Resolve an overnight date question

1. For a 12 AM–6 AM arrival, determine whether the entered flight date means takeoff day or arrival day.
2. Contact the guest or send the signed verification link when the issue is genuinely verifiable.
3. Record **previous day** or **same day** using the staff action.
4. Before choosing same day, confirm that moving pickup forward one calendar day is intended.
5. Refresh the reservation, flight, board, and conflicts after the decision.
6. If the link is expired or invalid, contact the guest manually and record the answer.

### Send confirmation SMS

1. Open **Confirmations** and confirm the service date.
2. Refresh/review flights first.
3. Review each phone number, generated message, unpaid warning, flight warning, VIP/SWBF marker, and prior-send timestamp.
4. Edit and save a custom message only when needed; reset it to return to the generated version.
5. For a single trip, send and confirm a success result.
6. For bulk sending, exclude rows that should not be sent and start the batch once.
7. Refresh until the batch finishes, then review failures and timestamps.
8. Handle SWBF/VIP group communication through the required RingCentral workflow rather than assuming the app bulk text covers it.

### Send confirmation or payment email

1. Verify the customer email address and saved reservation details first.
2. Send the confirmation or payment reminder once.
3. For a confirmation email, understand that immediate “success” means the background retry was queued.
4. Verify later through the email log or the operational response expected by the business; do not treat the first response as delivery proof.
5. For a payment reminder, act on an immediate error and retry only after correcting the cause.
6. If email/SMS fails after a reservation, refund request, after-hours charge, or time-off decision, the underlying action may still be saved. Verify the record first, then contact manually and document the communication failure.

## Tasks, alerts, GPS, and late operations

### Work the task queue

1. Start with **Mine**, overdue/critical tasks, and work nearest to service time.
2. Review **Unclaimed**, **Waiting**, and **Future Blockers**.
3. Open the reservation/leg/customer context before acting.
4. **Claim** work you are taking. Reassign or release it when ownership changes.
5. Use **Snooze** only with a meaningful next time: one hour, four hours, tomorrow at 9 AM, or the supported chosen option.
6. Treat a **blocked by** relationship as information; it does not enforce the dependency.
7. Log calls, texts, emails, outcome, contact, duration when useful, and concise notes.
8. **Complete** only when the operational outcome is actually resolved. Use cancel/dismiss only for invalid or no-longer-needed work and explain why.
9. Create a manual task when a real follow-up has no automatic task.

Common task types include payment chase, flight verification, driver conflict, driver assignment, confirmation texts, contact requests, after-hours fee, tight turn, and manual work.

### Handle KEOI and VIP

1. When a trip needs special attention, add KEOI with a specific category, status, and description of what dispatch must watch or do.
2. Confirm the indicator appears on the reservation and daily board.
3. Update KEOI as facts change; do not leave stale instructions.
4. Completed/cancelled legs close KEOI automatically, and reopening can reactivate it.
5. If KEOI should be removed and you lack the removal permission, hand it to a founder with the reason.
6. Confirm VIP status for manual VIPs and agency-driven SWBF trips.
7. Carry VIP/KEOI requirements into confirmation, affiliate, driver, and handoff communication.

### Use Samsara information

1. Look for the mapped fleet vehicle, last-seen freshness, movement, ETA, and risk signal.
2. Compare it with driver status, scheduled times, and the next pickup.
3. If the signal is fresh and indicates risk, contact the driver and adjust the live operation as needed.
4. If the signal is gray, stale, missing, or attached to no mapped vehicle, use manual contact and normal dispatch checks.
5. Do not assume Samsara changed a trip status, contacted a driver, or reassigned work; it does none of those things.

### Handle late, after-hours, or no-response issues

1. Check the trip, task queue, driver status/history, flight, contact logs, and fresh Samsara signal if available.
2. Call/text the responsible driver, affiliate, customer, or on-call dispatcher through the approved channel.
3. Record each contact attempt and the operational decision.
4. Reassign or escalate manually when needed.
5. Do not wait for wake-up escalation, NTFY, or browser push. Those automatic safety nets are switched off by default.

## Time clock, staffing, and founder handoffs

### Use the dispatcher time clock

1. Clock in at shift start.
2. Start and end breaks using the current state shown on the page.
3. Clock out at shift end.
4. If a double-click or invalid sequence produces an error, let the page resynchronize and confirm the displayed state before clicking again.
5. If a punch or break time is wrong, write down the correct time and ask a founder to edit it.

### View coverage and on-call information

1. Open **My Schedule**.
2. Review your recurring week and the actual selected day's overrides.
3. Confirm who is on duty with you and the handoff windows.
4. Confirm tonight's on-call name and hours.
5. Report a missing/incorrect schedule or coverage gap to a founder. Ordinary dispatchers cannot edit office staffing or on-call assignments.

### Founder handoff table

| Dispatcher encounters or starts | Dispatcher does | Founder finishes |
|---|---|---|
| Refund request | Choose the correct type/legs, enter reason and amount, submit, and protect newly unassigned work. | Approve/reject, correct, and process the Stripe refund. |
| Accidental duplicate | Verify both records, stop inappropriate reminders, add a clear task/note, and avoid permanent deletion when payment/history is involved. | Use duplicate cleanup and resolve protected records. |
| Office schedule, coverage, on-call, or time-clock error | Record the correct facts and maintain a clear handoff. | Edit staffing schedules, on-call assignments, shifts, or breaks. |
| Submitted/conflicted schedule draft | Stop editing, document conflicts and intended result. | Reject, publish/force-publish, and optionally send the driver release. |
| Affiliate missing setup/rate or optimizer decision | Confirm the operational need, contact status, and proposed assignment. | Create/update affiliate data, rates, optimizer actions, or payments. |
| KEOI requiring protected removal | Keep the KEOI current and provide the removal reason. | Remove it if the dispatcher lacks permission. |

## Systems dispatchers must not rely on

- **Wake-up escalation:** built, but `WAKEUP_CHECKS_ENABLED` defaults to false. Do not assume an early-morning driver received a wake-up text/call or that owners received the final alert.
- **NTFY escalation:** disabled in settings, and some notification hooks are no-ops. The task queue and real on-call contact are the operational source of truth.
- **Automatic browser push:** automatic notices default off and also require VAPID keys. Driver portal polling/reload and direct contact remain necessary.
- **Automatic task escalation:** the active generator does not escalate overdue tasks. Dispatchers must watch overdue and critical work.
- **Automatic after-hours charging:** off. A dispatcher must review and trigger a saved-card charge.
- **Samsara automation:** GPS/ETA/risk is read-only dispatcher information and requires a token plus vehicle mapping. It does not change statuses, contact drivers, or reassign work.
- **Auto-assignment certainty:** suggestions are environment- and data-dependent; stub-backed driver windows and default-off live distance limit confidence. Preview and manually verify before applying.
- **Schedule sandbox protection:** it applies only to users with the specific permission and only stages trip driver assignments. Vehicle assignments remain live, and users without permission can still change live work on a held day.
- **Email/SMS success banners:** confirmation email and some batch or notification actions queue background work. A successful request does not always mean delivery completed.
- **Reservation duplication:** no dispatcher clone tool exists. Recreate repeat service through the booking wizard and hand accidental duplicate cleanup to a founder.
