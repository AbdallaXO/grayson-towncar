// static/js/guest-quote.js
// Experimental guest-needs-first quote form — recommendation engine + form handler
// Completely isolated from the existing quote_form.js

(function () {
  'use strict';

  class GuestQuoteForm {
    constructor() {
      this.vehicles = window.GTC_VEHICLES || [];
      this.rates = window.GTC_RATES || {};
      this.endpoint = window.GTC_QUOTE_ENDPOINT || '/quote-form-handler/';
      this.staticUrl = window.GTC_STATIC_URL || '/static/';
      this.selectedVehicleId = null;
      this.debounceTimer = null;

      this.cacheElements();
      this.bindEvents();
      this.setMinDate();
    }

    cacheElements() {
      const $ = (sel) => document.querySelector(sel);
      const $$ = (sel) => document.querySelectorAll(sel);

      this.els = {
        form: $('#guest-quote-form'),
        pickup: $('#gq-pickup'),
        dropoff: $('#gq-dropoff'),
        date: $('#gq-date'),
        passengers: $('#gq-passengers'),
        luggage: $('#gq-luggage'),
        carseats: $('#gq-carseats'),
        tripOneway: $('#gq-oneway'),
        tripRoundtrip: $('#gq-roundtrip'),
        firstName: $('#gq-first-name'),
        lastName: $('#gq-last-name'),
        email: $('#gq-email'),
        phone: $('#gq-phone'),
        submitBtn: $('#gq-submit'),
        // Recommendation area
        recommendation: $('#gq-recommendation'),
        vehicleCards: $('#gq-vehicle-cards'),
        chooseOtherBtn: $('#gq-choose-other-btn'),
        allVehicles: $('#gq-all-vehicles'),
        allVehiclesGrid: $('#gq-all-vehicles-grid'),
        overflowMsg: $('#gq-overflow-msg'),
        // Results
        results: $('#gq-results'),
        resultPrice: $('#gq-result-price'),
        resultRoute: $('#gq-result-route'),
        resultActions: $('#gq-result-actions'),
        invalidResult: $('#gq-invalid-result'),
        // Steps
        steps: $$('.gq-step'),
        stepLines: $$('.gq-step-line'),
      };
    }

    bindEvents() {
      // Recommendation triggers (passengers, luggage, carseats)
      ['passengers', 'luggage', 'carseats'].forEach((field) => {
        this.els[field].addEventListener('input', () => {
          this.debounceRecommend();
          this.updateStepIndicator();
        });
      });

      // Location change triggers
      [this.els.pickup, this.els.dropoff].forEach((el) => {
        el.addEventListener('change', () => {
          this.validateLocations();
          this.updateStepIndicator();
        });
      });

      // Choose other vehicle toggle
      if (this.els.chooseOtherBtn) {
        this.els.chooseOtherBtn.addEventListener('click', () => {
          this.els.allVehicles.classList.toggle('visible');
          const isOpen = this.els.allVehicles.classList.contains('visible');
          this.els.chooseOtherBtn.textContent = isOpen
            ? 'Hide other vehicles'
            : 'Choose a different vehicle';
        });
      }

      // Contact field focus → step indicator (step 2)
      ['firstName', 'lastName', 'email', 'phone'].forEach((field) => {
        this.els[field].addEventListener('focus', () => this.setStep(2));
      });

      // Form submit
      this.els.form.addEventListener('submit', (e) => {
        e.preventDefault();
        if (this.validate()) this.submit();
      });

      // Stepper +/- buttons
      document.querySelectorAll('.gq-stepper-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
          const targetId = btn.dataset.target;
          const input = document.getElementById(targetId);
          if (!input) return;

          const min = parseInt(input.min) || 0;
          const max = parseInt(input.max) || 99;
          let val = parseInt(input.value) || 0;

          if (btn.classList.contains('gq-stepper-plus')) {
            val = Math.min(val + 1, max);
          } else {
            val = Math.max(val - 1, min);
          }

          input.value = val;
          input.dispatchEvent(new Event('input', { bubbles: true }));
          this.updateStepperStates();
        });
      });
      this.updateStepperStates();

      // Render all-vehicles grid
      this.renderAllVehicles();

      // Fire initial recommendation if passengers already has a value
      this.recommend();
    }

    updateStepperStates() {
      document.querySelectorAll('.gq-stepper').forEach((stepper) => {
        const input = stepper.querySelector('.gq-stepper-value');
        const minusBtn = stepper.querySelector('.gq-stepper-minus');
        const plusBtn = stepper.querySelector('.gq-stepper-plus');
        if (!input || !minusBtn || !plusBtn) return;

        const val = parseInt(input.value) || 0;
        const min = parseInt(input.min) || 0;
        const max = parseInt(input.max) || 99;

        minusBtn.disabled = val <= min;
        plusBtn.disabled = val >= max;
      });
    }

    setMinDate() {
      const today = new Date().toISOString().split('T')[0];
      this.els.date.setAttribute('min', today);
    }

    // ── Step Indicator ────────────────────────────────
    updateStepIndicator() {
      const hasTrip = this.els.pickup.value && this.els.dropoff.value;
      const hasNeeds = parseInt(this.els.passengers.value) >= 1;

      if (hasNeeds && this.selectedVehicleId) {
        this.setStep(3);
      } else if (hasTrip) {
        this.setStep(2);
      } else {
        this.setStep(1);
      }
    }

    setStep(n) {
      this.els.steps.forEach((step, i) => {
        const num = i + 1;
        step.classList.toggle('active', num === n);
        step.classList.toggle('completed', num < n);
      });
      this.els.stepLines.forEach((line, i) => {
        line.classList.toggle('completed', i + 1 < n);
      });
    }

    // ── Recommendation Engine ─────────────────────────
    debounceRecommend() {
      clearTimeout(this.debounceTimer);
      this.debounceTimer = setTimeout(() => this.recommend(), 200);
    }

    recommend() {
      const passengers = parseInt(this.els.passengers.value) || 0;
      const luggage = parseInt(this.els.luggage.value) || 0;
      const carseats = parseInt(this.els.carseats.value) || 0;

      if (passengers < 1) {
        this.hideRecommendation();
        return;
      }

      // Car seats take up passenger seats
      const totalPassengers = passengers + carseats;

      let bestFit = null;
      let upsell = null;

      // vehicles are sorted by capacity ascending
      for (let i = 0; i < this.vehicles.length; i++) {
        const v = this.vehicles[i];
        if (
          v.capacity >= totalPassengers &&
          v.luggage_capacity >= luggage &&
          v.carseats_capacity >= carseats
        ) {
          if (!bestFit) {
            bestFit = v;
          } else if (!upsell) {
            upsell = v;
            break;
          }
        }
      }

      if (!bestFit) {
        // Nothing fits — show largest vehicle + overflow message
        bestFit = this.vehicles[this.vehicles.length - 1];
        this.showRecommendation(bestFit, null, true);
      } else {
        this.showRecommendation(bestFit, upsell, false);
      }
    }

    showRecommendation(bestFit, upsell, isOverflow) {
      this.selectedVehicleId = bestFit.id;

      const passengers = parseInt(this.els.passengers.value) || 0;
      const luggage = parseInt(this.els.luggage.value) || 0;
      const carseats = parseInt(this.els.carseats.value) || 0;

      let html = '';
      const hasUpsell = upsell && !isOverflow;

      html += this.renderVehicleCard(bestFit, 'best-fit', 'Best Fit', this.getExplanation(bestFit, passengers, luggage, carseats));

      if (hasUpsell) {
        html += this.renderVehicleCard(upsell, 'upsell', 'Extra Room', this.getUpsellExplanation(upsell, bestFit));
      }

      this.els.vehicleCards.innerHTML = html;
      this.els.vehicleCards.classList.toggle('has-upsell', hasUpsell);

      // Overflow message
      if (this.els.overflowMsg) {
        this.els.overflowMsg.style.display = isOverflow ? 'block' : 'none';
      }

      // Show recommendation section
      this.els.recommendation.classList.add('visible');

      // Bind card click events
      this.els.vehicleCards.querySelectorAll('.gq-vehicle-card').forEach((card) => {
        card.addEventListener('click', () => this.selectVehicle(parseInt(card.dataset.vehicleId)));
      });

      // Update mini-card selection
      this.updateMiniCardSelection();
      this.updateStepIndicator();
    }

    renderVehicleCard(vehicle, type, badgeText, explanation) {
      const isSelected = vehicle.id === this.selectedVehicleId;
      const badgeClass = type === 'best-fit' ? 'gq-badge-best' : 'gq-badge-upgrade';

      return `
        <div class="gq-vehicle-card ${type} ${isSelected ? 'selected' : ''}" data-vehicle-id="${vehicle.id}">
          <span class="gq-badge ${badgeClass}">${badgeText}</span>
          <div class="gq-check"><i class="bi bi-check2"></i></div>
          <img class="gq-vehicle-img" src="${this.staticUrl}${vehicle.image}" alt="${vehicle.display_name}">
          <div class="gq-vehicle-name">${vehicle.display_name}</div>
          <div class="gq-vehicle-specs">
            <div class="gq-spec">
              <span class="gq-spec-label"><i class="bi bi-people-fill"></i> Passengers</span>
              <span class="gq-spec-value">Up to ${vehicle.capacity}</span>
            </div>
            <div class="gq-spec">
              <span class="gq-spec-label"><i class="bi bi-suitcase-lg-fill"></i> Luggage</span>
              <span class="gq-spec-value">Up to ${vehicle.luggage_capacity}</span>
            </div>
            <div class="gq-spec">
              <span class="gq-spec-label"><i class="bi bi-shield-fill-check"></i> Child Seats</span>
              <span class="gq-spec-value">Up to ${vehicle.carseats_capacity}</span>
            </div>
          </div>
          <div class="gq-vehicle-why">${explanation}</div>
        </div>
      `;
    }

    getExplanation(vehicle, passengers, luggage, carseats) {
      const parts = [];
      parts.push(`Comfortably fits your group of ${passengers}`);
      if (luggage > 0) parts[0] += ` with ${luggage} bag${luggage > 1 ? 's' : ''}`;
      if (carseats > 0) parts.push(`Includes ${carseats} child seat${carseats > 1 ? 's' : ''}`);
      return parts.join('. ') + '.';
    }

    getUpsellExplanation(upsell, bestFit) {
      const extraSeats = upsell.capacity - bestFit.capacity;
      const extraBags = upsell.luggage_capacity - bestFit.luggage_capacity;
      const parts = [];
      if (extraSeats > 0) parts.push(`${extraSeats} extra seat${extraSeats > 1 ? 's' : ''}`);
      if (extraBags > 0) parts.push(`room for ${extraBags} more bag${extraBags > 1 ? 's' : ''}`);
      if (parts.length > 0) {
        return 'More space: ' + parts.join(' and ') + ' for extra comfort.';
      }
      return 'A more spacious ride for extra comfort.';
    }

    hideRecommendation() {
      this.els.recommendation.classList.remove('visible');
      this.selectedVehicleId = null;
      this.updateStepIndicator();
    }

    selectVehicle(vehicleId) {
      this.selectedVehicleId = vehicleId;

      // Update card selection
      this.els.vehicleCards.querySelectorAll('.gq-vehicle-card').forEach((card) => {
        card.classList.toggle('selected', parseInt(card.dataset.vehicleId) === vehicleId);
      });

      this.updateMiniCardSelection();
      this.updateStepIndicator();
    }

    // ── All Vehicles Grid ─────────────────────────────
    renderAllVehicles() {
      if (!this.els.allVehiclesGrid) return;

      let html = '';
      this.vehicles.forEach((v) => {
        html += `
          <div class="gq-mini-card" data-vehicle-id="${v.id}">
            <img src="${this.staticUrl}${v.image}" alt="${v.display_name}">
            <div class="mini-name">${v.display_name}</div>
            <div class="mini-cap">${v.capacity} pax · ${v.luggage_capacity} bags</div>
          </div>
        `;
      });
      this.els.allVehiclesGrid.innerHTML = html;

      // Bind clicks
      this.els.allVehiclesGrid.querySelectorAll('.gq-mini-card').forEach((card) => {
        card.addEventListener('click', () => {
          this.selectVehicle(parseInt(card.dataset.vehicleId));
        });
      });
    }

    updateMiniCardSelection() {
      if (!this.els.allVehiclesGrid) return;
      this.els.allVehiclesGrid.querySelectorAll('.gq-mini-card').forEach((card) => {
        card.classList.toggle('selected', parseInt(card.dataset.vehicleId) === this.selectedVehicleId);
      });
    }

    // ── Price Display ─────────────────────────────────
    getRate(vehicleId) {
      const pickup = this.els.pickup.value;
      const dropoff = this.els.dropoff.value;
      if (!pickup || !dropoff || !vehicleId) return null;

      const key = [pickup, dropoff].sort().join('-');
      return this.rates[String(vehicleId)]?.[key] || null;
    }

    // ── Validation ────────────────────────────────────
    validate() {
      let valid = true;
      let firstInvalid = null;

      const required = [
        { el: this.els.pickup, name: 'pickup location' },
        { el: this.els.dropoff, name: 'dropoff location' },
        { el: this.els.passengers, name: 'passengers', min: 1 },
        { el: this.els.firstName, name: 'first name' },
        { el: this.els.lastName, name: 'last name' },
        { el: this.els.email, name: 'email' },
        { el: this.els.phone, name: 'phone' },
      ];

      // Clear previous errors
      document.querySelectorAll('.gq-input, .gq-select').forEach((el) => {
        el.classList.remove('is-invalid', 'is-valid');
      });
      document.querySelectorAll('.gq-stepper').forEach((el) => {
        el.classList.remove('is-invalid');
      });
      document.querySelectorAll('.gq-invalid-feedback').forEach((el) => el.remove());

      required.forEach((field) => {
        const val = field.el.value.trim();
        let isInvalid = false;

        if (!val) {
          isInvalid = true;
        } else if (field.min && parseInt(val) < field.min) {
          isInvalid = true;
        } else if (field.el === this.els.email && !this.isValidEmail(val)) {
          isInvalid = true;
        }

        if (isInvalid) {
          field.el.classList.add('is-invalid');
          valid = false;
          if (!firstInvalid) firstInvalid = field.el;
        } else {
          field.el.classList.add('is-valid');
        }
      });

      // Same pickup/dropoff
      if (
        this.els.pickup.value &&
        this.els.dropoff.value &&
        this.els.pickup.value === this.els.dropoff.value
      ) {
        this.els.dropoff.classList.add('is-invalid');
        this.showFieldError(this.els.dropoff, 'Pickup and dropoff cannot be the same');
        valid = false;
        if (!firstInvalid) firstInvalid = this.els.dropoff;
      }

      // Date not in past
      if (this.els.date.value) {
        const selected = new Date(this.els.date.value);
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        if (selected < today) {
          this.els.date.classList.add('is-invalid');
          this.showFieldError(this.els.date, 'Date cannot be in the past');
          valid = false;
          if (!firstInvalid) firstInvalid = this.els.date;
        }
      }

      // Must have a vehicle selected
      if (!this.selectedVehicleId) {
        valid = false;
        const stepperWrap = this.els.passengers.closest('.gq-stepper');
        if (stepperWrap) stepperWrap.classList.add('is-invalid');
        if (!firstInvalid) firstInvalid = this.els.passengers;
      }

      if (firstInvalid) {
        firstInvalid.scrollIntoView({ behavior: 'smooth', block: 'center' });
        firstInvalid.focus();
      }

      return valid;
    }

    showFieldError(el, msg) {
      const existing = el.parentNode.querySelector('.gq-invalid-feedback');
      if (existing) existing.remove();
      const div = document.createElement('div');
      div.className = 'gq-invalid-feedback';
      div.textContent = msg;
      el.parentNode.appendChild(div);
    }

    validateLocations() {
      if (
        this.els.pickup.value &&
        this.els.dropoff.value &&
        this.els.pickup.value === this.els.dropoff.value
      ) {
        this.els.dropoff.classList.add('is-invalid');
        this.showFieldError(this.els.dropoff, 'Pickup and dropoff cannot be the same');
      } else {
        this.els.dropoff.classList.remove('is-invalid');
        const err = this.els.dropoff.parentNode.querySelector('.gq-invalid-feedback');
        if (err) err.remove();
      }
    }

    isValidEmail(email) {
      return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    }

    // ── Submission ────────────────────────────────────
    async submit() {
      this.setLoading(true);

      const tripType = this.els.tripOneway.checked ? '1' : '2';
      const rate = this.getRate(this.selectedVehicleId);

      const payload = {
        first_name: this.els.firstName.value.trim(),
        last_name: this.els.lastName.value.trim(),
        email: this.els.email.value.trim(),
        phone: this.els.phone.value.trim(),
        pickup_date: this.els.date.value || '',
        vehicle_id: this.selectedVehicleId,
        pickup_location: this.els.pickup.options[this.els.pickup.selectedIndex].text,
        dropoff_location: this.els.dropoff.options[this.els.dropoff.selectedIndex].text,
        trip_type: tripType,
        estimated_price: rate ? (tripType === '1' ? rate.oneway : rate.round) : null,
      };

      // Capture UTM cookies
      const utmFields = ['gclid', 'fbclid', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'];
      utmFields.forEach((field) => {
        const val = this.getCookie(field);
        if (val) payload[field] = val;
      });

      try {
        const response = await fetch(this.endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });

        const data = await response.json();

        if (data.success) {
          if (rate) {
            this.showResults(rate, tripType);
          } else {
            this.showInvalidResult();
          }

          // Set Advanced Matching from the entered contact details. Pass
          // PLAINTEXT — the pixel hashes with SHA-256 client-side (never
          // pre-hash here). Improves match quality and clears the "Set up
          // manual advanced matching" diagnostic in Events Manager.
          if (typeof fbq === 'function') {
            try {
              fbq('init', '1261740178962298', {
                em: payload.email || '',
                ph: (payload.phone || '').replace(/\D/g, ''),
                fn: payload.first_name || '',
                ln: payload.last_name || '',
              });
            } catch (e) { /* silent */ }
          }

          // Fire Meta pixel if available. Pass the server's event_id as the
          // eventID so this browser Lead and the server-side CAPI Lead dedupe
          // to one event in Meta (instead of double-counting every quote).
          if (typeof fbq === 'function') {
            try {
              if (data.event_id) {
                fbq('track', 'Lead', {}, { eventID: data.event_id });
              } else {
                fbq('track', 'Lead');
              }
            } catch (e) { /* silent */ }
          }

          // Fire gtag conversion if available
          if (typeof gtag === 'function') {
            try { gtag('event', 'generate_lead', { currency: 'USD', value: payload.estimated_price || 0 }); } catch (e) { /* silent */ }
          }
        } else {
          this.showInvalidResult();
        }
      } catch (error) {
        console.error('Quote submission error:', error);
        this.showInvalidResult();
      } finally {
        this.setLoading(false);
      }
    }

    setLoading(loading) {
      const btn = this.els.submitBtn;
      if (loading) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Getting Your Quote...';
      } else {
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-shine"></span><i class="bi bi-arrow-right-circle-fill me-2"></i>Get My Personalized Quote';
      }
    }

    showResults(rate, tripType) {
      const price = tripType === '1' ? rate.oneway : rate.round;
      const route = `${rate.origin} → ${rate.destination}`;

      this.els.resultPrice.textContent = `$${price}`;
      this.els.resultRoute.textContent = route;

      // Build customer params for booking URL
      const params = new URLSearchParams({
        first_name: this.els.firstName.value.trim(),
        last_name: this.els.lastName.value.trim(),
        email: this.els.email.value.trim(),
        phone: this.els.phone.value.trim(),
      }).toString();

      const onewayUrl = `${rate.reserve_url}?round=1&${params}`;
      const roundtripUrl = `${rate.reserve_url}?round=2&${params}`;

      // Calculate savings
      const savings = rate.oneway * 2 - rate.round;
      let rtLabel = 'Book Round-trip';
      if (savings > 0) rtLabel += ` & Save $${savings}`;

      this.els.resultActions.innerHTML = `
        <a href="${onewayUrl}" class="gq-book-btn">
          <i class="bi bi-arrow-right"></i> Book One-way Now
        </a>
        <a href="${roundtripUrl}" class="gq-book-btn">
          <i class="bi bi-arrow-repeat"></i> ${rtLabel}
        </a>
      `;

      // Show results, hide form card
      this.els.results.classList.add('visible');
      this.els.invalidResult.classList.remove('visible');
      this.els.results.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    showInvalidResult() {
      this.els.invalidResult.classList.add('visible');
      this.els.results.classList.remove('visible');
      this.els.invalidResult.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    getCookie(name) {
      const cookies = document.cookie.split(';');
      for (const cookie of cookies) {
        const [k, v] = cookie.trim().split('=');
        if (k === name) return decodeURIComponent(v);
      }
      return null;
    }
  }

  // Initialize on DOM ready
  document.addEventListener('DOMContentLoaded', () => new GuestQuoteForm());
})();
