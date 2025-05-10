document.addEventListener('DOMContentLoaded', function () {
  console.log('Rates page script initialized');

  // Cache elements
  const DOM_ELEMENTS = {
    rateSections: document.querySelectorAll('.rate-card'),
    mobileRateCards: document.querySelectorAll('.mobile-rate-card'),
    smoothScrollAnchors: document.querySelectorAll('a[href^="#"]'),
  };

  const isMobile = isMobileDevice();

  // Initialize all features
  initializeFeatures();

  function initializeFeatures() {
    setupScrollAnimations();
    initializeVehicleTabs();
    initializeRouteFilters();
  }

  // Vehicle Tab Navigation System
  function initializeVehicleTabs() {
    const ratesContainer = document.querySelector('.container.p-0');
    if (!ratesContainer || !DOM_ELEMENTS.rateSections.length) return;

    // Create tab navigation HTML
    const tabNavHTML = `
      <div class="vehicle-tabs-container mb-4">
        <nav class="vehicle-tabs" role="tablist" aria-label="Vehicle type navigation">
          ${Array.from(DOM_ELEMENTS.rateSections)
        .map((card, index) => {
          const vehicleType = card.dataset.vehicleType || card.querySelector('h2').textContent.replace(' Rates', '');
          const activeClass = index === 0 ? 'active' : '';
          return `
              <button class="vehicle-tab ${activeClass}" 
                      role="tab" 
                      aria-selected="${index === 0 ? 'true' : 'false'}"
                      aria-controls="${vehicleType.toLowerCase().replace(' ', '-')}-content"
                      data-vehicle="${vehicleType.toLowerCase().replace(' ', '-')}"
                      data-vehicle-display="${vehicleType}">
                ${vehicleType}
              </button>
            `;
        })
        .join('')}
        </nav>
        
        <div class="vehicle-preview mb-4 bg-white shadow-sm fade-in" id="vehicle-preview" style="display: none;">
          <div class="card border-0">
            <div class="row g-0">
              <div class="col-md-6 p-4">
                <div class="preview-image text-center">
                  <img src="" alt="" class="img-fluid" style="max-height: 200px; width: auto; border-radius: 8px;" />
                </div>
              </div>
              <div class="col-md-6 p-4">
                <div class="preview-details">
                  <h3 class="preview-title h4 fw-bold mb-4"></h3>
                  <div class="preview-specs d-flex flex-column gap-3">
                    <div class="spec-item d-flex align-items-center">
                      <img src="/static/images/passengers.webp" class="vehicle-icon me-3" alt="Passengers icon" aria-hidden="true" style="width: 1.5em; height: 1.5em;" />
                      <span class="preview-capacity fs-5"></span>
                    </div>
                    <div class="spec-item d-flex align-items-center">
                      <img src="/static/images/luggage.webp" class="vehicle-icon me-3" alt="Luggage icon" aria-hidden="true" style="width: 1.5em; height: 1.5em;" />
                      <span class="preview-luggage fs-5"></span>
                    </div>
                    <div class="spec-item d-flex align-items-center">
                      <img src="/static/images/carseat.webp" class="vehicle-icon me-3" alt="Car Seats icon" aria-hidden="true" style="width: 1.5em; height: 1.5em;" />
                      <span class="preview-carseats fs-5"></span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;

    // Insert tabs and preview container
    ratesContainer.insertAdjacentHTML('afterbegin', tabNavHTML);

    // Setup tab functionality
    const tabs = document.querySelectorAll('.vehicle-tab');
    const preview = document.getElementById('vehicle-preview');

    tabs.forEach((tab, index) => {
      tab.addEventListener('click', function () {
        const vehicleType = this.dataset.vehicle;
        const vehicleDisplay = this.dataset.vehicleDisplay;

        // Update active states
        tabs.forEach((t) => {
          t.classList.remove('active');
          t.setAttribute('aria-selected', 'false');
        });
        this.classList.add('active');
        this.setAttribute('aria-selected', 'true');

        // Show corresponding rate card
        showVehicleRates(vehicleDisplay);

        // Update preview
        updateVehiclePreview(vehicleDisplay);
      });
    });

    // Get the first vehicle type and ensure it's displayed
    const firstTab = tabs[0];
    const firstVehicleDisplay = firstTab?.dataset.vehicleDisplay || 'Town Car';

    // Initial setup - show first vehicle by default
    showVehicleRates(firstVehicleDisplay);
    updateVehiclePreview(firstVehicleDisplay);
  }

  function showVehicleRates(vehicleType) {
    DOM_ELEMENTS.rateSections.forEach((card) => {
      const cardVehicleType = card.dataset.vehicleType || card.querySelector('h2').textContent.replace(' Rates', '');

      if (cardVehicleType === vehicleType) {
        card.style.display = 'block';
        card.style.opacity = '1';
        card.classList.add('fade-in');
        card.setAttribute('aria-hidden', 'false');
      } else {
        card.style.display = 'none';
        card.setAttribute('aria-hidden', 'true');
      }
    });
  }

  function updateVehiclePreview(vehicleType) {
    // Find the rate card for this vehicle type
    const rateCard = Array.from(DOM_ELEMENTS.rateSections).find((card) => {
      const cardVehicleType = card.dataset.vehicleType || card.querySelector('h2').textContent.replace(' Rates', '');
      return cardVehicleType === vehicleType;
    });

    if (!rateCard) return;

    const preview = document.getElementById('vehicle-preview');
    if (!preview) return;

    // Show the preview container
    preview.style.display = 'block';
    preview.classList.add('fade-in');

    // Update preview content using data attributes from the rate card
    const previewImg = preview.querySelector('.preview-image img');
    const previewTitle = preview.querySelector('.preview-title');
    const previewCapacity = preview.querySelector('.preview-capacity');
    const previewLuggage = preview.querySelector('.preview-luggage');
    const previewCarseats = preview.querySelector('.preview-carseats');

    if (previewImg && previewTitle && previewCapacity && previewLuggage && previewCarseats) {
      // Get data from rate card attributes
      previewImg.src = rateCard.dataset.vehicleImage || '';
      previewImg.alt = `${vehicleType} - Orlando Luxury Transportation`;
      previewTitle.textContent = vehicleType;
      previewCapacity.textContent = `${rateCard.dataset.capacity || '3'} Passengers`;
      previewLuggage.textContent = `${rateCard.dataset.luggage || '3'} Suitcases`;
      previewCarseats.textContent = rateCard.dataset.carseats || 'Car Seats Available';

      // Add transition effect
      preview.style.opacity = '0';
      requestAnimationFrame(() => {
        preview.style.transition = 'opacity 0.3s ease-in-out';
        preview.style.opacity = '1';
      });
    }
  }

  // Route Filtering System
  function initializeRouteFilters() {
    const ratesContainer = document.querySelector('.vehicle-tabs-container');
    if (!ratesContainer) return;

    // Create filter HTML
    const filterHTML = `
    <div class="route-filters-container mb-4">
      <div class="filter-buttons" role="group" aria-label="Route filters">
        <button class="fw-bold fs-6 filter-btn active" data-filter="all">All Routes</button>
        <button class="fw-bold fs-6 filter-btn" data-filter="airport">Airport</button>
        <button class="fw-bold fs-6 filter-btn" data-filter="disney">Disney</button>
        <button class="fw-bold fs-6 filter-btn" data-filter="universal">Universal</button>
        <button class="fw-bold fs-6 filter-btn" data-filter="cruise">Port Canaveral</button>
        <button class="fw-bold fs-6 filter-btn" data-filter="popular">Popular</button>
        <button class="fw-bold fs-6 filter-btn clear-filter" data-filter="clear">Clear All</button>
      </div>
    </div>
  `;

    ratesContainer.insertAdjacentHTML('afterend', filterHTML);

    // Add data attributes to routes
    categorizeRoutes();

    // Setup filter functionality
    const filterButtons = document.querySelectorAll('.filter-btn');

    filterButtons.forEach((button) => {
      button.addEventListener('click', function () {
        const filter = this.dataset.filter;

        if (filter === 'clear' || filter === 'all') {
          // Clear all filters or show all
          filterButtons.forEach((btn) => btn.classList.remove('active'));
          filterButtons[0].classList.add('active'); // Activate "All Routes"
          showAllRoutes();
          updateRateHeaders('all');
        } else {
          // Update active state
          filterButtons.forEach((btn) => btn.classList.remove('active'));
          this.classList.add('active');

          // Apply filter
          filterRoutes(filter);
          updateRateHeaders(filter);
        }
      });
    });
  }

  function categorizeRoutes() {
    // Desktop routes
    const routeRows = document.querySelectorAll('.rate-row');
    routeRows.forEach((row) => {
      const routeText = row.querySelector('td:first-child span').textContent.toLowerCase();
      row.setAttribute('data-route-category', getCategoryFromRoute(routeText));
    });

    // Mobile routes
    const mobileCards = document.querySelectorAll('.mobile-rate-card');
    mobileCards.forEach((card) => {
      const routeText = card.querySelector('h3').textContent.toLowerCase();
      card.setAttribute('data-route-category', getCategoryFromRoute(routeText));
    });
  }

  function getCategoryFromRoute(routeText) {
    const categories = [];

    if (routeText.includes('airport') || routeText.includes('mco')) {
      categories.push('airport');
    }
    if (
      routeText.includes('disney') ||
      routeText.includes('magic kingdom') ||
      routeText.includes('epcot') ||
      routeText.includes('hollywood') ||
      routeText.includes('animal kingdom')
    ) {
      categories.push('disney');
    }
    if (routeText.includes('universal') || routeText.includes('islands')) {
      categories.push('universal');
    }
    if (routeText.includes('port canaveral') || routeText.includes('cruise')) {
      categories.push('cruise');
    }
    if (
      routeText.includes('disney') ||
      routeText.includes('universal') ||
      routeText.includes('airport')
    ) {
      categories.push('popular');
    }

    return categories.join(' ') || 'other';
  }

  function filterRoutes(filter) {
    // Desktop
    const rows = document.querySelectorAll('.rate-row');
    rows.forEach((row) => {
      const category = row.getAttribute('data-route-category');
      const shouldShow = filter === 'all' || category.includes(filter);
      row.style.display = shouldShow ? '' : 'none';
    });

    // Mobile
    const mobileCards = document.querySelectorAll('.mobile-rate-card');
    mobileCards.forEach((card) => {
      const category = card.getAttribute('data-route-category');
      const shouldShow = filter === 'all' || category.includes(filter);
      card.style.display = shouldShow ? '' : 'none';
    });
  }

  function showAllRoutes() {
    // Show all route rows
    document.querySelectorAll('.rate-row').forEach((row) => {
      row.style.display = '';
    });

    // Show all mobile cards
    document.querySelectorAll('.mobile-rate-card').forEach((card) => {
      card.style.display = '';
    });
  }

  function updateRateHeaders(filter) {
    const rateCards = document.querySelectorAll('.rate-card');

    rateCards.forEach((card) => {
      const header = card.querySelector('.card-header h2');
      if (header) {
        const vehicleType = header.textContent.replace(' Rates', '').replace(/\s*-.*$/, '');

        if (filter === 'all') {
          header.innerHTML = `${vehicleType} Rates`;
        } else {
          const filterName = getFilterDisplayName(filter);
          header.innerHTML = `${vehicleType} Rates <span class="filter-label">- ${filterName} Routes</span>`;
        }
      }
    });
  }

  function getFilterDisplayName(filter) {
    const filterNames = {
      airport: 'Airport',
      disney: 'Disney',
      universal: 'Universal',
      cruise: 'Port Canaveral',
      popular: 'Popular',
    };
    return filterNames[filter] || filter;
  }

  // Scroll animations
  function setupScrollAnimations() {
    function animateElements() {
      const elements = document.querySelectorAll('.fade-in, .rate-card, .inclusion-item');
      elements.forEach((element) => {
        const elementPosition = element.getBoundingClientRect().top;
        const triggerPosition = window.innerHeight / 1.2;
        if (elementPosition < triggerPosition) {
          element.style.opacity = '1';
        }
      });
    }

    // Initial animation
    setTimeout(animateElements, 100);

    // Animate on scroll
    window.addEventListener('scroll', animateElements);
  }

  // Mobile device detection
  function isMobileDevice() {
    return (
      window.innerWidth < 768 ||
      navigator.maxTouchPoints > 0 ||
      navigator.msMaxTouchPoints > 0 ||
      'ontouchstart' in window ||
      navigator.userAgent.toLowerCase().includes('mobile')
    );
  }
});