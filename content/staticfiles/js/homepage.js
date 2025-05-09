document.addEventListener('DOMContentLoaded', function () {
  // Get DOM elements
  const rateData = JSON.parse(document.getElementById('rate-data').textContent);
  const vehicleSel = document.getElementById('vehicle');
  const routeSel = document.getElementById('route');
  const quoteBtn = document.getElementById('quote-btn');
  const displayEl = document.getElementById('quote-display');
  const displayContainer = document.getElementById('quote-display-container');
  const noteEl = document.getElementById('quote-note');
  const tripRadios = document.querySelectorAll('input[name="trip"]');
  const floatingBtn = document.getElementById('floating-quote-btn');
  const heroSection = document.querySelector('.hero');

  // Reset quote display
  function resetQuote() {
    displayEl.textContent = '';
    displayContainer.classList.add('d-none');
    noteEl.classList.add('d-none');
    quoteBtn.disabled = true;
    quoteBtn.innerHTML = '<i class="bi bi-calculator me-2"></i>Calculate Price';
    quoteBtn.onclick = null;
  }

  // Update quote display
  function updateQuoteDisplay() {
    const vehicleId = vehicleSel.value;
    const routeId = routeSel.value;
    const trip = document.querySelector('input[name="trip"]:checked').value;

    if (vehicleId && routeId && rateData[vehicleId] && rateData[vehicleId][routeId]) {
      const r = rateData[vehicleId][routeId];
      const price = trip === '1' ? r.oneway : r.round;
      displayEl.textContent = '$' + price;
      displayContainer.classList.remove('d-none');
      noteEl.classList.remove('d-none');
      quoteBtn.disabled = false;
      quoteBtn.innerHTML = '<i class="bi bi-check-circle me-2"></i>Reserve Now';
      quoteBtn.onclick = function (e) {
        e.preventDefault();
        window.location = r.reserve_url + '?round=' + trip;
      };
    } else {
      resetQuote();
    }
  }

  // Vehicle selection change handler
  if (vehicleSel) {
    vehicleSel.addEventListener('change', function () {
      routeSel.innerHTML = '<option value="" selected>Choose your route</option>';
      const routes = rateData[vehicleSel.value] || {};
      Object.values(routes).forEach(function (r) {
        routeSel.insertAdjacentHTML(
          'beforeend',
          '<option value="' + r.id + '">' + r.name + '</option>'
        );
      });
      routeSel.disabled = !vehicleSel.value;
      routeSel.value = '';
      resetQuote();
    });
  }

  // Route selection change handler
  if (routeSel) {
    routeSel.addEventListener('change', updateQuoteDisplay);
  }

  // Trip type radio change handler
  if (tripRadios) {
    tripRadios.forEach(function (radio) {
      radio.addEventListener('change', updateQuoteDisplay);
    });
  }

  // Prevent form submit
  const quoteForm = document.getElementById('quote-form');
  if (quoteForm) {
    quoteForm.addEventListener('submit', function (e) {
      e.preventDefault();
    });
  }

  // Initialize floating quote button
  if (floatingBtn && heroSection) {
    window.addEventListener('scroll', () => {
      const heroBottom = heroSection.offsetHeight;
      if (window.scrollY > heroBottom) {
        floatingBtn.classList.add('visible');
      } else {
        floatingBtn.classList.remove('visible');
      }
    });
  }

  // Smooth scroll to quote section
  window.scrollToQuote = function () {
    const quoteSection = document.querySelector('section[aria-labelledby="quote-heading"]');
    if (quoteSection) {
      const offset = 100; // Offset to show the heading
      const elementPosition = quoteSection.getBoundingClientRect().top;
      const offsetPosition = elementPosition + window.pageYOffset - offset;

      window.scrollTo({
        top: offsetPosition,
        behavior: 'smooth',
      });
    }
  };

  // Initialize quote display
  resetQuote();
});
