/* Rates page — click a vehicle (fleet card or toolbar link) to show only
   that vehicle's rates; destination filter + search apply to the list.
   Without JS all vehicles render stacked and the links work as anchors. */
document.addEventListener('DOMContentLoaded', function () {
  const ratesSection = document.querySelector('.rp-rates');
  const cards = Array.from(document.querySelectorAll('.rp-card'));
  const fleetCards = Array.from(document.querySelectorAll('.rp-fleet-card'));
  const jumpLinks = Array.from(document.querySelectorAll('.rp-jump a'));
  const chips = Array.from(document.querySelectorAll('.rp-chip'));
  const searchInput = document.getElementById('rp-search-input');
  const toolbar = document.getElementById('rates-toolbar');
  if (!ratesSection || !cards.length) return;

  let activeFilter = 'all';
  let query = '';

  // Panel switching only applies when JS is running
  ratesSection.classList.add('rp-js');

  // Keep the toolbar just below the site navbar
  const navbar = document.querySelector('.premium-navbar');
  function setOffsets() {
    const navHeight = navbar ? navbar.offsetHeight : 0;
    document.documentElement.style.setProperty('--rp-stick-top', navHeight + 'px');
    const toolbarHeight = toolbar ? toolbar.offsetHeight : 0;
    document.documentElement.style.setProperty('--rp-scroll-offset', navHeight + toolbarHeight + 14 + 'px');
  }
  setOffsets();
  window.addEventListener('resize', setOffsets);

  function applyFilters() {
    cards.forEach(function (card) {
      const rows = card.querySelectorAll('.rp-row');
      let visible = 0;
      rows.forEach(function (row) {
        const matchesCat = activeFilter === 'all' || (row.dataset.cats || '').split(' ').indexOf(activeFilter) !== -1;
        const matchesQuery = !query || (row.dataset.route || '').indexOf(query) !== -1;
        const show = matchesCat && matchesQuery;
        row.hidden = !show;
        if (show) visible++;
      });

      const count = card.querySelector('[data-count]');
      if (count) {
        count.textContent = visible === rows.length
          ? '(' + rows.length + ')'
          : '(' + visible + ' of ' + rows.length + ')';
      }

      const empty = card.querySelector('[data-empty]');
      if (empty) empty.hidden = visible > 0;
    });
  }

  function targetId(link) {
    return (link.getAttribute('href') || '').split('#')[1] || link.dataset.jump;
  }

  function selectVehicle(id, scrollToPanel, updateHash) {
    const panel = document.getElementById(id);
    if (!panel) return;

    cards.forEach(function (card) {
      card.classList.toggle('is-active', card === panel);
    });
    fleetCards.forEach(function (card) {
      const isActive = targetId(card) === id;
      card.classList.toggle('is-active', isActive);
      if (isActive) {
        card.setAttribute('aria-current', 'true');
      } else {
        card.removeAttribute('aria-current');
      }
      const go = card.querySelector('[data-go]');
      if (go) {
        go.innerHTML = isActive
          ? 'Viewing <i class="bi bi-check2" aria-hidden="true"></i>'
          : 'View rates <i class="bi bi-arrow-down" aria-hidden="true"></i>';
      }
    });
    jumpLinks.forEach(function (link) {
      link.classList.toggle('is-active', targetId(link) === id);
    });

    if (updateHash && history.replaceState) history.replaceState(null, '', '#' + id);

    // Bring the rates into view if they aren't already
    if (scrollToPanel && panel.getBoundingClientRect().top < 0) {
      panel.scrollIntoView({ behavior: 'smooth' });
    }
  }

  fleetCards.concat(jumpLinks).forEach(function (link) {
    link.addEventListener('click', function (event) {
      event.preventDefault();
      selectVehicle(targetId(link), true, true);
    });
  });

  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      chips.forEach(function (other) { other.classList.toggle('is-active', other === chip); });
      activeFilter = chip.dataset.filter;
      applyFilters();
    });
  });

  if (searchInput) {
    searchInput.addEventListener('input', function () {
      query = searchInput.value.trim().toLowerCase();
      applyFilters();
    });
  }

  // Deep link: /rates-booking/#rates-van-14-pax opens that vehicle;
  // otherwise sync the labels for the server-rendered default (SUV)
  const hash = window.location.hash.replace('#', '');
  const initial = (hash.indexOf('rates-') === 0 && document.getElementById(hash))
    ? hash
    : (cards.find(function (card) { return card.classList.contains('is-active'); }) || cards[0]).id;
  selectVehicle(initial, false, false);

  applyFilters();
});
