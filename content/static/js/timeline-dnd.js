/**
 * Timeline Drag-and-Drop Module
 * Enables dragging job slots between driver rows in the dispatch timeline.
 * Uses HTML5 Drag and Drop API — no external dependencies.
 */
(function () {
  'use strict';

  // ── State ──
  let draggedEl = null;
  let draggedLegId = null;
  let sourceDriverId = null;
  let sourceDriverName = null;
  const feasibilityCache = new Map();
  let undoTimer = null;
  let undoData = null;
  let scrollInterval = null;

  // ── CSRF ──
  function getCSRF() {
    const el = document.querySelector('[name=csrfmiddlewaretoken]');
    if (el) return el.value;
    const m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }

  // ── Feasibility check (debounced + cached) ──
  const pendingChecks = new Map();

  function checkFeasibility(legId, driverId) {
    const key = legId + '-' + driverId;
    if (feasibilityCache.has(key)) return Promise.resolve(feasibilityCache.get(key));
    if (pendingChecks.has(key)) return pendingChecks.get(key);

    const p = fetch('/dispatching/check-feasibility/?leg_id=' + legId + '&driver_id=' + driverId)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        feasibilityCache.set(key, data);
        pendingChecks.delete(key);
        return data;
      })
      .catch(function () {
        pendingChecks.delete(key);
        return { feasible: null, reason: 'Network error' };
      });
    pendingChecks.set(key, p);
    return p;
  }

  // ── Assignment API call ──
  // live_override (set via the held-day "Edit live" toggle) forces a write to the
  // live schedule even when the date is held — for emergency same-day changes.
  // overrideBooking: the dispatcher has already answered "Continue Anyway" to
  // the soft fleet booking on this car (the conflict modal led with it, or the
  // move is an undo back to where the trip was) — without it the server asks
  // first (409) and writes nothing.
  function assignLeg(legId, driverId, overrideBooking) {
    var body = { leg_id: legId, field: 'driver', value: driverId, live_override: !!window._draftEditLive };
    if (overrideBooking) body.override_booking = true;
    return fetch('/dispatching/update-leg-assignment/', {
      method: 'POST',
      headers: {
        'X-CSRFToken': getCSRF(),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json(); });
  }

  // A booking refusal from the server: {booking_conflict, can_override, error}.
  function isBookingRefusal(resp) {
    return !!(resp && resp.success === false && resp.booking_conflict);
  }

  // Say a booking refusal out loud. Soft → Promise<true> on Continue Anyway;
  // hard → shown, Promise<false>. booking-prompt.js does the asking when the
  // page loaded it; otherwise the toast (hard) / confirm (soft) stand in.
  function askBooking(resp) {
    if (window.BookingPrompt) return window.BookingPrompt.ask(resp);
    if (resp && resp.can_override) {
      return Promise.resolve(window.confirm((resp.error || 'This car is booked by fleet at that time.') +
                                            '\n\nOK = Continue Anyway.   Cancel = leave it.'));
    }
    showWarningsToast([{ severity: 'warning',
      text: 'Not assigned. ' + ((resp && resp.error) || 'This car is hard-booked by fleet at that time.') }]);
    return Promise.resolve(false);
  }

  // The booking sentence check-feasibility answers with (a soft clash only —
  // a hard one comes back as hard_block + reason).
  function bookingWarning(result) {
    return (result && typeof result.booking_warning === 'string' && result.booking_warning) || '';
  }

  // Feasibility warnings other than the booking sentence (so it is never
  // printed twice, whichever list the server put it in).
  function otherWarnings(result) {
    var bw = bookingWarning(result);
    return ((result && result.warnings) || []).filter(function (w) { return w && w !== bw; });
  }

  // ── Unassign (set driver to empty) ──
  function unassignLeg(legId) {
    return fetch('/dispatching/update-leg-assignment/', {
      method: 'POST',
      headers: {
        'X-CSRFToken': getCSRF(),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ leg_id: legId, field: 'driver', value: '', live_override: !!window._draftEditLive }),
    }).then(function (r) { return r.json(); });
  }

  // ── Clear row highlights ──
  function clearAllHighlights() {
    document.querySelectorAll('.driver-timeline-row').forEach(function (row) {
      row.classList.remove('dnd-over', 'dnd-feasible', 'dnd-infeasible', 'dnd-warning');
    });
  }

  // ── Auto-scroll during drag ──
  function startAutoScroll(container, clientY) {
    stopAutoScroll();
    var rect = container.getBoundingClientRect();
    var threshold = 40;
    var speed = 6;

    scrollInterval = setInterval(function () {
      if (clientY - rect.top < threshold) {
        container.scrollTop -= speed;
      } else if (rect.bottom - clientY < threshold) {
        container.scrollTop += speed;
      }
    }, 16);
  }

  function stopAutoScroll() {
    if (scrollInterval) {
      clearInterval(scrollInterval);
      scrollInterval = null;
    }
  }

  // ── Undo Toast ──
  function showUndoToast(msg, onUndo) {
    dismissUndoToast();
    var toast = document.createElement('div');
    toast.className = 'dnd-undo-toast';
    toast.innerHTML =
      '<div style="display:flex;align-items:center;gap:12px;">' +
        '<span>' + escapeHtml(msg) + '</span>' +
        '<button class="btn btn-sm btn-warning dnd-undo-btn">Undo</button>' +
        '<span class="dnd-undo-countdown" style="font-size:0.75rem;opacity:0.7;">8s</span>' +
      '</div>';
    document.body.appendChild(toast);

    var countdown = 8;
    var countdownEl = toast.querySelector('.dnd-undo-countdown');
    toast.querySelector('.dnd-undo-btn').addEventListener('click', function () {
      onUndo();
      dismissUndoToast();
    });

    undoTimer = setInterval(function () {
      countdown--;
      if (countdownEl) countdownEl.textContent = countdown + 's';
      if (countdown <= 0) dismissUndoToast();
    }, 1000);
    undoData = { toast: toast };
  }

  function dismissUndoToast() {
    if (undoTimer) { clearInterval(undoTimer); undoTimer = null; }
    if (undoData && undoData.toast && undoData.toast.parentNode) {
      undoData.toast.parentNode.removeChild(undoData.toast);
    }
    undoData = null;
  }

  // ── Show conflict modal ──
  // The page's own markup (title, "Assign Anyway") is the default. When fleet's
  // booking of the car is the ONLY thing to say, the modal becomes the booking
  // question itself — "Vehicle booked by fleet", the sentence, Continue Anyway
  // / Cancel — and goes back to the page's wording when it closes.
  var modalDefaults = null;

  function showConflictModal(result, onConfirm, onCancel) {
    var modal = document.getElementById('dndConflictModal');
    if (!modal) { onCancel(); return; }

    var titleEl = modal.querySelector('.modal-title');
    var confirmBtn = modal.querySelector('.dnd-conflict-confirm');
    var cancelBtn = modal.querySelector('.dnd-conflict-cancel');
    if (!modalDefaults) {
      modalDefaults = {
        title: titleEl ? titleEl.innerHTML : '',
        confirm: confirmBtn ? confirmBtn.innerHTML : '',
      };
    }

    var booking = bookingWarning(result);
    var others = otherWarnings(result);
    var bookingOnly = !!booking && result.feasible === true && !others.length &&
                      !result.vehicle_mismatch_detail;

    var body = modal.querySelector('.modal-body');
    var html = '';
    if (booking) {
      html += '<p class="mb-2"><i class="bi bi-calendar-event me-2" style="color:#C8A24A;"></i>' +
              escapeHtml(booking) + '</p>';
    }
    // "Issue:" is for a schedule that does not fit. A feasible result's reason
    // is a remark ("No other trips — fully available", "181min buffer") and
    // must never headline a warning about something else.
    if (result.reason && result.feasible === false) {
      html += '<p><strong>Issue:</strong> ' + escapeHtml(result.reason) + '</p>';
    }
    if (result.vehicle_mismatch_detail) {
      html += '<p><i class="bi bi-exclamation-triangle text-warning me-1"></i>' + escapeHtml(result.vehicle_mismatch_detail) + '</p>';
    }
    if (others.length) {
      html += '<ul>';
      others.forEach(function (w) { html += '<li>' + escapeHtml(w) + '</li>'; });
      html += '</ul>';
    }
    body.innerHTML = html;

    if (titleEl) {
      titleEl.innerHTML = bookingOnly
        ? '<i class="bi bi-calendar-event me-2" style="color:#C8A24A;"></i>Vehicle booked by fleet'
        : modalDefaults.title;
    }
    if (confirmBtn) {
      if (bookingOnly) confirmBtn.textContent = 'Continue Anyway';
      else confirmBtn.innerHTML = modalDefaults.confirm;
    }
    // Back to the page's own wording once the modal has faded out (not on the
    // click — the title would visibly flip during the fade).
    function restoreDefaults() {
      modal.removeEventListener('hidden.bs.modal', restoreDefaults);
      if (titleEl) titleEl.innerHTML = modalDefaults.title;
      if (confirmBtn) confirmBtn.innerHTML = modalDefaults.confirm;
    }
    modal.addEventListener('hidden.bs.modal', restoreDefaults);

    var bsModal = new bootstrap.Modal(modal);

    function cleanup() {
      confirmBtn.removeEventListener('click', onConfirmClick);
      cancelBtn.removeEventListener('click', onCancelClick);
      modal.removeEventListener('hidden.bs.modal', onHidden);
    }
    var resolved = false;
    function onConfirmClick() { resolved = true; cleanup(); bsModal.hide(); onConfirm(); }
    function onCancelClick() { resolved = true; cleanup(); bsModal.hide(); onCancel(); }
    function onHidden() { if (!resolved) { cleanup(); onCancel(); } }

    confirmBtn.addEventListener('click', onConfirmClick);
    cancelBtn.addEventListener('click', onCancelClick);
    modal.addEventListener('hidden.bs.modal', onHidden);
    bsModal.show();
  }

  function escapeHtml(str) {
    var d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  // ── Move slot DOM element between rows ──
  function moveSlotToRow(slotEl, targetRow, newDriverId) {
    var targetBar = targetRow.querySelector('.timeline-bar');
    if (!targetBar) return;
    slotEl.dataset.driverId = newDriverId;
    targetBar.appendChild(slotEl);
    // Update job count in source and target name columns
    updateRowJobCount(slotEl._sourceRow);
    updateRowJobCount(targetRow);
  }

  function moveSlotBack(slotEl, sourceRow, oldDriverId) {
    var sourceBar = sourceRow.querySelector('.timeline-bar');
    if (!sourceBar) return;
    slotEl.dataset.driverId = oldDriverId;
    sourceBar.appendChild(slotEl);
    updateRowJobCount(sourceRow);
    // Also update the row we took it from
    var currentRow = slotEl.closest('.driver-timeline-row');
    if (currentRow) updateRowJobCount(currentRow);
  }

  function updateRowJobCount(row) {
    if (!row) return;
    var bar = row.querySelector('.timeline-bar');
    if (!bar) return;
    var count = bar.querySelectorAll('.timeline-slot').length;
    var small = row.querySelector('.driver-name-col small');
    if (small) {
      // Update just the job count text
      var vehicleSpan = small.querySelector('span[style*="color:#0d6efd"]');
      var prefix = vehicleSpan ? vehicleSpan.outerHTML + ' &middot; ' : '';
      small.innerHTML = prefix + count + ' job' + (count !== 1 ? 's' : '');
    }
  }

  // ── Execute the assignment ──
  // bookingConfirmed: the dispatcher has already read the fleet-booking
  // sentence in the conflict modal and chosen to continue, so the write goes
  // with override_booking and the server does not ask a second time.
  function executeAssignment(slotEl, targetRow, targetDriverId, targetDriverName, bookingConfirmed) {
    var legId = slotEl.dataset.legId;
    var customerName = slotEl.dataset.customer || 'Job';
    var timeStr = slotEl.dataset.time || '';
    var srcName = sourceDriverName || 'Unassigned';
    var tgtName = targetDriverName || 'driver';

    // Save source row ref for undo
    slotEl._sourceRow = slotEl.closest('.driver-timeline-row');
    var origDriverId = sourceDriverId;
    var origRow = slotEl._sourceRow;

    var promise;
    if (targetDriverId === 'unassigned') {
      promise = unassignLeg(legId);
    } else {
      promise = assignLeg(legId, targetDriverId, !!bookingConfirmed);
    }

    promise.then(function (resp) {
      if (resp.success) {
        feasibilityCache.clear();
        // Advisory warnings for the assignment just made — survive the reload.
        // A booking the dispatcher has just said "continue" to is a decision
        // already made; it must not come back as a toast after the reload.
        var warnings = (resp.warnings || []).filter(function (w) {
          return !(bookingConfirmed && w && (w.code === 'vehicle_booking' || w.code === 'vehicle_booking_hard'));
        });
        if (warnings.length) stashAssignWarnings(warnings);
        // Show success toast briefly then reload to update layout
        showUndoToast(
          customerName + ' ' + timeStr + ': ' + srcName + ' → ' + tgtName,
          function () {
            // Undo: reassign back then reload. The stash from the reverted
            // assignment must die with it — a "Check this assignment" toast
            // about a move that no longer exists would misdirect.
            clearStashedAssignWarnings();
            var undoPromise;
            if (origDriverId === 'unassigned') {
              undoPromise = unassignLeg(legId);
            } else {
              // Putting the trip back where it was is not a new decision: a
              // soft booking on the original car does not ask again. A hard
              // one still refuses, and says so.
              undoPromise = assignLeg(legId, origDriverId, true);
            }
            undoPromise.then(function (r) {
              if (r.success) { window.location.reload(); return; }
              if (isBookingRefusal(r)) { askBooking(r); return; }
              showErrorToast((r && r.error) || 'Undo failed');
            });
          }
        );
        // Reload after short delay so user sees the toast
        setTimeout(function () { window.location.reload(); }, 1200);
      } else if (isBookingRefusal(resp) && !bookingConfirmed) {
        // Fleet booked the car after the schedule was checked (or the check
        // was cached). Ask now; nothing has been written.
        askBooking(resp).then(function (go) {
          if (go && resp.can_override) {
            executeAssignment(slotEl, targetRow, targetDriverId, targetDriverName, true);
          }
        });
      } else if (isBookingRefusal(resp)) {
        // Refused even though the dispatcher continued — a hard booking.
        askBooking(Object.assign({}, resp, { can_override: false }));
      } else {
        showErrorToast(resp.error || 'Assignment failed');
      }
    }).catch(function () {
      showErrorToast('Network error — assignment not saved');
    });
  }

  function showErrorToast(msg) {
    var toast = document.createElement('div');
    toast.className = 'dnd-undo-toast';
    toast.style.background = '#dc3545';
    toast.innerHTML = '<span>' + escapeHtml(msg) + '</span>';
    document.body.appendChild(toast);
    setTimeout(function () {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 4000);
  }

  // ── Assignment warnings toast (warn-only validation) ──
  // update-leg-assignment now returns advisory `warnings` for the assignment
  // just made (turn slack, shared-car checks). The assignment ALWAYS goes
  // through — these only inform. The board reloads right after a successful
  // drop, so the warnings are stashed in sessionStorage and rendered as a
  // dismissible toast once the reloaded page comes up.
  var WARN_KEY = 'dndAssignWarnings';
  var WARN_STASH_TTL_MS = 15000;

  function stashAssignWarnings(warnings) {
    // Only a REAL conflict interrupts. An assignment whose only remarks are
    // "info" (a tight turn, a shared-car note) is left alone: the board still
    // bands the turn, and the remark still rides along inside a toast that a
    // conflict raised. Founder's call, 2026-09-06, on the measurement — the
    // tight-turn class is right 69% of the time against the conflict class's
    // 90%, so one interruption in three was a false alarm.
    // The rule lives here, not at the two call sites, so the board and the
    // planner's quick-assign cannot drift apart.
    if (!warnings || !warnings.some(function (w) { return w && w.severity === 'warning'; })) {
      clearStashedAssignWarnings();
      return;
    }
    // Stamped with time + page so a stash orphaned by a failed reload can
    // never fire later on the wrong page or for a long-gone assignment.
    var payload = { ts: Date.now(), path: location.pathname, warnings: warnings };
    try { sessionStorage.setItem(WARN_KEY, JSON.stringify(payload)); } catch (e) { /* private mode */ }
  }

  function clearStashedAssignWarnings() {
    try { sessionStorage.removeItem(WARN_KEY); } catch (e) { /* private mode */ }
  }

  function showStashedAssignWarnings() {
    var raw = null;
    try {
      raw = sessionStorage.getItem(WARN_KEY);
      if (raw !== null) sessionStorage.removeItem(WARN_KEY);
    } catch (e) { return; }
    if (!raw) return;
    var payload;
    try { payload = JSON.parse(raw); } catch (e) { return; }
    if (!payload || !payload.warnings || !payload.warnings.length) return;
    if (!payload.ts || (Date.now() - payload.ts) > WARN_STASH_TTL_MS) return;
    if (payload.path && payload.path !== location.pathname) return;
    showWarningsToast(payload.warnings);
  }

  function showWarningsToast(warnings) {
    var toast = document.createElement('div');
    toast.className = 'dnd-warn-toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    var rows = '';
    warnings.forEach(function (w) {
      var sev = (w && w.severity === 'warning') ? 'warn' : 'info';
      var text = (w && w.text) ? w.text : String(w);
      rows += '<div class="dnd-warn-row dnd-warn-' + sev + '">' + escapeHtml(text) + '</div>';
    });
    toast.innerHTML =
      '<div class="dnd-warn-head">' +
        '<span>Check this assignment</span>' +
        '<button type="button" class="dnd-warn-close" aria-label="Dismiss">&times;</button>' +
      '</div>' + rows;
    document.body.appendChild(toast);
    function dismiss() { if (toast.parentNode) toast.parentNode.removeChild(toast); }
    toast.querySelector('.dnd-warn-close').addEventListener('click', dismiss);
    setTimeout(dismiss, 30000);
  }

  // Pages that assign through their own fetch (the planner's quick-assign
  // dropdown) stash the endpoint's warnings here before their reload; init()
  // renders them after it, same as a drag-drop.
  window.dndStashAssignWarnings = stashAssignWarnings;

  // ── Cached row rects for fast hit-testing during drag ──
  var rowRectsCache = null;

  function buildRowRectsCache(container) {
    var rows = container.querySelectorAll('.driver-timeline-row');
    var cache = [];
    rows.forEach(function (row) {
      var rect = row.getBoundingClientRect();
      cache.push({ row: row, top: rect.top, bottom: rect.bottom });
    });
    return cache;
  }

  function findNearestRow(clientY) {
    if (!rowRectsCache) return null;
    for (var i = 0; i < rowRectsCache.length; i++) {
      var entry = rowRectsCache[i];
      if (clientY >= entry.top - 6 && clientY <= entry.bottom + 6) {
        return entry.row;
      }
    }
    return null;
  }

  // ── Initialize DnD ──
  function init() {
    // Warnings stashed by the drop that triggered this reload (shown on any
    // page that runs this module, even before the rows container mounts).
    showStashedAssignWarnings();

    var container = document.querySelector('.timeline-rows');
    if (!container) return;

    // Listen on the whole card for drag events so we can always preventDefault
    var card = container.closest('.card') || container;

    // Also find the unassigned chip pool (separate card above timeline)
    var unassignedPool = document.querySelector('[data-driver-id="unassigned"]');

    // Generic dragstart handler for both timeline slots and unassigned chips
    function handleDragStart(e) {
      var slot = e.target.closest('[draggable="true"]');
      if (!slot) { e.preventDefault(); return; }
      if (window._gapPopupActive) { e.preventDefault(); return; }
      // Board locked: a draft submitted for review is read-only until the
      // manager approves or requests changes.
      if (container.dataset.boardLocked === '1') {
        e.preventDefault();
        if (window.showBoardLockedNotice) window.showBoardLockedNotice();
        return;
      }

      draggedEl = slot;
      draggedLegId = slot.dataset.legId;
      sourceDriverId = slot.dataset.driverId;
      var parentRow = slot.closest('.driver-timeline-row');
      sourceDriverName = parentRow ? parentRow.dataset.driverName : 'Unassigned';

      // Cache row positions at drag start
      rowRectsCache = buildRowRectsCache(container);

      slot.classList.add('dnd-dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', draggedLegId);
    }

    // Attach dragstart to both containers
    container.addEventListener('dragstart', handleDragStart);
    if (unassignedPool) {
      unassignedPool.addEventListener('dragstart', handleDragStart);
    }

    // Drag over — must always preventDefault to allow drops
    // Throttled: only update highlights every 50ms
    var lastDragoverTime = 0;
    var lastHoveredRow = null;

    document.addEventListener('dragover', function (e) {
      if (!draggedEl) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';

      var now = Date.now();
      if (now - lastDragoverTime < 50) return;
      lastDragoverTime = now;

      // Find the row being hovered
      var row = e.target.closest('.driver-timeline-row') || findNearestRow(e.clientY);

      if (row === lastHoveredRow) return;
      lastHoveredRow = row;

      if (row) {
        clearAllHighlights();
        row.classList.add('dnd-over');

        var targetDriverId = row.dataset.driverId;
        if (targetDriverId && targetDriverId !== sourceDriverId && targetDriverId !== 'unassigned') {
          checkFeasibility(draggedLegId, targetDriverId).then(function (result) {
            if (!row.classList.contains('dnd-over')) return;
            if (result.hard_block) {
              row.classList.add('dnd-infeasible');
            } else if (result.feasible === true && !otherWarnings(result).length && !bookingWarning(result)) {
              row.classList.add('dnd-feasible');
            } else if (result.feasible === true) {
              row.classList.add('dnd-warning');
            } else if (result.feasible === false) {
              row.classList.add('dnd-infeasible');
            }
          });
        } else if (targetDriverId === 'unassigned') {
          row.classList.add('dnd-warning');
        }
      } else {
        clearAllHighlights();
      }

      var wrapper = container.closest('.card-body') || container;
      startAutoScroll(wrapper, e.clientY);
    });

    // Drag leave
    document.addEventListener('dragleave', function (e) {
      var row = e.target.closest('.driver-timeline-row');
      if (!row) return;
      var related = e.relatedTarget;
      if (related && row.contains(related)) return;
      row.classList.remove('dnd-over', 'dnd-feasible', 'dnd-infeasible', 'dnd-warning');
    });

    // Drop
    document.addEventListener('drop', function (e) {
      if (!draggedEl) return;
      e.preventDefault();
      stopAutoScroll();
      clearAllHighlights();

      var row = e.target.closest('.driver-timeline-row') || findNearestRow(e.clientY);
      if (!row) {
        draggedEl.classList.remove('dnd-dragging');
        draggedEl = null;
        return;
      }

      var targetDriverId = row.dataset.driverId;
      var targetDriverName = row.dataset.driverName || 'driver';

      // Same driver — no-op
      if (targetDriverId === sourceDriverId) {
        draggedEl.classList.remove('dnd-dragging');
        draggedEl = null;
        return;
      }

      var slotEl = draggedEl;
      slotEl.classList.remove('dnd-dragging');

      // For unassigned target, skip feasibility check
      if (targetDriverId === 'unassigned') {
        executeAssignment(slotEl, row, targetDriverId, targetDriverName);
        draggedEl = null;
        return;
      }

      // Check feasibility before committing
      checkFeasibility(draggedLegId, targetDriverId).then(function (result) {
        // Fleet has hard-booked this chauffeur's car across the trip. The
        // server refuses the write, so offering "assign anyway" would only
        // lead to a second refusal — say why, once, with nothing to continue.
        if (result.hard_block) {
          askBooking({ success: false, booking_conflict: true, hard: true, can_override: false,
                       error: result.reason || 'This car is hard-booked by fleet at that time.' });
          return;
        }
        // The modal led with the booking sentence, so "continue" there is the
        // dispatcher's answer to it — the write carries override_booking.
        var askedBooking = !!bookingWarning(result);
        if (result.feasible === true && !otherWarnings(result).length && !askedBooking) {
          executeAssignment(slotEl, row, targetDriverId, targetDriverName);
        } else if (result.feasible === true) {
          showConflictModal(result, function () {
            executeAssignment(slotEl, row, targetDriverId, targetDriverName, askedBooking);
          }, function () { /* cancelled */ });
        } else if (result.feasible === false) {
          showConflictModal(result, function () {
            executeAssignment(slotEl, row, targetDriverId, targetDriverName, askedBooking);
          }, function () { /* cancelled */ });
        } else {
          showErrorToast('Could not verify schedule — try again');
        }
      });

      draggedEl = null;
    });

    // Drag end (cleanup)
    document.addEventListener('dragend', function () {
      stopAutoScroll();
      clearAllHighlights();
      if (draggedEl) {
        draggedEl.classList.remove('dnd-dragging');
        draggedEl = null;
      }
      rowRectsCache = null;
      lastHoveredRow = null;
    });
  }

  // Start when DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
