/* Rates page ("fare folio") — pick a vehicle in the sticky tab strip to show
   its showcase stage + tariff; destination chips and search filter the routes
   (state shared across vehicles). Without JS all vehicles render stacked, the
   tabs work as plain anchors and the filter tools stay hidden. */
document.addEventListener('DOMContentLoaded', function () {
  const page = document.getElementById('rl-page');
  const sections = Array.from(document.querySelectorAll('.rl-vehicle'));
  const tabs = Array.from(document.querySelectorAll('.rl-tab'));
  const tabsBar = document.getElementById('rl-tabs');
  if (!page || !sections.length) return;

  let activeFilter = 'all';
  let query = '';

  page.classList.add('rl-js');

  // Filter tools only make sense with JS running
  document.querySelectorAll('[data-tools]').forEach(function (tools) {
    tools.hidden = false;
  });

  // Keep the tab strip just below the site navbar; anchor jumps land under both
  const navbar = document.querySelector('.premium-navbar');
  function setOffsets() {
    const navHeight = navbar ? navbar.offsetHeight : 0;
    document.documentElement.style.setProperty('--rl-stick-top', navHeight + 'px');
    // The tab bar only stacks on top of content while it is sticky (desktop)
    const tabsSticky = tabsBar && getComputedStyle(tabsBar).position === 'sticky';
    const tabsHeight = tabsSticky ? tabsBar.offsetHeight : 0;
    document.documentElement.style.setProperty('--rl-scroll-offset', navHeight + tabsHeight + 16 + 'px');
  }
  setOffsets();
  window.addEventListener('resize', setOffsets);

  // Soft shadow once the strip actually sticks
  const sentinel = document.querySelector('[data-tabs-sentinel]');
  if (tabsBar && sentinel && 'IntersectionObserver' in window) {
    new IntersectionObserver(function (entries) {
      tabsBar.classList.toggle('is-stuck', !entries[0].isIntersecting);
    }, { rootMargin: '-80px 0px 0px 0px' }).observe(sentinel);
  }

  function applyFilters() {
    sections.forEach(function (section) {
      const rows = section.querySelectorAll('.rl-row');
      let visible = 0;
      rows.forEach(function (row) {
        const matchesCat = activeFilter === 'all' || (row.dataset.cats || '').split(' ').indexOf(activeFilter) !== -1;
        const matchesQuery = !query || (row.dataset.route || '').indexOf(query) !== -1;
        const show = matchesCat && matchesQuery;
        row.hidden = !show;
        if (show) visible++;
      });

      const count = section.querySelector('[data-count]');
      if (count) {
        count.textContent = visible === rows.length
          ? rows.length + ' routes'
          : visible + ' of ' + rows.length + ' routes';
      }

      const empty = section.querySelector('[data-empty]');
      if (empty) empty.hidden = visible > 0;
    });
  }

  function selectVehicle(id, scrollToPanel, updateHash) {
    const panel = document.getElementById(id);
    if (!panel) return;

    sections.forEach(function (section) {
      const isActive = section === panel;
      section.classList.toggle('is-active', isActive);
      section.classList.toggle('is-entering', isActive);
    });
    tabs.forEach(function (tab) {
      const isActive = tab.dataset.target === id;
      tab.classList.toggle('is-active', isActive);
      if (isActive) {
        tab.setAttribute('aria-current', 'true');
        if (tab.scrollIntoView) {
          tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        }
      } else {
        tab.removeAttribute('aria-current');
      }
    });

    if (updateHash && history.replaceState) history.replaceState(null, '', '#' + id);

    // Bring the stage into view if the reader has scrolled past it
    if (scrollToPanel && panel.getBoundingClientRect().top < 0) {
      panel.scrollIntoView({ behavior: 'smooth' });
    }
  }

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function (event) {
      event.preventDefault();
      selectVehicle(tab.dataset.target, true, true);
    });
  });

  // Chips + search are duplicated per vehicle section; state is shared so a
  // filter chosen on one vehicle carries over when comparing another.
  const allChips = Array.from(document.querySelectorAll('.rl-chip'));
  const allSearches = Array.from(document.querySelectorAll('[data-search]'));

  allChips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      activeFilter = chip.dataset.filter;
      allChips.forEach(function (other) {
        other.classList.toggle('is-active', other.dataset.filter === activeFilter);
      });
      applyFilters();
    });
  });

  allSearches.forEach(function (input) {
    input.addEventListener('input', function () {
      query = input.value.trim().toLowerCase();
      allSearches.forEach(function (other) {
        if (other !== input && other.value !== input.value) other.value = input.value;
      });
      applyFilters();
    });
  });

  // Deep link the route filter: /rates-booking/?filter=disney (or ...#disney) opens
  // with the matching destination chip pre-selected + scrolled into view, so a visitor
  // who already picked "Disney" on the home page doesn't re-choose it here.
  (function () {
    const known = allChips.map(function (c) { return c.dataset.filter; });
    let wanted = (new URLSearchParams(window.location.search).get('filter') || '').toLowerCase();
    if (!wanted) {
      const h = window.location.hash.replace('#', '').toLowerCase();
      if (known.indexOf(h) !== -1) wanted = h;  // allow #disney etc, not #rates-*
    }
    if (wanted && wanted !== 'all' && known.indexOf(wanted) !== -1) {
      activeFilter = wanted;
      allChips.forEach(function (chip) {
        chip.classList.toggle('is-active', chip.dataset.filter === wanted);
      });
      const tools = document.querySelector('[data-tools]');
      if (tools) {
        requestAnimationFrame(function () {
          tools.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      }
    }
  })();

  // Deep link: /rates-booking/#rates-van-14-pax opens that vehicle;
  // otherwise keep the server-rendered default (SUV)
  const hash = window.location.hash.replace('#', '');
  const initial = (hash.indexOf('rates-') === 0 && document.getElementById(hash))
    ? hash
    : (sections.find(function (section) { return section.classList.contains('is-active'); }) || sections[0]).id;
  selectVehicle(initial, false, false);

  applyFilters();
  // No stage-image preload needed: the tab thumbnails eagerly load the same
  // image files the stages use, so switching vehicles is instant.
});
