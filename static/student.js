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
const rollNoEl        = document.getElementById("rollNo");
const busNoEl         = document.getElementById("busNo");
const showBtn         = document.getElementById("showMap");
const mapEl           = document.getElementById("map");
const selectDestBtn   = document.getElementById("selectDestination");
const destinationBlock = document.getElementById("destinationBlock");
const reminderBox     = document.getElementById("reminderBox");
const rem2km          = document.getElementById("rem2km");
const rem1km          = document.getElementById("rem1km");
const rem500m         = document.getElementById("rem500m");


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
// setStatus is overridden in student.html to use the styled bar.
// This default is a safe fallback.
// ─────────────────────────────────────────────
function setStatus(text) {
  const el = document.getElementById("statusMsg");
  if (el) el.textContent = text;
}


// ─────────────────────────────────────────────
// BUS STATUS TEXT
// ─────────────────────────────────────────────
function updateBusStatus(bus, busNo) {
  if (!bus.active || bus.lastSeen > 60) {
    setStatus(`🔴 Bus ${busNo} is not active`);
  } else if (bus.lastSeen > 20) {
    setStatus(`🟡 Bus ${busNo} updating…`);
  } else {
    setStatus(`🟢 Bus ${busNo} (${bus.busType}) active`);
  }
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
// FETCH BUS LOCATIONS  ← main fix
//
// Uses BASE_URL so it works on Render cross-origin deployments.
// Distinguishes three failure modes with specific log messages:
//   1. Network failure  — fetch() rejects (no connection / CORS block)
//   2. HTTP error       — server responded with non-2xx status
//   3. Parse error      — server returned non-JSON (usually a 500 HTML page)
// ─────────────────────────────────────────────
async function loadBusLocations(studentLat, studentLng) {
  const busNo = busNoEl.value.trim();
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
    // Network failure: no connection, DNS error, CORS preflight rejected, etc.
    console.error("[loadBusLocations] Network error fetching", url, "→", networkErr);
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
    // Server returned non-JSON (HTML error page, proxy timeout page, etc.)
    console.error("[loadBusLocations] Response is not valid JSON from", url, "→", parseErr);
    setStatus("⚠️ Unexpected server response — please try again");
    return;
  }

  // ── Step 4: process bus data ──
  if (!Array.isArray(data)) {
    console.warn("[loadBusLocations] Unexpected data shape:", data);
    setStatus("⚠️ Unexpected data from server");
    return;
  }

  // If no buses at all, show a neutral status
  const matchingBus = data.find(b => String(b.route) === String(busNo));
  if (!matchingBus && !isOnboard) {
    setStatus(`🔴 Bus ${busNo} is not active`);
  }

  data.forEach(bus => {
    if (String(bus.route) !== String(busNo)) return;

    if (!bus.active) {
      if (busMarkers[bus.route]) {
        busMarkers[bus.route].remove();
        delete busMarkers[bus.route];
      }
      if (!isOnboard) setStatus(`🔴 Bus ${busNo} is not active`);
      return;
    }

    // Place or animate marker
    if (!busMarkers[bus.route]) {
      busMarkers[bus.route] = createBusMarker(bus.route, bus.lat, bus.lng);
    } else {
      animateMarker(bus.route, bus.lat, bus.lng);
    }

    if (!isOnboard) updateBusStatus(bus, busNo);

    if (tripMode === "evening") checkDestinationReminder(bus);

    // Onboard detection (morning mode only)
    if (
      typeof studentLat === "number" &&
      typeof studentLng === "number" &&
      !isOnboard &&
      tripMode === "morning"
    ) {
      const dist = getDistance(studentLat, studentLng, bus.lat, bus.lng);

      if (dist <= 20) {
        // POST to /onboard — now a real endpoint
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
        setStatus(`🟢 ONBOARD Bus ${busNo} (${bus.busType})`);
      }
    }
  });
}


// ─────────────────────────────────────────────
// SHOW MAP BUTTON
// ─────────────────────────────────────────────
showBtn.addEventListener("click", () => {
  const roll = rollNoEl.value.trim();
  const bus  = busNoEl.value.trim();

  if (!roll || !bus) {
    alert("Enter Roll No & Bus No.");
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
