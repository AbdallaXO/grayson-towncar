document.addEventListener('DOMContentLoaded', function () {
    // Interactive area selection
    const areaButtons = document.querySelectorAll('#areaButtons button');
    const areaDescriptions = document.querySelectorAll('.area-description');

    areaButtons.forEach(button => {
        button.addEventListener('click', function () {
            // Remove active class from all buttons and add to clicked button
            areaButtons.forEach(btn => btn.classList.remove('active'));
            this.classList.add('active');

            // Hide all descriptions and show the selected one
            const areaToShow = this.getAttribute('data-area');
            areaDescriptions.forEach(desc => desc.classList.remove('active'));
            document.getElementById(`${areaToShow}-description`).classList.add('active');
        });
    });

    // Add animation to service cards
    const serviceCards = document.querySelectorAll('.service-card');

    serviceCards.forEach(card => {
        card.addEventListener('mouseenter', function () {
            this.classList.add('card-hover');
        });

        card.addEventListener('mouseleave', function () {
            this.classList.remove('card-hover');
        });
    });

    // Smooth scrolling for anchor links
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            const targetId = this.getAttribute('href');
            if (targetId !== '#' && document.querySelector(targetId)) {
                e.preventDefault();
                document.querySelector(targetId).scrollIntoView({
                    behavior: 'smooth',
                    block: 'start'
                });
            }
        });
    });
});