document.addEventListener('DOMContentLoaded', function () {
  console.log('Vehicle card script initialized');

  // Cache elements and constants
  const DOM_ELEMENTS = {
    vehicleCards: document.querySelectorAll('.vehicle-card-container'),
    flipButtons: document.querySelectorAll('.flip-card-btn'),
    viewRateButtons: document.querySelectorAll('.view-rates-btn'),
    rateSections: document.querySelectorAll('.rate-card'),
    mobileRateCards: document.querySelectorAll('.mobile-rate-card'),
    smoothScrollAnchors: document.querySelectorAll('a[href^="#"]'),
  };

  const isMobile = isMobileDevice();

  // Initialize all features
  initializeFeatures();

  function initializeFeatures() {
    ensureCardsAreVisible();
    setupCardFlip();
    setupSmoothScrolling();
    setupScrollAnimations();

    // Initialize new features
    initializeVehicleTabs();
    initializeRouteFilters();
    setupViewRatesNavigation();

    if (isMobile) {
      applyMobileOptimizations();
    } else {
      setupHoverEffects();
    }
  }

  // Vehicle Tab Navigation System
  function initializeVehicleTabs() {
    const ratesContainer = document.querySelector('.container.p-0');
    if (!ratesContainer) return;

    // First, ensure all rate cards are visible initially
    DOM_ELEMENTS.rateSections.forEach((card) => {
      card.style.display = 'block';
    });

    // Create tab navigation HTML
    const tabNavHTML = `
      <div class="vehicle-tabs-container mb-4">
        <nav class="vehicle-tabs" role="tablist" aria-label="Vehicle type navigation">
          ${Array.from(DOM_ELEMENTS.rateSections)
            .map((card, index) => {
              const vehicleType = card.querySelector('h2').textContent.replace(' Rates', '');
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
        
        <div class="vehicle-preview" id="vehicle-preview">
          <!-- Preview content will be inserted dynamically -->
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
      const cardTitle = card.querySelector('h2').textContent;
      const cardVehicleType = cardTitle.replace(' Rates', '').replace(/\s*-.*$/, '');

      // Use exact matching instead of includes
      const isMatch = cardVehicleType === vehicleType;

      // Ensure the card is displayed with proper opacity
      if (isMatch) {
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
    const vehicleCard = Array.from(document.querySelectorAll('.vehicle-card-front')).find(
      (card) => {
        const title = card.querySelector('.card-title').textContent;
        return title === vehicleType;
      }
    );

    if (!vehicleCard) return;

    const image = vehicleCard.querySelector('img').src;
    const title = vehicleCard.querySelector('.card-title').textContent;
    const details = vehicleCard.querySelectorAll('.vehicle-details p');

    const previewHTML = `
      <div class="preview-content">
        <div class="preview-image">
          <img src="${image}" alt="${title}" class="img-fluid" />
        </div>
        <div class="preview-details">
          <h3 class="preview-title">${title}</h3>
          <div class="preview-specs">
            ${Array.from(details)
              .map(
                (detail) => `
              <div class="spec-item">
                ${detail.innerHTML}
              </div>
            `
              )
              .join('')}
          </div>
        </div>
      </div>
    `;

    const preview = document.getElementById('vehicle-preview');
    preview.innerHTML = previewHTML;
    preview.style.opacity = '1';
  }

  // Route Filtering System
  function initializeRouteFilters() {
    const ratesContainer = document.querySelector('.vehicle-tabs-container');
    if (!ratesContainer) return;

    // Create filter HTML
    const filterHTML = `
    <div class="route-filters-container mb-4">
      <div class="filter-buttons" role="group" aria-label="Route filters">
        <button class="filter-btn active" data-filter="all">All Routes</button>
        <button class="filter-btn" data-filter="airport">Airport</button>
        <button class="filter-btn" data-filter="disney">Disney</button>
        <button class="filter-btn" data-filter="universal">Universal</button>
        <button class="filter-btn" data-filter="cruise">Cruise Port</button>
        <button class="filter-btn" data-filter="popular">Popular</button>
        <button class="filter-btn clear-filter" data-filter="clear">Clear All</button>
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
      cruise: 'Cruise Port',
      popular: 'Popular',
    };
    return filterNames[filter] || filter;
  }

  // Enhanced View Rates Navigation
  function setupViewRatesNavigation() {
    DOM_ELEMENTS.viewRateButtons.forEach((button) => {
      button.addEventListener('click', function (e) {
        e.preventDefault();

        const vehicleCard = this.closest('.vehicle-card-front');
        const vehicleType = vehicleCard.querySelector('.card-title').textContent;
        const targetId = this.getAttribute('href').substring(1);

        // Find the corresponding tab
        const tabs = document.querySelectorAll('.vehicle-tab');
        const targetTab = Array.from(tabs).find((tab) => {
          return tab.textContent === vehicleType;
        });

        // Activate the tab
        if (targetTab) {
          targetTab.click();
        }

        // Scroll to the rates section after tab change
        setTimeout(() => {
          const targetElement = document.getElementById(targetId);
          if (targetElement) {
            const offset = isMobile ? 60 : 80;
            const targetPosition =
              targetElement.getBoundingClientRect().top + window.pageYOffset - offset;

            window.scrollTo({
              top: targetPosition,
              behavior: 'smooth',
            });
          }
        }, 300);
      });
    });
  }

  // Existing functions with enhancements
  function ensureCardsAreVisible() {
    document.querySelectorAll('.vehicle-card-container').forEach((container) => {
      container.style.display = 'block';
      container.style.height = '100%';
    });
    document.querySelectorAll('.vehicle-card-front').forEach((front) => {
      front.style.display = 'flex';
      front.style.position = 'relative';
      front.style.zIndex = '1';
    });
    document.querySelectorAll('.vehicle-card-back').forEach((back) => {
      back.style.position = 'absolute';
      back.style.top = '0';
      back.style.left = '0';
      back.style.right = '0';
      back.style.bottom = '0';
    });
  }

  function setupCardFlip() {
    DOM_ELEMENTS.flipButtons.forEach((button) => {
      button.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();

        const container = this.closest('.vehicle-card-container');
        if (container) {
          // Close other flipped cards
          document.querySelectorAll('.vehicle-card-container.flipped').forEach((card) => {
            if (card !== container) {
              card.classList.remove('flipped');
            }
          });

          container.classList.toggle('flipped');
          const isFlipped = container.classList.contains('flipped');
          this.setAttribute('aria-expanded', isFlipped ? 'true' : 'false');

          if (isMobile && isFlipped) {
            document.body.classList.add('card-flipped');
          } else {
            document.body.classList.remove('card-flipped');
          }
        }
      });
    });

    if (isMobile) {
      document.addEventListener('click', function (e) {
        const flippedContainer = document.querySelector('.vehicle-card-container.flipped');
        if (
          flippedContainer &&
          !e.target.closest('.vehicle-card-back') &&
          !e.target.closest('.flip-card-btn')
        ) {
          flippedContainer.classList.remove('flipped');
          document.body.classList.remove('card-flipped');
        }
      });
    }
  }

  function setupSmoothScrolling() {
    DOM_ELEMENTS.smoothScrollAnchors.forEach((anchor) => {
      anchor.addEventListener('click', function (e) {
        e.preventDefault();
        const target = document.querySelector(this.getAttribute('href'));
        if (target) {
          document.querySelectorAll('.vehicle-card-container.flipped').forEach((card) => {
            card.classList.remove('flipped');
          });
          const offset = isMobile ? 60 : 80;
          const targetPosition = target.getBoundingClientRect().top + window.pageYOffset - offset;
          window.scrollTo({
            top: targetPosition,
            behavior: 'smooth',
          });
        }
      });
    });
  }

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
    setTimeout(animateElements, 100);
    window.addEventListener('scroll', animateElements);
  }

  function applyMobileOptimizations() {
    document.querySelectorAll('.flip-indicator').forEach((indicator) => {
      indicator.style.opacity = '1';
      indicator.style.transform = 'rotate(180deg)';
    });
    document.querySelectorAll('.flip-card-btn, .view-rates-btn').forEach((btn) => {
      btn.style.minHeight = '36px';
      btn.addEventListener('touchstart', function () {
        this.style.transform = 'scale(0.97)';
      });
      btn.addEventListener('touchend', function () {
        this.style.transform = 'scale(1)';
        setTimeout(() => {
          this.blur();
        }, 300);
      });
    });
  }

  function setupHoverEffects() {
    document.querySelectorAll('.vehicle-card').forEach((card) => {
      const front = card.querySelector('.vehicle-card-front');
      if (front) {
        front.addEventListener('mouseenter', function () {
          this.style.boxShadow = '0 0.5rem 1rem rgba(0,0,0,0.1)';
          const indicator = this.querySelector('.flip-indicator');
          if (indicator) {
            indicator.style.opacity = '1';
            indicator.style.transform = 'rotate(180deg)';
          }
        });
        front.addEventListener('mouseleave', function () {
          this.style.boxShadow = '0 0.125rem 0.25rem rgba(0,0,0,0.075)';
          const indicator = this.querySelector('.flip-indicator');
          if (indicator) {
            indicator.style.opacity = '0';
            indicator.style.transform = 'rotate(0deg)';
          }
        });
      }
    });
  }

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
