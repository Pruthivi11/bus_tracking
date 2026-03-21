/**
 * driver.js — Commute Assistant
 *
 * Handles trip start/end, GPS tracking, and smart route recommendation.
 *
 * Route inputs:
 *   #routeNo   — Bus number (e.g. "12")  → looked up in bus_details.xlsx via /get-route
 *   #routeArea — Route area (e.g. "Velachery") → auto-filled, driver can override
 *
 * The value sent to /location as `route` remains the routeNo (bus number),
 * keeping full backward compatibility with student tracking and admin dashboard.
 * The routeArea is sent as the additional `busRoute` field.
 */

'use strict';

let watchId   = null;
let tripActive = false;

const startBtn  = document.getElementById("startBtn");
const endBtn    = document.getElementById("endBtn");

// ─────────────────────────────────────────────
// STATUS BANNER
// setStatus is defined in driver.html to use the
// styled banner; this fallback handles edge cases.
// ─────────────────────────────────────────────

if (typeof setStatus !== "function") {
  window.setStatus = function(text, color) {
    console.log("Status:", text, color);
  };
}


// ─────────────────────────────────────────────
// ROUTE RECOMMENDATION
// ─────────────────────────────────────────────

const routeNoInput      = document.getElementById("routeNo");
const routeAreaInput    = document.getElementById("routeArea");
const routeSuggestion   = document.getElementById("routeSuggestion");
const suggestionText    = document.getElementById("suggestionText");
const routeNoMatch      = document.getElementById("routeNoMatch");
const routeLoadingIcon  = document.getElementById("routeLoadingIcon");

// Track whether the driver manually edited the area field
let routeAreaModified = false;
// Track the last suggestion so we can detect overrides
let lastSuggestion    = "";

/**
 * Called on every input event on #routeNo.
 * Debounced to avoid a fetch on every keystroke.
 */
let _routeDebounce = null;
routeNoInput.addEventListener("input", () => {
  clearTimeout(_routeDebounce);
  _routeDebounce = setTimeout(fetchRouteRecommendation, 400);
});

/**
 * Also trigger on blur so recommendation fires even if
 * the driver pastes a number without triggering input events.
 */
routeNoInput.addEventListener("blur", fetchRouteRecommendation);

/**
 * Mark route area as modified when driver edits it,
 * so we know not to overwrite it with a future suggestion.
 */
routeAreaInput.addEventListener("input", () => {
  if (routeAreaInput.value.trim() !== lastSuggestion) {
    routeAreaModified = true;
    routeAreaInput.classList.add("route-modified");
    // Hide suggestion badge if driver is actively editing
    routeSuggestion.classList.remove("visible");
    routeNoMatch.classList.remove("visible");
  } else {
    routeAreaModified = false;
    routeAreaInput.classList.remove("route-modified");
  }
});

/**
 * Fetch the recommended route area for the entered bus number.
 * Auto-fills #routeArea if driver has NOT manually modified it.
 */
async function fetchRouteRecommendation() {
  const busNo = routeNoInput.value.trim();

  // Clear badges on empty input
  if (!busNo) {
    routeSuggestion.classList.remove("visible");
    routeNoMatch.classList.remove("visible");
    routeLoadingIcon.classList.remove("visible");
    return;
  }

  // Show loading spinner on the routeNo input
  routeLoadingIcon.classList.add("visible");
  routeSuggestion.classList.remove("visible");
  routeNoMatch.classList.remove("visible");

  try {
    const res  = await fetch(`/get-route?bus_no=${encodeURIComponent(busNo)}`);
    const data = await res.json();

    routeLoadingIcon.classList.remove("visible");

    if (data.found && data.bus_route) {
      lastSuggestion = data.bus_route;

      // Only autofill if driver has NOT manually modified the area
      if (!routeAreaModified) {
        routeAreaInput.value = data.bus_route;
        routeAreaInput.classList.remove("route-modified");
      }

      // Always show the suggestion badge
      suggestionText.textContent = data.bus_route;
      routeSuggestion.classList.add("visible");
      routeNoMatch.classList.remove("visible");

    } else {
      // Bus number not found in Excel
      lastSuggestion = "";
      routeSuggestion.classList.remove("visible");
      routeNoMatch.classList.add("visible");
    }

  } catch (err) {
    console.log("Route fetch error:", err);
    routeLoadingIcon.classList.remove("visible");
    // Fail silently — driver can still type the area manually
  }
}

/**
 * Allow driver to accept the suggestion by clicking the badge.
 * This resets any manual override.
 */
routeSuggestion.addEventListener("click", () => {
  if (lastSuggestion) {
    routeAreaInput.value = lastSuggestion;
    routeAreaInput.classList.remove("route-modified");
    routeAreaModified    = false;
    routeSuggestion.classList.remove("visible");
  }
});


// ─────────────────────────────────────────────
// TRIP CONTROLS
// ─────────────────────────────────────────────

function startTrip() {
  const routeNo   = routeNoInput.value.trim();
  const routeArea = routeAreaInput.value.trim();
  const busType   = document.getElementById("busType").value;

  if (!routeNo) {
    showFieldError(routeNoInput, "Enter the route number");
    return;
  }
  if (!busType) {
    alert("Select bus type");
    return;
  }

  tripActive = true;
  setStatus("TRIP STARTED", "#09f443");
  startBtn.disabled = true;
  endBtn.disabled   = false;

  watchId = navigator.geolocation.watchPosition(
    pos => {
      if (!tripActive) return;

      fetch("/location", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          route:    routeNo,       // bus number — used for tracking identity
          busRoute: routeArea,     // route area — display/info field
          busType:  busType,
          lat:      pos.coords.latitude,
          lng:      pos.coords.longitude
        })
      }).catch(err => console.log("Location send error:", err));
    },
    err => {
      console.log("GPS error:", err);
      alert("Enable GPS for tracking");
    },
    {
      enableHighAccuracy: true,
      maximumAge:         0,
      timeout:            5000
    }
  );
}

function endTrip() {
  const routeNo = routeNoInput.value.trim();

  tripActive = false;
  setStatus("TRIP ENDED", "#dc3545");
  startBtn.disabled = false;
  endBtn.disabled   = true;

  if (watchId !== null) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }

  fetch("/end_trip", {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ route: routeNo })
  }).catch(err => console.log("End trip error:", err));
}

function logout() {
  endTrip();
  window.location.href = "/logout";
}


// ─────────────────────────────────────────────
// FIELD ERROR HELPER
// Shows a brief shake + border highlight on invalid field.
// ─────────────────────────────────────────────

function showFieldError(inputEl, message) {
  inputEl.classList.add("route-modified");    // reuse amber border
  inputEl.focus();
  // Brief shake via a transient class
  inputEl.animate
    ? inputEl.animate(
        [
          { transform: "translateX(0)" },
          { transform: "translateX(-5px)" },
          { transform: "translateX(5px)" },
          { transform: "translateX(0)" }
        ],
        { duration: 300, easing: "ease" }
      )
    : null;
  // Use native tooltip-style placeholder warning
  const orig = inputEl.placeholder;
  inputEl.placeholder = message;
  setTimeout(() => { inputEl.placeholder = orig; }, 2500);
}
