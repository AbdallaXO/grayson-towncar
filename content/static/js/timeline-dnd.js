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
  function assignLeg(legId, driverId) {
    return fetch('/dispatching/update-leg-assignment/', {
      method: 'POST',
      headers: {
        'X-CSRFToken': getCSRF(),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ leg_id: legId, field: 'driver', value: driverId }),
    }).then(function (r) { return r.json(); });
  }

  // ── Unassign (set driver to empty) ──
  function unassignLeg(legId) {
    return fetch('/dispatching/update-leg-assignment/', {
      method: 'POST',
      headers: {
        'X-CSRFToken': getCSRF(),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ leg_id: legId, field: 'driver', value: '' }),
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
        '<span>' + msg + '</span>' +
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
  function showConflictModal(result, onConfirm, onCancel) {
    var modal = document.getElementById('dndConflictModal');
    if (!modal) { onCancel(); return; }

    var body = modal.querySelector('.modal-body');
    var html = '';
    if (result.reason) {
      html += '<p><strong>Issue:</strong> ' + escapeHtml(result.reason) + '</p>';
    }
    if (result.vehicle_mismatch_detail) {
      html += '<p><i class="bi bi-exclamation-triangle text-warning me-1"></i>' + escapeHtml(result.vehicle_mismatch_detail) + '</p>';
    }
    if (result.warnings && result.warnings.length) {
      html += '<ul>';
      result.warnings.forEach(function (w) { html += '<li>' + escapeHtml(w) + '</li>'; });
      html += '</ul>';
    }
    body.innerHTML = html;

    var bsModal = new bootstrap.Modal(modal);

    var confirmBtn = modal.querySelector('.dnd-conflict-confirm');
    var cancelBtn = modal.querySelector('.dnd-conflict-cancel');

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
  function executeAssignment(slotEl, targetRow, targetDriverId, targetDriverName) {
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
      promise = assignLeg(legId, targetDriverId);
    }

    promise.then(function (resp) {
      if (resp.success) {
        feasibilityCache.clear();
        // Show success toast briefly then reload to update layout
        showUndoToast(
          customerName + ' ' + timeStr + ': ' + srcName + ' → ' + tgtName,
          function () {
            // Undo: reassign back then reload
            var undoPromise;
            if (origDriverId === 'unassigned') {
              undoPromise = unassignLeg(legId);
            } else {
              undoPromise = assignLeg(legId, origDriverId);
            }
            undoPromise.then(function (r) {
              if (r.success) window.location.reload();
            });
          }
        );
        // Reload after short delay so user sees the toast
        setTimeout(function () { window.location.reload(); }, 1200);
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
            if (result.feasible === true && (!result.warnings || !result.warnings.length)) {
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
        if (result.feasible === true && (!result.warnings || !result.warnings.length)) {
          executeAssignment(slotEl, row, targetDriverId, targetDriverName);
        } else if (result.feasible === true) {
          showConflictModal(result, function () {
            executeAssignment(slotEl, row, targetDriverId, targetDriverName);
          }, function () { /* cancelled */ });
        } else if (result.feasible === false) {
          showConflictModal(result, function () {
            executeAssignment(slotEl, row, targetDriverId, targetDriverName);
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
