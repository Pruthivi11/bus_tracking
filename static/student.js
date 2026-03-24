/**
 * student.js — Commute Assistant
 *
 * BASE_URL is injected by Flask into student.html as window.BASE_URL
 * before this script loads. It is empty string when frontend and backend
 * share the same origin (same Render service), or the full backend URL
 * when they are on different origins.
 *
 * All fetch() calls use BASE_URL + path so the same JS works
 * locally, on Render, and in a future React Native app.
 */

'use strict';

// ─────────────────────────────────────────────
// BASE URL
// Set by Flask template before this script loads (see student.html).
// Never hardcode localhost here.
// ─────────────────────────────────────────────
const BASE_URL = (typeof window.BASE_URL === 'string' ? window.BASE_URL : '').replace(/\/$/, '');


// ─────────────────────────────────────────────
// STATE
// ─────────────────────────────────────────────
let map               = null;
let busMarkers        = {};
let studentMarker     = null;
let destinationMarker = null;
let isOnboard         = false;
let lastBusPosition   = {};
let destination       = null;
let tripMode          = "morning";

let reminders = {};   // legacy — kept so no reference errors; logic replaced by proximityAlerts


// ─────────────────────────────────────────────
// SMART POLLING STATE
//
// onboardTriggered — once-trigger guard; set true when the student
//   boards so the onboard POST fires exactly once per trip session.
//   Separate from isOnboard so the two concerns are explicit.
//
// lastDistance — last known bus-to-student distance in metres.
//   Used by shouldRecalculate() to skip redundant work when the
//   student has barely moved (< 10 m change).
//
// pollingInterval — current fetch interval in milliseconds.
//   Starts at 5 s; shrinks to 2 s when close to the bus,
//   grows to 15 s when far away.
//
// pollingTimer — handle returned by setInterval(); cleared and
//   restarted by updatePolling() when the interval changes.
//   The timer is the single driver of /get_locations fetches.
//   watchPosition only updates the student marker, not the fetch.
// ─────────────────────────────────────────────

let onboardTriggered = false;
let lastDistance     = null;

const POLL_FAR    = 15000;   // ms  — bus > 1000 m away
const POLL_MID    = 5000;    // ms  — bus 100 m – 1000 m away
const POLL_NEAR   = 2000;    // ms  — bus < 100 m away
const POLL_DEFAULT = POLL_MID;

let pollingInterval = POLL_DEFAULT;
let pollingTimer    = null;


// ─────────────────────────────────────────────
// DOM REFERENCES
// ─────────────────────────────────────────────
const rollNoEl         = document.getElementById("rollNo");
const busNoEl          = document.getElementById("busNo");           // hidden — holds resolved route_no
const studentSearchEl  = document.getElementById("studentSearch");   // visible search bar
const searchClearBtn   = document.getElementById("studentSearchClear");
const searchDropdownEl = document.getElementById("studentSearchDropdown");
const showBtn          = document.getElementById("showMap");
const mapEl            = document.getElementById("map");
const selectDestBtn    = document.getElementById("selectDestination");
const destinationBlock = document.getElementById("destinationBlock");
const proximityBox     = document.getElementById("proximityBox");
const proxLimitHint    = document.getElementById("proxLimitHint");
const proximityToast   = document.getElementById("proximityToast");


// ─────────────────────────────────────────────
// SMART SEARCH STATE
// ─────────────────────────────────────────────

/**
 * The resolved selection from the dropdown.
 * Populated by onSuggestionClick() and cleared when the search input changes.
 * loadBusLocations() reads busNoEl.value (= selectedRoute.route_no) —
 * this object provides context (e.g. route_area) for display.
 */
let selectedRoute = null;   // { route_no, route_area, is_active } | null


// ─────────────────────────────────────────────
// TRIP MODE TOGGLE
// ─────────────────────────────────────────────
document.querySelectorAll("input[name='tripMode']").forEach(el => {
  el.addEventListener("change", () => {
    tripMode = el.value;
    if (tripMode === "evening") {
      destinationBlock.classList.add("visible");
    } else {
      destinationBlock.classList.remove("visible");
    }
  });
});

selectDestBtn?.addEventListener("click", () => {
  alert("Click on map to select destination");
});


// ═══════════════════════════════════════════════════════════════════
// PROXIMITY ALERTS  (Evening mode only)
//
// Students select up to 3 distance thresholds from a chip grid.
// When the bus crosses a threshold, a non-blocking in-page toast
// is shown once per threshold per destination pin.
//
// selectedDistances  — Set of metres (numbers) currently selected
// firedDistances     — Set of metres already alerted this session;
//                      cleared when the destination pin moves
// MAX_SELECTIONS     — max chips that can be selected at once
// ═══════════════════════════════════════════════════════════════════

const MAX_PROX_SELECTIONS = 3;

/** metres currently active as alert thresholds */
const selectedDistances = new Set();

/** metres for which an alert has already been shown this destination */
const firedDistances = new Set();


/**
 * Toggle a distance chip on/off.
 * Enforces MAX_PROX_SELECTIONS — if limit is reached, deselect another first.
 *
 * @param {number} metres
 * @param {HTMLElement} chipEl
 */
function toggleDistance(metres, chipEl) {
  if (selectedDistances.has(metres)) {
    // Deselect
    selectedDistances.delete(metres);
    chipEl.classList.remove("prox-selected");
    chipEl.setAttribute("aria-pressed", "false");
  } else {
    if (selectedDistances.size >= MAX_PROX_SELECTIONS) {
      // Max reached — flash the hint and bail
      proxLimitHint.classList.add("prox-limit-flash");
      setTimeout(() => proxLimitHint.classList.remove("prox-limit-flash"), 600);
      return;
    }
    selectedDistances.add(metres);
    chipEl.classList.add("prox-selected");
    chipEl.setAttribute("aria-pressed", "true");
  }

  // Update count hint
  const remaining = MAX_PROX_SELECTIONS - selectedDistances.size;
  if (selectedDistances.size === 0) {
    proxLimitHint.textContent = "Select up to 3";
  } else if (remaining === 0) {
    proxLimitHint.textContent = "Max selected";
    proxLimitHint.classList.add("prox-limit-reached");
  } else {
    proxLimitHint.textContent = `${remaining} more`;
    proxLimitHint.classList.remove("prox-limit-reached");
  }
}

// Wire up all proximity chips
document.querySelectorAll(".prox-chip").forEach(chip => {
  const metres = parseInt(chip.dataset.metres, 10);
  chip.setAttribute("aria-pressed", "false");

  chip.addEventListener("click", () => toggleDistance(metres, chip));
});

/**
 * Called by initMap when the driver pins a new destination.
 * Resets firedDistances so alerts can fire again for the new stop.
 */
function resetProximityFired() {
  firedDistances.clear();
}

/**
 * Show a non-blocking proximity toast for `seconds` then hide it.
 * Multiple calls queue properly — a new alert extends the timer.
 *
 * @param {string}  message
 * @param {number}  [seconds=5]
 */
let _toastTimer = null;
function showProximityToast(message, seconds = 5) {
  clearTimeout(_toastTimer);
  proximityToast.textContent = message;
  proximityToast.classList.add("prox-toast-visible");
  _toastTimer = setTimeout(() => {
    proximityToast.classList.remove("prox-toast-visible");
  }, seconds * 1000);
}

/**
 * Check if the bus has crossed any selected proximity threshold.
 * Called from loadBusLocations() in evening mode when bus is active.
 *
 * @param {{ lat: number, lng: number }} bus
 */
function checkProximityAlerts(bus) {
  if (!destination || selectedDistances.size === 0) return;
  if (bus.lat == null || bus.lng == null) return;

  const dist = getDistance(bus.lat, bus.lng, destination.lat, destination.lng);

  // Sort thresholds descending so the largest un-fired one triggers first
  const sorted = Array.from(selectedDistances).sort((a, b) => b - a);

  for (const threshold of sorted) {
    if (dist <= threshold && !firedDistances.has(threshold)) {
      firedDistances.add(threshold);

      const label = threshold >= 1000
        ? `${threshold / 1000} km`
        : `${threshold} m`;

      showProximityToast(`🔔 Bus is within ${label} of your stop!`);
      console.log(`[proximity] alert fired: ${label} (dist=${dist.toFixed(0)}m)`);
    }
  }
}


// ═══════════════════════════════════════════════════════════════════
// MORNING PICKUP ALERT  (Morning mode only)
//
// Mirrors the evening proximity system but measures bus-to-STUDENT
// distance instead of bus-to-destination distance.
//
// Key differences from evening system:
//   • No selection limit — all 5 chips can be active simultaneously
//   • Alarm is gated by a toggle (morningAlarmEnabled)
//   • morningFiredDistances cleared on route change AND session reset
//   • Uses .morn-chip DOM class (independent of .prox-chip)
// ═══════════════════════════════════════════════════════════════════

/** Whether the student has enabled the pickup alarm */
let morningAlarmEnabled = false;

/** Threshold metres the student has selected for morning alerts */
const morningSelectedDistances = new Set();

/** Metres for which a morning alert has already fired this session */
const morningFiredDistances = new Set();

// ── Alarm toggle button ──
const pickupAlarmBtn = document.getElementById("pickupAlarmToggle");
const morningProxBox = document.getElementById("morningProxBox");
const morningProxCount = document.getElementById("morningProxCount");

pickupAlarmBtn?.addEventListener("click", () => {
  morningAlarmEnabled = !morningAlarmEnabled;
  pickupAlarmBtn.setAttribute("aria-checked", String(morningAlarmEnabled));
  pickupAlarmBtn.classList.toggle("morning-alarm-on", morningAlarmEnabled);

  if (!morningAlarmEnabled) {
    // Alarm turned off — clear fired set so re-enabling starts fresh
    morningFiredDistances.clear();
    console.log("[morning-alert] alarm disabled — fired set cleared");
  } else {
    console.log("[morning-alert] alarm enabled");
  }
});

/**
 * Toggle a morning threshold chip on/off.
 * No selection limit — all chips can be active simultaneously.
 *
 * @param {number}      metres
 * @param {HTMLElement} chipEl
 */
function toggleMorningDistance(metres, chipEl) {
  if (morningSelectedDistances.has(metres)) {
    morningSelectedDistances.delete(metres);
    chipEl.classList.remove("morn-selected");
    chipEl.setAttribute("aria-pressed", "false");
  } else {
    morningSelectedDistances.add(metres);
    chipEl.classList.add("morn-selected");
    chipEl.setAttribute("aria-pressed", "true");
  }

  // Update selection count label
  const n = morningSelectedDistances.size;
  morningProxCount.textContent = n === 0
    ? "0 selected"
    : `${n} selected`;
}

// Wire up all morning chips
document.querySelectorAll(".morn-chip").forEach(chip => {
  const metres = parseInt(chip.dataset.metres, 10);
  chip.addEventListener("click", () => toggleMorningDistance(metres, chip));
});

/**
 * Check if the bus has entered any selected morning pickup threshold.
 * Measures bus-to-student distance (not bus-to-destination).
 * Called from loadBusLocations() in morning mode when bus is active
 * and student GPS is available.
 *
 * Each threshold fires exactly once per session.
 * Resets when alarm is toggled off, route changes, or session resets.
 *
 * @param {{ lat: number, lng: number }} bus
 * @param {number} studentLat
 * @param {number} studentLng
 */
function checkMorningProximityAlerts(bus, studentLat, studentLng) {
  if (!morningAlarmEnabled) return;
  if (morningSelectedDistances.size === 0) return;
  if (bus.lat == null || bus.lng == null) return;
  if (typeof studentLat !== "number" || typeof studentLng !== "number") return;

  // Bus-to-student distance (not destination)
  const dist = getDistance(bus.lat, bus.lng, studentLat, studentLng);

  // Sort descending — largest threshold alerts first
  const sorted = Array.from(morningSelectedDistances).sort((a, b) => b - a);

  for (const threshold of sorted) {
    if (dist <= threshold && !morningFiredDistances.has(threshold)) {
      morningFiredDistances.add(threshold);

      const label = threshold >= 1000
        ? `${threshold / 1000} km`
        : `${threshold} m`;

      showProximityToast(`🚍 Bus is within ${label} — get ready!`);
      console.log(
        `[morning-alert] fired: ${label} (dist=${dist.toFixed(0)}m)`
      );
    }
  }
}


// ─────────────────────────────────────────────
// STATUS BAR
//
// Always calls window.setStatus so the post-load override in
// student.html (which wires up the styled status bar) takes effect.
// The local definition here is a safe no-op fallback for any call
// that fires before the override is installed.
// ─────────────────────────────────────────────
window.setStatus = window.setStatus || function setStatus(text) {
  const el = document.getElementById("statusMsg");
  if (el) el.textContent = text;
};

function setStatus(text) {
  window.setStatus(text);
}


// ─────────────────────────────────────────────
// BUS STATUS TEXT
// ─────────────────────────────────────────────
function updateBusStatus(bus, busNo) {
  // backend `active` is the single source of truth — no secondary time check here.
  // is_bus_active() on the backend already applies the threshold; duplicating it
  // here with a different value (60s vs 300s) created false "not active" reports.
  if (!bus.active) {
    setStatus(`🔴 Bus ${busNo} is not active`);
  } else if (bus.lastSeen > 20) {
    setStatus(`🟡 Bus ${busNo} updating…`);
  } else {
    setStatus(`🟢 Bus ${busNo} (${bus.busType}) active`);
  }
}


// ═══════════════════════════════════════════════════════════════════
// SMART SEARCH  — unified route number + area search
//
// Flow:
//   1. Student types in #studentSearch
//   2. Debounced 250ms → fetchRouteSuggestions(query)
//      → GET /search_routes?q=<query>
//      → Returns [{route_no, route_area, is_active}]
//   3. renderSuggestions(list) builds the dropdown
//   4. onSuggestionClick(item):
//      → fills #studentSearch with "22 — Karayanchavadi"
//      → sets hidden #busNo to "22"
//      → stores selectedRoute for context
//
// loadBusLocations() reads busNoEl.value (= route_no) — UNCHANGED.
// ═══════════════════════════════════════════════════════════════════

let _searchDebounce = null;

studentSearchEl.addEventListener("input", () => {
  const q = studentSearchEl.value.trim();

  searchClearBtn.classList.toggle("visible", q.length > 0);

  if (!q) {
    clearSearchSelection();
    hideSearchDropdown();
    return;
  }

  // Invalidate stored selection if the field was edited after a pick
  if (selectedRoute && studentSearchEl.value !== formatSuggestionLabel(selectedRoute)) {
    selectedRoute = null;
    busNoEl.value = "";
  }

  clearTimeout(_searchDebounce);
  _searchDebounce = setTimeout(() => fetchRouteSuggestions(q), 250);
});

studentSearchEl.addEventListener("focus", () => {
  const q = studentSearchEl.value.trim();
  if (q) fetchRouteSuggestions(q);
});

studentSearchEl.addEventListener("blur", () => {
  setTimeout(hideSearchDropdown, 200);
});

searchClearBtn.addEventListener("click", () => {
  studentSearchEl.value = "";
  searchClearBtn.classList.remove("visible");
  clearSearchSelection();
  hideSearchDropdown();
  studentSearchEl.focus();
});

document.addEventListener("click", (e) => {
  if (
    !searchDropdownEl.contains(e.target) &&
    e.target !== studentSearchEl &&
    e.target !== searchClearBtn
  ) {
    hideSearchDropdown();
  }
});


async function fetchRouteSuggestions(query) {
  try {
    const url = `${BASE_URL}/search_routes?q=${encodeURIComponent(query)}`;
    const res = await fetch(url, { headers: { "Accept": "application/json" } });
    const data = await res.json();
    if (Array.isArray(data)) renderSuggestions(data, query);
    else hideSearchDropdown();
  } catch (err) {
    console.error("[fetchRouteSuggestions] error:", err);
    hideSearchDropdown();
  }
}


function renderSuggestions(items, query) {
  searchDropdownEl.innerHTML = "";

  if (items.length === 0) {
    searchDropdownEl.innerHTML =
      '<li class="search-dropdown-empty">No matching routes found</li>';
    _openSearchDropdown();
    return;
  }

  const active   = items.filter(i => i.is_active);
  const inactive = items.filter(i => !i.is_active);

  if (active.length > 0) {
    searchDropdownEl.appendChild(_sectionLabel("Active now"));
    active.forEach(item =>
      searchDropdownEl.appendChild(_buildSuggestionRow(item, query))
    );
  }

  if (inactive.length > 0) {
    if (active.length > 0) {
      searchDropdownEl.appendChild(_sectionLabel("All routes"));
    }
    inactive.forEach(item =>
      searchDropdownEl.appendChild(_buildSuggestionRow(item, query))
    );
  }

  _openSearchDropdown();
}


function _sectionLabel(text) {
  const li = document.createElement("li");
  li.className = "search-dropdown-section";
  li.textContent = text;
  li.setAttribute("aria-hidden", "true");
  return li;
}


function _buildSuggestionRow(item, query) {
  const li = document.createElement("li");
  li.className = `search-suggestion-item${item.is_active ? " active-bus" : ""}`;
  li.role = "option";
  li.setAttribute("aria-selected", "false");

  const areaHtml  = highlightSearchMatch(item.route_area || "", query);
  const liveBadge = item.is_active
    ? `<span class="search-live-badge">
         <span class="search-live-dot"></span>Live
       </span>`
    : "";

  li.innerHTML = `
    <span class="search-suggestion-num">${escHtml(item.route_no)}</span>
    <span class="search-suggestion-text">
      <span class="search-suggestion-area">${areaHtml || "<em>Unknown area</em>"}</span>
      <span class="search-suggestion-meta">Route ${escHtml(item.route_no)}</span>
    </span>
    ${liveBadge}
  `;

  li.addEventListener("mousedown", (e) => { e.preventDefault(); onSuggestionClick(item); });
  li.addEventListener("touchend",  (e) => { e.preventDefault(); onSuggestionClick(item); });
  return li;
}


function onSuggestionClick(item) {
  selectedRoute         = item;
  studentSearchEl.value = formatSuggestionLabel(item);
  busNoEl.value         = item.route_no;   // this is what loadBusLocations() reads

  // Route changed — clear morning fired set so thresholds can re-trigger
  morningFiredDistances.clear();

  searchClearBtn.classList.add("visible");
  hideSearchDropdown();
  studentSearchEl.blur();

  console.log(
    `[search] selected → route_no="${item.route_no}" ` +
    `area="${item.route_area}" active=${item.is_active}`
  );
}


function formatSuggestionLabel(item) {
  return item.route_area
    ? `${item.route_no} — ${item.route_area}`
    : item.route_no;
}

function clearSearchSelection() {
  selectedRoute = null;
  busNoEl.value = "";
}

function _openSearchDropdown() {
  studentSearchEl.setAttribute("aria-expanded", "true");
  searchDropdownEl.classList.add("open");
}

function hideSearchDropdown() {
  studentSearchEl.setAttribute("aria-expanded", "false");
  searchDropdownEl.classList.remove("open");
  searchDropdownEl.innerHTML = "";
}

function highlightSearchMatch(text, query) {
  if (!query || !text) return escHtml(text);
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return escHtml(text);
  return (
    escHtml(text.slice(0, idx)) +
    "<mark>" + escHtml(text.slice(idx, idx + query.length)) + "</mark>" +
    escHtml(text.slice(idx + query.length))
  );
}

function escHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}


// ─────────────────────────────────────────────
// MAP INIT
// ─────────────────────────────────────────────
function initMap(center) {
  mapboxgl.accessToken = "pk.eyJ1IjoiY29kZXMtMTE3IiwiYSI6ImNtbXRsNnAwazFza2UycXNkeTBsdHZqd2YifQ.BM0XjTixeeXYFc3S-Jrm5A";

  map = new mapboxgl.Map({
    container: "map",
    style:     "mapbox://styles/mapbox/streets-v11",
    center:    center || [80.2707, 13.0827],
    zoom:      15
  });

  map.on("click", (e) => {
    if (tripMode !== "evening") return;

    const { lng, lat } = e.lngLat;
    destination = { lat, lng };

    if (destinationMarker) destinationMarker.remove();

    destinationMarker = new mapboxgl.Marker({ color: "green" })
      .setLngLat([lng, lat])
      .addTo(map);

    // New destination pin — reset fired alerts so thresholds trigger again
    resetProximityFired();

    document.getElementById("proximityBox").classList.add("visible");
  });
}


// ─────────────────────────────────────────────
// STUDENT MARKER
// ─────────────────────────────────────────────
function placeStudentMarker(lat, lng) {
  if (isOnboard) return;

  if (!studentMarker) {
    studentMarker = new mapboxgl.Marker({ color: "blue" })
      .setLngLat([lng, lat])
      .addTo(map);
  } else {
    studentMarker.setLngLat([lng, lat]);
  }
}


// ─────────────────────────────────────────────
// HAVERSINE DISTANCE (metres)
// ─────────────────────────────────────────────
function getDistance(lat1, lon1, lat2, lon2) {
  const R    = 6371000;
  const toRad = deg => deg * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}


// ─────────────────────────────────────────────
// BUS MARKER
// ─────────────────────────────────────────────
function createBusMarker(route, lat, lng) {
  const el = document.createElement("div");
  el.className = "bus-marker-icon";    // styled via style.css — no inline styles
  el.innerHTML = `
    <i class="fa-solid fa-bus bus-marker-bus-icon"></i>
    <div class="bus-marker-label">${route}</div>
  `;
  return new mapboxgl.Marker(el)
    .setLngLat([lng, lat])
    .addTo(map);
}


// ─────────────────────────────────────────────
// SMOOTH MARKER ANIMATION
// ─────────────────────────────────────────────
function animateMarker(route, newLat, newLng) {
  const marker = busMarkers[route];
  if (!marker) return;

  const start     = lastBusPosition[route] || marker.getLngLat();
  const startLng  = start.lng;
  const startLat  = start.lat;
  const duration  = 1000;
  const startTime = performance.now();

  function animate(time) {
    const progress = Math.min((time - startTime) / duration, 1);
    marker.setLngLat([
      startLng + (newLng - startLng) * progress,
      startLat + (newLat - startLat) * progress
    ]);
    if (progress < 1) requestAnimationFrame(animate);
  }

  requestAnimationFrame(animate);
  lastBusPosition[route] = { lat: newLat, lng: newLng };
}


// ─────────────────────────────────────────────
// EVENING REMINDER ALERTS
// ─────────────────────────────────────────────
// checkDestinationReminder replaced by checkProximityAlerts() above


// ═══════════════════════════════════════════════════════════════════
// SMART POLLING  — dynamic interval + once-trigger onboard detection
// ═══════════════════════════════════════════════════════════════════

/**
 * Decide whether to run the full onboard/proximity/polling update.
 *
 * Returns true only when the bus-to-student distance has changed by
 * at least 10 metres since the last check. This filters out GPS jitter
 * (the device reports tiny position changes even when stationary) and
 * avoids wasting CPU on Haversine + DOM updates every 2–5 seconds when
 * nothing meaningful has moved.
 *
 * Always returns true on the very first call (lastDistance === null).
 *
 * @param {number} distance — current bus-to-student distance in metres
 * @returns {boolean}
 */
function shouldRecalculate(distance) {
  if (lastDistance === null) {
    lastDistance = distance;
    return true;
  }
  if (Math.abs(distance - lastDistance) >= 10) {
    lastDistance = distance;
    return true;
  }
  return false;
}

/**
 * Adjust the polling interval based on how close the bus is.
 * Clears and restarts pollingTimer only when the interval actually changes,
 * avoiding unnecessary timer churn.
 *
 * Distance bands:
 *   > 1000 m  →  15 s  (bus is far — save bandwidth + battery)
 *   100–1000 m →  5 s  (approaching — normal tracking)
 *   < 100 m   →  2 s  (very close — high-frequency for onboard detection)
 *
 * @param {number} distance — current bus-to-student distance in metres
 */
function updatePolling(distance) {
  const newInterval =
    distance > 1000 ? POLL_FAR  :
    distance > 100  ? POLL_MID  :
                      POLL_NEAR ;

  if (newInterval === pollingInterval) return;   // no change — leave timer alone

  console.log(
    `[polling] interval ${pollingInterval / 1000}s → ${newInterval / 1000}s ` +
    `(dist=${distance.toFixed(0)}m)`
  );

  pollingInterval = newInterval;
  clearInterval(pollingTimer);
  pollingTimer = setInterval(_pollTick, pollingInterval);
}

/**
 * Called when the bus is found and active in loadBusLocations.
 * Orchestrates the three distance-based behaviours:
 *   1. shouldRecalculate — skip if position barely changed
 *   2. checkOnboard      — once-trigger boarding detection (morning)
 *   3. updatePolling     — dynamic fetch interval adjustment
 *
 * @param {number} distance      — bus-to-student metres
 * @param {number} studentLat
 * @param {number} studentLng
 * @param {object} matchingBus   — bus record from /get_locations
 * @param {string} busNo         — normalised route number
 */
function handleLocationUpdate(distance, studentLat, studentLng, matchingBus, busNo) {
  if (!shouldRecalculate(distance)) return;   // skip — nothing meaningful changed

  // ── Once-trigger onboard detection (morning mode only) ──
  if (
    tripMode === "morning" &&
    !onboardTriggered &&
    typeof studentLat === "number" &&
    typeof studentLng === "number" &&
    distance <= 20
  ) {
    onboardTriggered = true;
    isOnboard        = true;

    fetch(`${BASE_URL}/onboard`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rollNo:   rollNoEl.value,
        busRoute: busNo,
        onboard:  true
      })
    }).catch(err => console.error("[onboard] POST failed:", err));

    if (studentMarker) {
      studentMarker.remove();
      studentMarker = null;
    }

    setStatus(`🟢 ONBOARD Bus ${busNo} (${matchingBus.busType})`);
    console.log(`[onboard] triggered once — dist=${distance.toFixed(0)}m`);
  }

  // ── Dynamic polling interval ──
  updatePolling(distance);
}

/**
 * Reset all polling and onboard state.
 * Called each time the student clicks "Show Live Map" so a fresh
 * tracking session starts cleanly.
 */
function resetPollingState() {
  onboardTriggered = false;
  lastDistance     = null;
  pollingInterval  = POLL_DEFAULT;
  clearInterval(pollingTimer);
  pollingTimer     = null;
  isOnboard        = false;
  // Clear morning fired set so all thresholds can fire again this session
  morningFiredDistances.clear();
  console.log("[polling] state reset");
}


// ─────────────────────────────────────────────
// FETCH BUS LOCATIONS
//
// Route number is normalised to UPPERCASE before comparison,
// matching backend _normalize_route() and driver-side .toUpperCase().
// Logs the full API response to the console for easy debugging.
//
// This function is called by _pollTick() (the dynamic timer).
// Student lat/lng come from _lastStudentLat/_lastStudentLng,
// updated by watchPosition in the showBtn listener.
// ─────────────────────────────────────────────

/**
 * Last known student position, updated by watchPosition.
 * Used by _pollTick so the timer doesn't need access to GPS callbacks.
 */
let _lastStudentLat = null;
let _lastStudentLng = null;

/** Single polling tick — reads the last known student position. */
function _pollTick() {
  loadBusLocations(_lastStudentLat, _lastStudentLng);
}

async function loadBusLocations(studentLat, studentLng) {
  // Normalise exactly as backend does: strip + uppercase
  const busNo = busNoEl.value.trim().toUpperCase();
  const url   = `${BASE_URL}/get_locations`;

  let response;
  let data;

  // ── Step 1: network fetch ──
  try {
    response = await fetch(url, {
      method:  "GET",
      headers: { "Accept": "application/json" }
    });
  } catch (networkErr) {
    console.error("[loadBusLocations] Network error →", networkErr);
    setStatus("⚠️ Cannot reach server — check your connection");
    return;
  }

  // ── Step 2: HTTP status check ──
  if (!response.ok) {
    console.error(`[loadBusLocations] HTTP ${response.status} from ${url}`);
    setStatus(`⚠️ Server error (${response.status}) — please try again`);
    return;
  }

  // ── Step 3: JSON parse ──
  try {
    data = await response.json();
  } catch (parseErr) {
    console.error("[loadBusLocations] Not valid JSON from", url, "→", parseErr);
    setStatus("⚠️ Unexpected server response — please try again");
    return;
  }

  // ── Step 4: Log full response ──
  console.log(`[loadBusLocations] full API response (${data.length} record(s)):`, data);

  if (!Array.isArray(data)) {
    console.warn("[loadBusLocations] unexpected shape:", data);
    setStatus("⚠️ Unexpected data from server");
    return;
  }

  // ── Step 5: find matching bus ──
  const matchingBus = data.find(b => String(b.route).trim().toUpperCase() === busNo);

  console.log(
    `[loadBusLocations] looking for bus="${busNo}" — ` +
    (matchingBus
      ? `found → active=${matchingBus.active} lastSeen=${matchingBus.lastSeen}s timestamp=${matchingBus.timestamp}`
      : `NOT FOUND in response (routes present: [${data.map(b => b.route).join(", ")}])`)
  );

  // ── Step 6: handle result ──
  if (!matchingBus) {
    if (!isOnboard) setStatus(`🔴 Bus ${busNo} — no trip started yet`);
    return;
  }

  if (!matchingBus.active) {
    if (busMarkers[matchingBus.route]) {
      busMarkers[matchingBus.route].remove();
      delete busMarkers[matchingBus.route];
    }
    if (!isOnboard) {
      const ago = matchingBus.lastSeen != null ? ` (last seen ${matchingBus.lastSeen}s ago)` : "";
      setStatus(`🔴 Bus ${busNo} is not active${ago}`);
    }
    return;
  }

  // ── Bus is active — place or animate marker ──
  if (!busMarkers[matchingBus.route]) {
    busMarkers[matchingBus.route] = createBusMarker(
      matchingBus.route, matchingBus.lat, matchingBus.lng
    );
  } else {
    animateMarker(matchingBus.route, matchingBus.lat, matchingBus.lng);
  }

  if (!isOnboard) updateBusStatus(matchingBus, busNo);

  // Evening mode: bus-to-destination proximity alerts
  if (tripMode === "evening") checkProximityAlerts(matchingBus);

  // Morning mode: bus-to-student pickup alerts
  if (tripMode === "morning") {
    checkMorningProximityAlerts(matchingBus, studentLat, studentLng);
  }

  // ── Smart polling + once-trigger onboard detection ──
  // Delegates to handleLocationUpdate() which gates all work behind
  // shouldRecalculate() — skips redundant computation on minor GPS jitter.
  if (
    typeof studentLat === "number" &&
    typeof studentLng === "number" &&
    matchingBus.lat != null &&
    matchingBus.lng != null &&
    !isOnboard
  ) {
    const dist = getDistance(studentLat, studentLng, matchingBus.lat, matchingBus.lng);
    handleLocationUpdate(dist, studentLat, studentLng, matchingBus, busNo);
  }
}


// ─────────────────────────────────────────────
// SHOW MAP BUTTON
// ─────────────────────────────────────────────
showBtn.addEventListener("click", () => {
  const roll   = rollNoEl.value.trim();
  const busNo  = busNoEl.value.trim();
  const search = studentSearchEl.value.trim();

  if (!roll) {
    alert("Enter your Roll Number");
    return;
  }

  if (!busNo) {
    if (search) {
      alert("Select a route from the suggestions below your search.");
    } else {
      alert("Search for your bus route first.");
    }
    studentSearchEl.focus();
    return;
  }

  mapEl.style.display = "block";

  if (!map) initMap([80.2707, 13.0827]);
  map.resize();

  // Reset all polling and onboard state for a clean tracking session
  resetPollingState();

  // ── GPS: update student position + marker only ──
  // fetchBusLocations is NOT called here — the polling timer owns all fetches.
  // This eliminates the race where watchPosition and setInterval fire
  // almost simultaneously and double-hit /get_locations.
  navigator.geolocation.watchPosition(
    pos => {
      const { latitude, longitude } = pos.coords;
      _lastStudentLat = latitude;
      _lastStudentLng = longitude;
      map.setCenter([longitude, latitude]);
      placeStudentMarker(latitude, longitude);
    },
    err => {
      console.error("[geolocation] error:", err);
      alert("Location error: " + err.message);
    },
    { enableHighAccuracy: true, maximumAge: 1000 }
  );

  // ── Fetch immediately, then start the dynamic polling timer ──
  // Fire one fetch right away so the student doesn't wait up to
  // POLL_DEFAULT seconds to see the first result.
  _pollTick();
  pollingTimer = setInterval(_pollTick, pollingInterval);
  console.log(`[polling] started — initial interval ${pollingInterval / 1000}s`);
});
