document.addEventListener('DOMContentLoaded', () => {
    // Add copy link functionality
    const copyLinkBtn = document.getElementById('copy-link');
    if (copyLinkBtn) {
        copyLinkBtn.addEventListener('click', () => {
            navigator.clipboard.writeText(window.location.href).then(() => {
                copyLinkBtn.textContent = 'Link Copied!';
                setTimeout(() => {
                    copyLinkBtn.textContent = 'Copy Link';
                }, 2000);
            });
        });
    }

    // Lazy load images
    const images = document.querySelectorAll('img');
    const imageObserver = new IntersectionObserver((entries, observer) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const img = entry.target;
                img.classList.add('loaded');
                observer.unobserve(img);
            }
        });
    }, {
        rootMargin: '50px'
    });

    images.forEach(img => {
        imageObserver.observe(img);
    });
});