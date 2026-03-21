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
let map              = null;
let busMarkers       = {};
let studentMarker    = null;
let destinationMarker = null;
let isOnboard        = false;
let lastBusPosition  = {};
let destination      = null;
let tripMode         = "morning";

let reminders = { km2: false, km1: false, m500: false };


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
const reminderBox      = document.getElementById("reminderBox");
const rem2km           = document.getElementById("rem2km");
const rem1km           = document.getElementById("rem1km");
const rem500m          = document.getElementById("rem500m");


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

rem2km?.addEventListener("change",  () => { reminders.km2  = rem2km.checked;  });
rem1km?.addEventListener("change",  () => { reminders.km1  = rem1km.checked;  });
rem500m?.addEventListener("change", () => { reminders.m500 = rem500m.checked; });


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

    document.getElementById("reminderBox").classList.add("visible");
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
function checkDestinationReminder(bus) {
  if (!destination) return;
  const dist = getDistance(bus.lat, bus.lng, destination.lat, destination.lng);

  if (reminders.km2 && dist <= 2000) {
    alert("Bus is 2 km from your stop");
    reminders.km2 = false;
  }
  if (reminders.km1 && dist <= 1000) {
    alert("Bus is 1 km from your stop");
    reminders.km1 = false;
  }
  if (reminders.m500 && dist <= 500) {
    alert("Bus is 500 m from your stop");
    reminders.m500 = false;
  }
}


// ─────────────────────────────────────────────
// FETCH BUS LOCATIONS
//
// Route number is normalised to UPPERCASE before comparison,
// matching backend _normalize_route() and driver-side .toUpperCase().
// Logs the full API response to the console for easy debugging.
// ─────────────────────────────────────────────
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

  // ── Step 4: Log full response (visible in browser DevTools console) ──
  console.log(`[loadBusLocations] full API response (${data.length} record(s)):`, data);

  if (!Array.isArray(data)) {
    console.warn("[loadBusLocations] unexpected shape:", data);
    setStatus("⚠️ Unexpected data from server");
    return;
  }

  // ── Step 5: find matching bus ──
  // Both sides normalised: backend stores UPPERCASE, we compare UPPERCASE
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

  // ── Bus is active ──
  if (!busMarkers[matchingBus.route]) {
    busMarkers[matchingBus.route] = createBusMarker(
      matchingBus.route, matchingBus.lat, matchingBus.lng
    );
  } else {
    animateMarker(matchingBus.route, matchingBus.lat, matchingBus.lng);
  }

  if (!isOnboard) updateBusStatus(matchingBus, busNo);

  if (tripMode === "evening") checkDestinationReminder(matchingBus);

  // ── Onboard detection (morning mode) ──
  if (
    typeof studentLat === "number" &&
    typeof studentLng === "number" &&
    !isOnboard &&
    tripMode === "morning"
  ) {
    const dist = getDistance(studentLat, studentLng, matchingBus.lat, matchingBus.lng);

    if (dist <= 20) {
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

      isOnboard = true;
      setStatus(`🟢 ONBOARD Bus ${busNo} (${matchingBus.busType})`);
    }
  }
}


// ─────────────────────────────────────────────
// SHOW MAP BUTTON
// ─────────────────────────────────────────────
showBtn.addEventListener("click", () => {
  const roll   = rollNoEl.value.trim();
  const busNo  = busNoEl.value.trim();          // resolved route_no (hidden field)
  const search = studentSearchEl.value.trim();  // what the student typed / selected

  if (!roll) {
    alert("Enter your Roll Number");
    return;
  }

  if (!busNo) {
    // Student typed something but didn't pick from the dropdown
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

  isOnboard = false;

  navigator.geolocation.watchPosition(
    pos => {
      const { latitude, longitude } = pos.coords;
      map.setCenter([longitude, latitude]);
      placeStudentMarker(latitude, longitude);
      loadBusLocations(latitude, longitude);
    },
    err => {
      console.error("[geolocation] error:", err);
      alert("Location error: " + err.message);
    },
    { enableHighAccuracy: true, maximumAge: 1000 }
  );

  // Poll every 5 seconds using the student marker position
  setInterval(() => {
    if (studentMarker) {
      const pos = studentMarker.getLngLat();
      loadBusLocations(pos.lat, pos.lng);
    } else {
      // Still poll even without a student marker (e.g. GPS denied)
      loadBusLocations(null, null);
    }
  }, 5000);
});
