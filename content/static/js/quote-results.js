/* =====================================================
   Grayson Towncar — Quote Results map
   A clean, warm-grayscale route map: custom monochrome
   styling, the default A/B pins replaced with simple
   gold (pickup) and ink (drop-off) dots, and the route
   line. Addresses live in the itinerary panel below the
   map (no clipping); the map only feeds the live
   distance + ETA into the trip bar and itinerary.
   Best-effort: if Maps fails, the panel still shows the
   addresses and the cards still work.
   ===================================================== */
(function () {
  "use strict";

  var mapEl = document.getElementById("qrMap");
  if (!mapEl) return;

  var origin = mapEl.getAttribute("data-origin");
  var destination = mapEl.getAttribute("data-destination");
  if (!origin || !destination) return;

  // Warm-grayscale map style (on-brand cream land, muted water, no clutter).
  var MAP_STYLE = [
    { elementType: "geometry", stylers: [{ color: "#fbf8f2" }] },
    { elementType: "labels.icon", stylers: [{ visibility: "off" }] },
    { elementType: "labels.text.fill", stylers: [{ color: "#b3ada1" }] },
    { elementType: "labels.text.stroke", stylers: [{ color: "#fbf8f2" }] },
    { featureType: "administrative", elementType: "geometry", stylers: [{ visibility: "off" }] },
    { featureType: "administrative.land_parcel", stylers: [{ visibility: "off" }] },
    { featureType: "administrative.neighborhood", stylers: [{ visibility: "off" }] },
    { featureType: "poi", stylers: [{ visibility: "off" }] },
    { featureType: "road", elementType: "geometry", stylers: [{ color: "#e7e2d6" }] },
    { featureType: "road", elementType: "labels", stylers: [{ visibility: "off" }] },
    { featureType: "road.highway", elementType: "geometry", stylers: [{ color: "#ded7c9" }] },
    { featureType: "transit", stylers: [{ visibility: "off" }] },
    { featureType: "water", elementType: "geometry", stylers: [{ color: "#d8d3c8" }] },
    { featureType: "water", elementType: "labels", stylers: [{ visibility: "off" }] },
  ];

  function dotIcon(color) {
    return {
      path: google.maps.SymbolPath.CIRCLE,
      scale: 7,
      fillColor: color,
      fillOpacity: 1,
      strokeColor: "#ffffff",
      strokeWeight: 3,
    };
  }

  function draw() {
    if (!window.google || !google.maps) return;

    var map = new google.maps.Map(mapEl, {
      disableDefaultUI: true,
      gestureHandling: "cooperative",
      styles: MAP_STYLE,
      backgroundColor: "#fbf8f2",
    });

    var service = new google.maps.DirectionsService();
    var renderer = new google.maps.DirectionsRenderer({
      map: map,
      suppressMarkers: true, // we draw our own clean dots
      preserveViewport: true, // we fit bounds ourselves with padding
      polylineOptions: { strokeColor: "#1b1813", strokeWeight: 4, strokeOpacity: 0.9 },
    });

    service.route(
      { origin: origin, destination: destination, travelMode: google.maps.TravelMode.DRIVING },
      function (res, status) {
        if (status !== "OK" || !res.routes || !res.routes.length) return;
        renderer.setDirections(res);
        var leg = res.routes[0].legs[0];
        if (!leg) return;

        new google.maps.Marker({ position: leg.start_location, map: map, icon: dotIcon("#b08d57"), zIndex: 3 });
        new google.maps.Marker({ position: leg.end_location, map: map, icon: dotIcon("#14110c"), zIndex: 3 });

        var bounds = new google.maps.LatLngBounds();
        bounds.extend(leg.start_location);
        bounds.extend(leg.end_location);
        map.fitBounds(bounds, { top: 48, right: 48, bottom: 48, left: 48 });

        var meta = leg.distance.text + " · about " + leg.duration.text;

        var routeMeta = document.getElementById("qrRouteMeta");
        if (routeMeta) routeMeta.textContent = meta;

        var etaWrap = document.getElementById("qrEta");
        var etaText = document.getElementById("qrEtaText");
        if (etaText) etaText.textContent = meta;
        if (etaWrap) etaWrap.hidden = false;
      }
    );
  }

  if (window.qwMapsReady) draw();
  else document.addEventListener("qw-maps-ready", draw, { once: true });
})();
