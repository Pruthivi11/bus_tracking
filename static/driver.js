/**
 * driver.js — Commute Assistant
 *
 * Route inputs:
 *   #routeNo   — Bus number (e.g. "12")
 *                On input/blur → /get-route autofills #routeArea with the
 *                matching area from bus_details (unchanged behaviour).
 *
 *   #routeArea — Route area (e.g. "Velachery")
 *                Auto-filled by route number lookup.
 *                If driver types, a dropdown of matching areas from the
 *                bus_details dataset appears (via GET /routes?q=<input>).
 *                Selecting a dropdown item sets the field.
 *                If nothing is selected the autofilled default is used.
 *
 * All fetch() calls use BASE_URL (injected by Flask) so the code works
 * on same-origin and cross-origin Render deployments.
 */

'use strict';

// ─────────────────────────────────────────────
// BASE URL
// ─────────────────────────────────────────────
const BASE_URL = (typeof window.BASE_URL === 'string'
  ? window.BASE_URL
  : '').replace(/\/$/, '');


// ─────────────────────────────────────────────
// TRIP STATE
// ─────────────────────────────────────────────
let watchId    = null;
let tripActive = false;

const startBtn = document.getElementById("startBtn");
const endBtn   = document.getElementById("endBtn");


// ─────────────────────────────────────────────
// STATUS BANNER FALLBACK
// setStatus is overridden in driver.html; this handles any call
// that fires before that override is installed.
// ─────────────────────────────────────────────
if (typeof setStatus !== "function") {
  window.setStatus = function(text, color) {
    console.log("[setStatus]", text, color);
  };
}


// ─────────────────────────────────────────────
// DOM REFS
// ─────────────────────────────────────────────
const routeNoInput     = document.getElementById("routeNo");
const routeAreaInput   = document.getElementById("routeArea");
const routeDropdown    = document.getElementById("routeAreaDropdown");
const routeLoadingIcon = document.getElementById("routeLoadingIcon");


// ─────────────────────────────────────────────
// STATE
// ─────────────────────────────────────────────
let lastSuggestion    = "";   // area autofilled from route number
let routeAreaModified = false;  // true once driver manually edits the area


// ═══════════════════════════════════════════════════════════════
// SECTION 1: ROUTE NUMBER → AREA AUTOFILL
//   When driver enters a bus number, /get-route is called.
//   The returned area is placed in #routeArea if the driver
//   has not manually modified it.
// ═══════════════════════════════════════════════════════════════

let _routeNoDebounce = null;

routeNoInput.addEventListener("input", () => {
  clearTimeout(_routeNoDebounce);
  _routeNoDebounce = setTimeout(fetchRouteRecommendation, 400);
});

// Trigger on blur too — handles paste without input event
routeNoInput.addEventListener("blur", fetchRouteRecommendation);

/**
 * Look up the default route area for the entered bus number.
 * Autofills #routeArea only if the driver has not yet modified it.
 */
async function fetchRouteRecommendation() {
  const busNo = routeNoInput.value.trim().toUpperCase();

  if (!busNo) {
    routeLoadingIcon.classList.remove("visible");
    return;
  }

  routeLoadingIcon.classList.add("visible");

  try {
    const res  = await fetch(`${BASE_URL}/get-route?bus_no=${encodeURIComponent(busNo)}`);
    const data = await res.json();

    routeLoadingIcon.classList.remove("visible");

    if (data.found && data.bus_route) {
      lastSuggestion = data.bus_route;

      if (!routeAreaModified) {
        // Autofill only — driver hasn't overridden yet
        routeAreaInput.value = data.bus_route;
        routeAreaInput.classList.remove("route-modified");
      }
    } else {
      lastSuggestion = "";
    }

  } catch (err) {
    console.error("[fetchRouteRecommendation] error:", err);
    routeLoadingIcon.classList.remove("visible");
  }
}


// ═══════════════════════════════════════════════════════════════
// SECTION 2: ROUTE AREA AUTOCOMPLETE DROPDOWN
//   When the driver types in #routeArea, GET /routes?q=<query>
//   is called and matching areas from the bus_details dataset
//   are shown as a tap-friendly dropdown.
//   Selecting an item fills the field and closes the dropdown.
// ═══════════════════════════════════════════════════════════════

let _routeAreaDebounce = null;

routeAreaInput.addEventListener("input", () => {
  routeAreaModified = true;
  routeAreaInput.classList.add("route-modified");

  clearTimeout(_routeAreaDebounce);
  _routeAreaDebounce = setTimeout(() => {
    fetchRouteSuggestions(routeAreaInput.value.trim());
  }, 250);
});

// Open dropdown on focus if field already has content
routeAreaInput.addEventListener("focus", () => {
  const q = routeAreaInput.value.trim();
  if (q.length > 0) {
    fetchRouteSuggestions(q);
  }
});

// Close dropdown on blur — small delay so a tap on an item registers first
routeAreaInput.addEventListener("blur", () => {
  setTimeout(hideDropdown, 180);
});

// Sync aria-expanded with dropdown visibility
function _setDropdownOpen(open) {
  routeAreaInput.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) {
    routeDropdown.classList.add("open");
  } else {
    routeDropdown.classList.remove("open");
  }
}

/**
 * Fetch matching route areas from /routes?q=<query>.
 * Renders results as tappable list items in #routeAreaDropdown.
 *
 * @param {string} query - The text the driver has typed so far
 */
async function fetchRouteSuggestions(query) {
  try {
    const url = `${BASE_URL}/routes?q=${encodeURIComponent(query)}`;
    const res = await fetch(url);
    const suggestions = await res.json();

    if (!Array.isArray(suggestions)) { hideDropdown(); return; }

    renderDropdown(suggestions, query);

  } catch (err) {
    console.error("[fetchRouteSuggestions] error:", err);
    hideDropdown();
  }
}

/**
 * Build dropdown list items from an array of route area strings.
 * Highlights the matching portion of each item with <mark>.
 *
 * @param {string[]} items  - Array of matching route area names
 * @param {string}   query  - Current input text (used for highlighting)
 */
function renderDropdown(items, query) {
  routeDropdown.innerHTML = "";

  if (items.length === 0) {
    routeDropdown.innerHTML =
      '<li class="route-dropdown-empty">No matching routes</li>';
    _setDropdownOpen(true);
    return;
  }

  items.forEach(area => {
    const li = document.createElement("li");
    li.className    = "route-dropdown-item";
    li.role         = "option";
    li.setAttribute("aria-selected", "false");

    // Highlight the matched portion
    const highlighted = highlightMatch(area, query);
    li.innerHTML = `
      <i class="fa-solid fa-location-dot route-dropdown-item-icon"></i>
      <span>${highlighted}</span>
    `;

    // Use mousedown (fires before blur) so the click registers before
    // the blur handler hides the dropdown
    li.addEventListener("mousedown", (e) => {
      e.preventDefault();       // prevent blur from firing first
      selectSuggestion(area);
    });

    // Touch support for mobile
    li.addEventListener("touchend", (e) => {
      e.preventDefault();
      selectSuggestion(area);
    });

    routeDropdown.appendChild(li);
  });

  _setDropdownOpen(true);
}

/**
 * Fill the route area input with the selected suggestion,
 * clear the modified flag (this is now a valid dataset selection),
 * and close the dropdown.
 *
 * @param {string} value - The selected route area name
 */
function selectSuggestion(value) {
  routeAreaInput.value = value;
  routeAreaInput.classList.remove("route-modified");
  routeAreaModified    = false;
  lastSuggestion       = value;
  hideDropdown();
  routeAreaInput.blur();
}

/**
 * Close and empty the dropdown.
 */
function hideDropdown() {
  _setDropdownOpen(false);
  routeDropdown.innerHTML = "";
}

/**
 * Wrap the matched portion of text in <mark> tags for highlighting.
 * Case-insensitive. Returns escaped HTML to prevent injection.
 *
 * @param {string} text  - Full route area name
 * @param {string} query - Text to highlight
 * @returns {string} HTML string with <mark> around match
 */
function highlightMatch(text, query) {
  if (!query) return escHtml(text);
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return escHtml(text);
  return (
    escHtml(text.slice(0, idx)) +
    "<mark>" + escHtml(text.slice(idx, idx + query.length)) + "</mark>" +
    escHtml(text.slice(idx + query.length))
  );
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Close dropdown if driver taps anywhere outside it
document.addEventListener("click", (e) => {
  if (!routeDropdown.contains(e.target) && e.target !== routeAreaInput) {
    hideDropdown();
  }
});


// ═══════════════════════════════════════════════════════════════
// SECTION 3: TRIP CONTROLS
//   startTrip, endTrip, logout — fully unchanged from previous
//   version except routeArea is sourced from the (possibly
//   dropdown-selected) value of #routeArea.
// ═══════════════════════════════════════════════════════════════

function startTrip() {
  // Normalise to UPPERCASE — matches backend _normalize_route() and student comparison
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

  // Reflect normalised value back so driver sees what will be stored
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


// ═══════════════════════════════════════════════════════════════
// SECTION 4: FIELD ERROR HELPER
// ═══════════════════════════════════════════════════════════════

function showFieldError(inputEl, message) {
  inputEl.classList.add("route-modified");
  inputEl.focus();

  if (inputEl.animate) {
    inputEl.animate(
      [
        { transform: "translateX(0)" },
        { transform: "translateX(-5px)" },
        { transform: "translateX(5px)" },
        { transform: "translateX(0)" }
      ],
      { duration: 300, easing: "ease" }
    );
  }

  const orig = inputEl.placeholder;
  inputEl.placeholder = message;
  setTimeout(() => { inputEl.placeholder = orig; }, 2500);
}


// ═══════════════════════════════════════════════════════════════
// SECTION 5: ROUTE MAPPING
//
// The mapping card (#mappingCard) is hidden by default.
// After the driver enters a route number, checkMappingPermission()
// calls GET /driver/mapping-status?route=X. If the admin has
// enabled mapping, the card becomes visible.
//
// The driver taps "Start Mapping" → POST /driver/start-mapping.
// From that point, every /location POST on the backend
// automatically appends GPS points (smart-compressed to ≥15 m).
// No additional frontend calls are needed during mapping.
//
// The driver taps "Stop & Save" → POST /driver/stop-mapping.
// The backend encodes all collected points as a polyline and
// clears raw_points. The card resets to its initial state.
// ═══════════════════════════════════════════════════════════════

const mappingCard       = document.getElementById("mappingCard");
const mappingBadge      = document.getElementById("mappingBadge");
const mappingHint       = document.getElementById("mappingHint");
const mappingStats      = document.getElementById("mappingStats");
const startMappingBtn   = document.getElementById("startMappingBtn");
const startMappingLabel = document.getElementById("startMappingLabel");
const stopMappingBtn    = document.getElementById("stopMappingBtn");

let mappingActive = false;

/**
 * Called whenever the route number changes (input/blur on #routeNo).
 * Checks whether the admin has allowed mapping for this route and
 * shows/hides the mapping card accordingly.
 */
async function checkMappingPermission() {
  const routeNo = routeNoInput.value.trim().toUpperCase();
  if (!routeNo) {
    mappingCard.classList.remove("visible");
    return;
  }

  try {
    const res  = await fetch(`${BASE_URL}/driver/mapping-status?route=${encodeURIComponent(routeNo)}`);
    const data = await res.json();

    if (!data.allowed) {
      mappingCard.classList.remove("visible");
      return;
    }

    // Mapping is permitted — show the card and set its state
    mappingCard.classList.add("visible");

    if (data.is_active) {
      _setMappingUI("active", data.point_count);
    } else if (data.has_polyline) {
      startMappingLabel.textContent = "Update Mapping";
      _setMappingUI("idle", 0, true);
    } else {
      startMappingLabel.textContent = "Start Mapping";
      _setMappingUI("idle", 0, false);
    }

    console.log("[mapping] status for route", routeNo, data);
  } catch (err) {
    console.error("[mapping] status check failed:", err);
    mappingCard.classList.remove("visible");
  }
}

/**
 * Update the mapping card UI state.
 * @param {"idle"|"active"|"done"} state
 * @param {number} pointCount
 * @param {boolean} [hasPoly]
 */
function _setMappingUI(state, pointCount = 0, hasPoly = false) {
  mappingCard.classList.remove("rm-mapping-active");

  if (state === "active") {
    mappingActive = true;
    mappingCard.classList.add("rm-mapping-active");
    mappingBadge.textContent = "Recording…";
    mappingBadge.className   = "rm-driver-badge rm-badge-recording";
    mappingHint.textContent  = "Drive the full route. GPS points are saved automatically.";
    mappingStats.textContent = pointCount > 0 ? `${pointCount} points recorded` : "";
    startMappingBtn.disabled = true;
    stopMappingBtn.disabled  = false;

  } else if (state === "done") {
    mappingActive = false;
    mappingBadge.textContent = "Saved ✓";
    mappingBadge.className   = "rm-driver-badge rm-badge-mapped";
    mappingHint.textContent  = "Route saved successfully.";
    startMappingBtn.disabled = false;
    stopMappingBtn.disabled  = true;

  } else {
    mappingActive = false;
    mappingBadge.textContent = hasPoly ? `v${0} — ready to update` : "Ready";
    mappingBadge.className   = hasPoly
      ? "rm-driver-badge rm-badge-allowed"
      : "rm-driver-badge";
    mappingHint.textContent  = hasPoly
      ? "Admin has enabled re-recording. Drive the full route to update."
      : "Admin has enabled route recording. Drive the full route, then stop.";
    mappingStats.textContent = "";
    startMappingBtn.disabled = false;
    stopMappingBtn.disabled  = true;
  }
}

/** Start a mapping session. */
async function startMapping() {
  const routeNo = routeNoInput.value.trim().toUpperCase();
  if (!routeNo) {
    alert("Enter a route number first.");
    return;
  }

  startMappingBtn.disabled = true;

  try {
    const res  = await fetch(`${BASE_URL}/driver/start-mapping`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ route: routeNo }),
    });
    const data = await res.json();

    if (data.status === "ok") {
      _setMappingUI("active", 0);
      console.log("[mapping] started for route:", routeNo);
    } else {
      startMappingBtn.disabled = false;
      alert(data.error || "Could not start mapping.");
    }
  } catch (err) {
    startMappingBtn.disabled = false;
    console.error("[mapping] start failed:", err);
    alert("Network error — could not start mapping.");
  }
}

/** Stop the mapping session and save the polyline. */
async function stopMapping() {
  const routeNo = routeNoInput.value.trim().toUpperCase();
  stopMappingBtn.disabled = true;

  try {
    const res  = await fetch(`${BASE_URL}/driver/stop-mapping`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ route: routeNo }),
    });
    const data = await res.json();

    if (data.status === "ok") {
      const msg = data.message || `Route saved — ${data.point_count} points`;
      console.log("[mapping] stopped:", data);

      if (data.polyline) {
        mappingStats.textContent = `${data.point_count} points → polyline saved (v${data.version})`;
        _setMappingUI("done", data.point_count);
      } else {
        // Too few points
        mappingStats.textContent = data.message || "Not enough points recorded";
        _setMappingUI("idle", 0, false);
      }

      alert(msg);
    } else {
      stopMappingBtn.disabled = false;
      alert(data.error || "Could not stop mapping.");
    }
  } catch (err) {
    stopMappingBtn.disabled = false;
    console.error("[mapping] stop failed:", err);
    alert("Network error — could not stop mapping.");
  }
}

// Hook into route number input events to check mapping permission
routeNoInput.addEventListener("input",  () => {
  clearTimeout(_routeNoDebounce);
  _routeNoDebounce = setTimeout(() => {
    fetchRouteRecommendation();
    checkMappingPermission();
  }, 400);
});

routeNoInput.addEventListener("blur", () => {
  fetchRouteRecommendation();
  checkMappingPermission();
});
