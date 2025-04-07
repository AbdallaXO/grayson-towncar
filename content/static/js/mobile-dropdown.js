document.addEventListener('DOMContentLoaded', function () {
    // Function to handle responsive behavior
    function handleResponsiveDisplay() {
        const isMobile = window.innerWidth < 768;
        const collapseElements = document.querySelectorAll('.collapse');

        collapseElements.forEach(function (element) {
            if (!isMobile) {
                element.classList.add('show');
            } else if (!element.classList.contains('show-initial')) {
                element.classList.remove('show');
            }
        });
    }

    // Initial call
    handleResponsiveDisplay();

    // Add resize listener
    window.addEventListener('resize', handleResponsiveDisplay);
});