/**
 * driver.js — Commute Assistant
 *
 * Handles trip start/end, GPS tracking, and smart route recommendation.
 *
 * Route inputs:
 *   #routeNo   — Bus number (e.g. "12")  → looked up in bus_details.xlsx via /get-route
 *   #routeArea — Route area (e.g. "Velachery") → auto-filled, driver can override
 *
 * The value sent to /location as `route` is routeNo normalised to UPPERCASE,
 * keeping it consistent with student-side comparison and backend storage.
 * The routeArea is sent as the additional `busRoute` field.
 */

'use strict';

// ─────────────────────────────────────────────
// BASE URL
// Injected by Flask into driver.html as window.BASE_URL before this
// script loads. Ensures all fetch() calls reach the correct backend
// on Render cross-origin deployments. Never hardcode localhost.
// ─────────────────────────────────────────────
const BASE_URL = (typeof window.BASE_URL === 'string' ? window.BASE_URL : '').replace(/\/$/, '');

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
  const busNo = routeNoInput.value.trim().toUpperCase();

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
    const res  = await fetch(`${BASE_URL}/get-route?bus_no=${encodeURIComponent(busNo)}`);
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
    console.error("[fetchRouteRecommendation] error:", err);
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
  // Normalise to UPPERCASE — must match backend _normalize_route() and student comparison
  const routeNo   = routeNoInput.value.trim().toUpperCase();
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

  // Reflect normalised value back in the input so driver sees what was stored
  routeNoInput.value = routeNo;

  tripActive = true;
  setStatus("TRIP STARTED", "#09f443");
  startBtn.disabled = true;
  endBtn.disabled   = false;

  console.log(`[startTrip] route=${routeNo} area=${routeArea} type=${busType}`);

  watchId = navigator.geolocation.watchPosition(
    pos => {
      if (!tripActive) return;

      const payload = {
        route:    routeNo,
        busRoute: routeArea,
        busType:  busType,
        lat:      pos.coords.latitude,
        lng:      pos.coords.longitude
      };

      console.log("[location] sending:", payload);

      fetch(`${BASE_URL}/location`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(payload)
      })
      .then(res => res.json())
      .then(data => {
        if (data.error) {
          console.error("[location] server error:", data.error);
        } else {
          console.log("[location] saved OK — route:", data.route, "ts:", data.timestamp);
        }
      })
      .catch(err => console.error("[location] fetch failed:", err));
    },
    err => {
      console.error("[GPS] error:", err);
      alert("Enable GPS for tracking");
    },
    {
      enableHighAccuracy: true,
      maximumAge:         0,
      timeout:            10000
    }
  );
}

function endTrip() {
  // Normalise consistently with startTrip and backend
  const routeNo = routeNoInput.value.trim().toUpperCase();

  tripActive = false;
  setStatus("TRIP ENDED", "#dc3545");
  startBtn.disabled = false;
  endBtn.disabled   = true;

  if (watchId !== null) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }

  console.log(`[endTrip] route=${routeNo}`);

  fetch(`${BASE_URL}/end_trip`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ route: routeNo })
  })
  .then(res => res.json())
  .then(data => console.log("[endTrip] response:", data))
  .catch(err => console.error("[endTrip] fetch failed:", err));
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
