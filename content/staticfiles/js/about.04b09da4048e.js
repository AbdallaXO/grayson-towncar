document.addEventListener('DOMContentLoaded', function() {
  // Scroll reveal animation
  const scrollRevealElements = document.querySelectorAll('.scroll-reveal');

  const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
          if (entry.isIntersecting) {
              entry.target.classList.add('revealed');
              entry.target.style.opacity = "1";
              entry.target.style.transform = "translateY(0)";
          }
      });
  }, {
      threshold: 0.1,
      rootMargin: '0px 0px -50px 0px'
  });

  scrollRevealElements.forEach(element => {
      element.style.opacity = "0";
      element.style.transform = "translateY(30px)";
      observer.observe(element);
  });

  // Smooth scroll for anchor links
  document.querySelectorAll('a[href^="#"]').forEach(anchor => {
      anchor.addEventListener('click', function(e) {
          e.preventDefault();

          const targetId = this.getAttribute('href');
          const targetElement = document.querySelector(targetId);

          if (targetElement) {
              window.scrollTo({
                  top: targetElement.offsetTop - 100,
                  behavior: 'smooth'
              });
          }
      });
  });

  // Cards hover effect
  const aboutCards = document.querySelectorAll('.about-card');
  aboutCards.forEach(card => {
      card.addEventListener('mouseenter', function() {
          this.style.transform = 'translateY(-5px)';
          this.style.boxShadow = '0 10px 20px rgba(0, 0, 0, 0.1)';
      });

      card.addEventListener('mouseleave', function() {
          this.style.transform = 'translateY(0)';
          this.style.boxShadow = '0 0.125rem 0.25rem rgba(0, 0, 0, 0.075)';
      });
  });
});