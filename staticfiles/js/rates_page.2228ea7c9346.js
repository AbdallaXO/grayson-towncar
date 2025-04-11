document.addEventListener('DOMContentLoaded', function () {
  console.log('Vehicle card script initialized');

  ensureCardsAreVisible();
  setupCardFlip();
  setupSmoothScrolling();
  setupScrollAnimations();

  // Cache mobile flag for initial setup (note: not reactive to resize)
  const isMobile = isMobileDevice();
  if (isMobile) {
    applyMobileOptimizations();
  } else {
    setupHoverEffects();
  }

  /** Ensure card elements have correct display and position styles */
  function ensureCardsAreVisible() {
    document.querySelectorAll('.vehicle-card-container').forEach(container => {
      container.style.display = 'block';
      container.style.height = '100%';
    });
    document.querySelectorAll('.vehicle-card-front').forEach(front => {
      front.style.display = 'flex';
      front.style.position = 'relative';
      front.style.zIndex = '1';
    });
    document.querySelectorAll('.vehicle-card-back').forEach(back => {
      back.style.position = 'absolute';
      back.style.top = '0';
      back.style.left = '0';
      back.style.right = '0';
      back.style.bottom = '0';
    });
  }

  /** Toggle card flip state on button click */
  function setupCardFlip() {
    document.querySelectorAll('.flip-card-btn').forEach(button => {
      button.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();

        const container = this.closest('.vehicle-card-container');
        if (container) {
          container.classList.toggle('flipped');
          const isFlipped = container.classList.contains('flipped');
          this.setAttribute('aria-expanded', isFlipped ? 'true' : 'false');

          if (isMobileDevice() && isFlipped) {
            document.body.classList.add('card-flipped');
          } else {
            document.body.classList.remove('card-flipped');
          }
        }
      });
    });

    // For mobile: close flipped card when clicking outside
    if (isMobileDevice()) {
      document.addEventListener('click', function (e) {
        const flippedContainer = document.querySelector('.vehicle-card-container.flipped');
        if (flippedContainer && !e.target.closest('.vehicle-card-back') && !e.target.closest('.flip-card-btn')) {
          flippedContainer.classList.remove('flipped');
          document.body.classList.remove('card-flipped');
        }
      });
    }
  }

  /** Smooth scroll to anchors */
  function setupSmoothScrolling() {
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
      anchor.addEventListener('click', function (e) {
        e.preventDefault();
        const target = document.querySelector(this.getAttribute('href'));
        if (target) {
          // Close any flipped cards before scrolling
          document.querySelectorAll('.vehicle-card-container.flipped').forEach(card => {
            card.classList.remove('flipped');
          });
          const offset = isMobileDevice() ? 60 : 80;
          const targetPosition = target.getBoundingClientRect().top + window.pageYOffset - offset;
          window.scrollTo({
            top: targetPosition,
            behavior: 'smooth'
          });
        }
      });
    });
  }

  /** Animate elements upon scrolling into view */
  function setupScrollAnimations() {
    function animateElements() {
      const elements = document.querySelectorAll('.fade-in, .rate-card, .inclusion-item');
      elements.forEach(element => {
        const elementPosition = element.getBoundingClientRect().top;
        const triggerPosition = window.innerHeight / 1.2;
        if (elementPosition < triggerPosition) {
          element.style.opacity = '1';
          if (element.classList.contains('inclusion-item')) {
            const index = Array.from(element.parentNode.children).indexOf(element);
            element.style.animationDelay = `${index * 100}ms`;
            element.style.animation = 'fadeInUp 0.5s ease forwards';
          }
        }
      });
    }
    setTimeout(animateElements, 100);
    window.addEventListener('scroll', animateElements);
  }

  /** Enhance touch targets and visuals for mobile devices */
  function applyMobileOptimizations() {
    document.querySelectorAll('.flip-indicator').forEach(indicator => {
      indicator.style.opacity = '1';
      indicator.style.transform = 'rotate(180deg)';
    });
    document.querySelectorAll('.flip-card-btn, .view-rates-btn, .booking-btn').forEach(btn => {
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

  /** Desktop-specific hover effects */
  function setupHoverEffects() {
    document.querySelectorAll('.vehicle-card').forEach(card => {
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

  /** Mobile device detection helper */
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
