/**
 * Booking Prompt — the one question dispatch is asked when fleet has booked
 * the car a trip (or a whole day's car) is about to land on.
 *
 * Fleet books part of a car's day from Fleet → The day. A SOFT booking leaves
 * the car usable: every assign endpoint answers 409 with
 *   {success:false, booking_conflict:true, hard:false, can_override:true, error}
 * and writes nothing until the caller comes back with override_booking:true.
 * A HARD booking answers the same body with can_override:false — there is
 * nothing to continue, the way through is fleet moving the booking.
 *
 * Every dispatch page asks that question the same way, through here:
 *
 *   BookingPrompt.ask(data)                  → Promise<boolean>
 *       Soft: "Vehicle booked by fleet", the sentence, Cancel / Continue Anyway.
 *       Hard: "Car hard-booked by fleet", the sentence, a single OK (→ false).
 *   BookingPrompt.postWithBookingPrompt(url, body, opts) → Promise<data>
 *       POSTs JSON with the CSRF token; on a booking refusal asks, and on
 *       Continue re-POSTs the same body plus override_booking:true. Resolves
 *       with the final JSON, or {success:false, cancelled:true, ...} when the
 *       dispatcher cancelled or the car is hard-booked (already shown).
 *   BookingPrompt.escapeHtml(s)
 *
 * Bootstrap loads AFTER page content on these pages, so window.bootstrap is
 * only looked at when a question is actually asked — never at load. Without
 * it (a standalone page) the question falls back to confirm()/alert().
 */
(function () {
  'use strict';

  if (window.BookingPrompt) return;   // included twice — the first copy stands

  var SOFT_TITLE = 'Vehicle booked by fleet';
  var HARD_TITLE = 'Car hard-booked by fleet';

  function escapeHtml(s) {
    return s == null ? '' : String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function csrfToken(explicit) {
    if (explicit) return explicit;
    var el = document.querySelector('[name=csrfmiddlewaretoken]');
    if (el && el.value) return el.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  // Soft unless the server said there is nothing to continue.
  function isSoft(data) {
    if (!data) return false;
    if (data.can_override === true) return true;
    if (data.can_override === false) return false;
    return !data.hard;
  }

  function isRefusal(data) {
    return !!(data && data.booking_conflict && data.success === false);
  }

  // ── The modal ──────────────────────────────────────────────────────────
  // Built on first use and reused. It sits above every other overlay on these
  // pages (the copy-yesterday sheet is z 10000) because it is asked FROM them.
  var CSS =
    '.modal.bp-modal{z-index:10060;background:rgba(15,27,61,.46);}' +
    '.bp-modal .modal-dialog{max-width:440px;}' +
    '.bp-modal .modal-content{border:0;border-radius:14px;overflow:hidden;' +
      'box-shadow:0 2px 6px rgba(15,27,61,.08),0 22px 48px rgba(15,27,61,.22);' +
      'background:linear-gradient(180deg,#fff 0%,#fcfbf8 100%);color:#1d2438;}' +
    '.bp-modal .bp-head{display:flex;align-items:center;gap:12px;padding:20px 22px 6px;}' +
    '.bp-modal .bp-mark{flex:0 0 38px;height:38px;border-radius:50%;display:flex;' +
      'align-items:center;justify-content:center;font-size:1.05rem;' +
      'background:#faf4e6;color:#a8842f;box-shadow:inset 0 0 0 1px #eadcb6;}' +
    '.bp-modal.bp-hard .bp-mark{background:#faedea;color:#a32a1f;box-shadow:inset 0 0 0 1px #ebcfc9;}' +
    '.bp-modal .bp-eyebrow{font-size:.62rem;font-weight:700;letter-spacing:.12em;' +
      'text-transform:uppercase;color:#a8842f;margin:0 0 2px;}' +
    '.bp-modal.bp-hard .bp-eyebrow{color:#a32a1f;}' +
    '.bp-modal .bp-title{font-size:1.02rem;font-weight:600;letter-spacing:.005em;margin:0;color:#0f1b3d;}' +
    '.bp-modal .bp-text{padding:10px 22px 4px 72px;font-size:.88rem;line-height:1.55;' +
      'color:#3a4156;white-space:pre-line;overflow-wrap:anywhere;margin:0;' +
      'max-height:52vh;overflow-y:auto;}' +
    '.bp-modal .bp-foot{display:flex;justify-content:flex-end;gap:10px;padding:18px 22px 20px;}' +
    '.bp-modal .bp-btn{font-size:.8rem;font-weight:600;letter-spacing:.02em;border-radius:9px;' +
      'padding:8px 16px;border:1px solid transparent;transition:background .18s ease,' +
      'color .18s ease,box-shadow .18s ease,transform .18s ease;}' +
    '.bp-modal .bp-btn:focus-visible{outline:2px solid #c8a24a;outline-offset:2px;}' +
    '.bp-modal .bp-cancel{background:#fff;color:#1d2438;border-color:#dfe2ea;}' +
    '.bp-modal .bp-cancel:hover{background:#f6f7fb;border-color:#cfd3de;}' +
    '.bp-modal .bp-go{background:#0f1b3d;color:#f3e3b5;box-shadow:0 1px 2px rgba(15,27,61,.2);}' +
    '.bp-modal .bp-go:hover{background:#1a2a57;color:#fff;transform:translateY(-1px);' +
      'box-shadow:0 6px 16px rgba(15,27,61,.22);}' +
    '@media (max-width:480px){.bp-modal .bp-text{padding-left:22px;}' +
      '.bp-modal .bp-foot{flex-direction:column-reverse;}.bp-modal .bp-btn{width:100%;padding:11px 16px;}}';

  var modalEl = null;

  function ensureModal() {
    if (modalEl) return modalEl;
    if (!document.getElementById('bpStyle')) {
      var style = document.createElement('style');
      style.id = 'bpStyle';
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    modalEl = document.createElement('div');
    modalEl.className = 'modal fade bp-modal';
    modalEl.id = 'bookingPromptModal';
    modalEl.tabIndex = -1;
    modalEl.setAttribute('aria-hidden', 'true');
    modalEl.setAttribute('aria-labelledby', 'bookingPromptTitle');
    modalEl.innerHTML =
      '<div class="modal-dialog modal-dialog-centered">' +
        '<div class="modal-content">' +
          '<div class="bp-head">' +
            '<span class="bp-mark" aria-hidden="true"><i class="bi"></i></span>' +
            '<div><p class="bp-eyebrow">Fleet booking</p>' +
            '<h5 class="bp-title" id="bookingPromptTitle"></h5></div>' +
          '</div>' +
          '<p class="bp-text"></p>' +
          '<div class="bp-foot">' +
            '<button type="button" class="bp-btn bp-cancel">Cancel</button>' +
            '<button type="button" class="bp-btn bp-go">Continue Anyway</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modalEl);
    return modalEl;
  }

  // mode: 'soft' (Cancel / Continue Anyway → true|false), 'hard' (OK → false),
  // 'info' (OK → true; a report after the fact, e.g. what a publish skipped).
  function fallbackAsk(mode, title, text) {
    if (mode === 'soft') {
      return window.confirm(title + '\n\n' + text +
                            '\n\nOK = Continue Anyway.   Cancel = leave it as it was.');
    }
    window.alert(title + '\n\n' + text);
    return mode === 'info';
  }

  function askNow(data, mode, title) {
    var soft = mode === 'soft';
    var text = (data && data.error) ||
      (mode === 'hard' ? 'This car is hard-booked by fleet at that time.'
                       : 'This car is booked by fleet at that time.');
    var BS = window.bootstrap;
    if (!BS || !BS.Modal || !document.body) {
      return Promise.resolve(fallbackAsk(mode, title, text));
    }
    var el = ensureModal();
    el.classList.toggle('bp-hard', mode === 'hard');
    el.querySelector('.bp-mark i').className = 'bi ' + (mode === 'hard' ? 'bi-lock-fill' : 'bi-calendar-event');
    el.querySelector('.bp-title').textContent = title;
    // Server text goes in as TEXT — the booking reason is free text fleet typed.
    el.querySelector('.bp-text').textContent = text;
    var go = el.querySelector('.bp-go');
    var cancel = el.querySelector('.bp-cancel');
    go.textContent = soft ? 'Continue Anyway' : 'OK';
    cancel.style.display = soft ? '' : 'none';

    return new Promise(function (resolve) {
      var answer = mode === 'info';
      var modal = BS.Modal.getOrCreateInstance(el, { backdrop: false, keyboard: true, focus: true });
      function onGo() { answer = mode !== 'hard'; modal.hide(); }
      function onCancel() { answer = false; modal.hide(); }
      function onShown() { (soft ? cancel : go).focus(); }
      function onHidden() {
        go.removeEventListener('click', onGo);
        cancel.removeEventListener('click', onCancel);
        el.removeEventListener('shown.bs.modal', onShown);
        el.removeEventListener('hidden.bs.modal', onHidden);
        // Asked from inside another open modal: closing this one must not
        // unlock the page under that one.
        if (document.querySelector('.modal.show')) document.body.classList.add('modal-open');
        resolve(answer);
      }
      go.addEventListener('click', onGo);
      cancel.addEventListener('click', onCancel);
      el.addEventListener('shown.bs.modal', onShown);
      el.addEventListener('hidden.bs.modal', onHidden);
      modal.show();
    });
  }

  // One question at a time: a second refusal waits for the first answer.
  var queue = Promise.resolve();
  function enqueue(data, mode, title) {
    var turn = queue.then(function () { return askNow(data, mode, title); });
    queue = turn.catch(function () { return false; });
    return turn;
  }

  function ask(data) {
    return isSoft(data) ? enqueue(data, 'soft', SOFT_TITLE) : enqueue(data, 'hard', HARD_TITLE);
  }

  // A report, not a question: what a bulk action did around fleet's bookings
  // (published anyway, skipped, copied). Single OK; resolves when it closes.
  function tell(text, opts) {
    opts = opts || {};
    return enqueue({ error: text }, opts.hard ? 'hard' : 'info',
                   opts.title || (opts.hard ? HARD_TITLE : SOFT_TITLE));
  }

  // "• text" lines for a list of {text} (or plain strings), capped so a
  // modal never becomes a wall; the rest are counted.
  function listLines(items, max) {
    max = max || 8;
    var texts = (items || []).map(function (i) { return typeof i === 'string' ? i : (i && i.text) || ''; })
      .filter(Boolean);
    var lines = texts.slice(0, max).map(function (t) { return '• ' + t; });
    if (texts.length > max) lines.push('…and ' + (texts.length - max) + ' more');
    return lines.join('\n');
  }

  // ── POST with the question built in ─────────────────────────────────────
  function post(url, body, opts) {
    opts = opts || {};
    var headers = {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken(opts.csrfToken),
      'X-Requested-With': 'XMLHttpRequest',
    };
    Object.keys(opts.headers || {}).forEach(function (k) { headers[k] = opts.headers[k]; });
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: headers,
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.text().then(function (txt) {
        var data;
        try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = null; }
        if (!data || typeof data !== 'object') {
          var err = new Error('The server did not answer properly (' + r.status + ')');
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }

  function cancelled(data) {
    return {
      success: false,
      cancelled: true,
      booking_conflict: true,
      hard: !isSoft(data),
      error: (data && data.error) || '',
    };
  }

  function postWithBookingPrompt(url, body, opts) {
    var sent = {};
    Object.keys(body || {}).forEach(function (k) { sent[k] = body[k]; });
    return post(url, sent, opts).then(function (data) {
      if (!isRefusal(data)) return data;
      return ask(data).then(function (go) {
        if (!go || !isSoft(data)) return cancelled(data);
        sent.override_booking = true;
        return post(url, sent, opts).then(function (again) {
          if (!isRefusal(again)) return again;
          // Refused even with the override — fleet hardened the booking
          // between the two clicks. Say so; there is nothing left to continue.
          var hard = {};
          Object.keys(again).forEach(function (k) { hard[k] = again[k]; });
          hard.can_override = false;
          return ask(hard).then(function () { return cancelled(hard); });
        });
      });
    });
  }

  window.BookingPrompt = {
    escapeHtml: escapeHtml,
    ask: ask,
    tell: tell,
    listLines: listLines,
    isRefusal: isRefusal,
    isSoft: isSoft,
    post: post,
    postWithBookingPrompt: postWithBookingPrompt,
  };
})();
