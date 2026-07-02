/* =====================================================
   Grayson Towncar — Trip widget (step 1 of 2)
   Collects the trip (pickup + drop-off addresses with
   Google Places autocomplete, or hours) and redirects to
   the full-width results page (/transfer-quote/) where
   every vehicle class is priced. Degrades gracefully: if
   Maps fails to load the inputs stay plain text and the
   results page still prices from the typed address.
   ===================================================== */
(function () {
  "use strict";

  var dataEl = document.getElementById("gtc-quote-data");
  var root = document.getElementById("qw");
  if (!dataEl || !root) return;

  var DATA;
  try {
    DATA = JSON.parse(dataEl.textContent);
  } catch (e) {
    return;
  }

  var form = document.getElementById("qwForm");
  var tabs = root.querySelectorAll(".qw-tab");
  var modeCity = root.querySelector(".qw-mode-city");
  var modeHourly = root.querySelector(".qw-mode-hourly");
  var pickupInput = document.getElementById("qw-pickup");
  var dropoffInput = document.getElementById("qw-dropoff");
  var hoursInput = document.getElementById("qw-hours");
  var dateInput = document.getElementById("qw-date");
  var errorBox = document.getElementById("qwError");
  var submitBtn = form.querySelector(".qw-submit");

  var mode = "city_to_city";
  var resultsUrl = DATA.results_url || "/transfer-quote/";

  // ---- Date defaults -----------------------------------------------------
  var today = new Date();
  var iso = today.toISOString().slice(0, 10);
  dateInput.min = iso;
  if (!dateInput.value) dateInput.value = iso;

  // ---- Helpers -----------------------------------------------------------
  function showError(msg) { errorBox.textContent = msg; errorBox.hidden = false; }
  function clearError() { errorBox.hidden = true; errorBox.textContent = ""; }

  // ---- Google Places autocomplete (progressive enhancement) -------------
  function initAutocomplete() {
    if (!window.google || !google.maps || !google.maps.places || !pickupInput) return;
    var opts = {
      fields: ["formatted_address", "geometry", "name"],
      componentRestrictions: { country: "us" },
    };
    try {
      new google.maps.places.Autocomplete(pickupInput, opts);
      new google.maps.places.Autocomplete(dropoffInput, opts);
    } catch (e) { /* fall back to plain text inputs */ }
  }
  if (window.qwMapsReady) initAutocomplete();
  else document.addEventListener("qw-maps-ready", initAutocomplete, { once: true });

  // ---- Tabs --------------------------------------------------------------
  function setMode(m) {
    mode = m;
    tabs.forEach(function (t) {
      var on = t.getAttribute("data-mode") === m;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", String(on));
    });
    modeCity.hidden = m !== "city_to_city";
    modeHourly.hidden = m !== "hourly";
    clearError();
  }
  tabs.forEach(function (t) {
    t.addEventListener("click", function () { setMode(t.getAttribute("data-mode")); });
  });

  // ---- Submit: hand off to the results page ------------------------------
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    clearError();

    var params = new URLSearchParams();
    params.set("service_type", mode);
    params.set("date", dateInput.value || "");

    if (mode === "city_to_city") {
      var origin = (pickupInput.value || "").trim();
      var destination = (dropoffInput.value || "").trim();
      if (!origin) { showError("Please enter a pickup location."); return; }
      if (!destination) { showError("Please enter a drop-off location."); return; }
      params.set("origin", origin);
      params.set("destination", destination);
    } else {
      params.set("hours", hoursInput.value || "");
    }

    submitBtn.disabled = true;
    submitBtn.classList.add("is-loading");
    window.location.href = resultsUrl + "?" + params.toString();
  });

  // Init
  setMode("city_to_city");
})();
