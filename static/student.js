let map;
let busMarkers = {};
let studentMarker = null;
let studentWatchId = null;
let isOnboard = false;

const rollNoEl = document.getElementById('rollNo');
const busNoEl = document.getElementById('busNo');
const showBtn = document.getElementById('showMap');
const mapEl = document.getElementById('map');
const statusEl = document.getElementById('statusMsg');

function initMap(center) {
  mapboxgl.accessToken = 'pk.eyJ1IjoiY29kZXMtMTE3IiwiYSI6ImNta2Y2dzhwdjBnNjAzaHF6Y2tydXY2aXgifQ.Ss1FmjnHljaQc7BgTDvZSQ'; // replace with your real token

  map = new mapboxgl.Map({
    container: 'map',
    style: 'mapbox://styles/mapbox/streets-v11',
    center: center || [80.2707, 13.0827],
    zoom: 15
  });
}

function placeStudentMarker(lat, lng) {
  if (isOnboard) return;

  if (!studentMarker) {
    studentMarker = new mapboxgl.Marker({ color: "blue" })
      .setLngLat([lng, lat])
      .setPopup(new mapboxgl.Popup().setText("Your Location"))
      .addTo(map);
  } else {
    studentMarker.setLngLat([lng, lat]);
  }
}

// Haversine distance (meters)
function getDistance(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const toRad = deg => deg * Math.PI / 180;

  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);

  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRad(lat1)) *
      Math.cos(toRad(lat2)) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);

  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

// Create custom bus marker
function createBusMarker(route, lat, lng) {
  const el = document.createElement('div');
  el.className = 'bus-marker';
  el.innerHTML = `
    <i class="fa-solid fa-bus" style="color:red;font-size:20px;"></i>
    <div style="font-size:12px;text-align:center;">${route}</div>
  `;

  return new mapboxgl.Marker(el)
    .setLngLat([lng, lat])
    .setPopup(new mapboxgl.Popup().setText(`Bus ${route}`))
    .addTo(map);
}

function fetchBusLocations(studentLat, studentLng) {
  const busNo = busNoEl.value?.trim();

  fetch("/get_locations")
    .then(r => r.json())
    .then(items => {

      // Clear old markers
      Object.values(busMarkers).forEach(m => m.remove());
      busMarkers = {};

      if (!busNo) {
        statusEl.textContent = "No bus number entered.";
        return;
      }

      const bus = items.find(
        b => String(b.route) === String(busNo)
      );

      if (!bus) {
        statusEl.textContent = `Bus ${busNo} is not active.`;
        return;
      }

      // Show bus marker
      const key = bus.route + "-" + bus.busType;
      busMarkers[key] = createBusMarker(bus.route, bus.lat, bus.lng);

      statusEl.textContent = `Bus ${busNo} (${bus.busType}) is active.`;

      // -----------------------------
      // ✅ FIXED ONBOARD LOGIC
      // -----------------------------
      if (
        typeof studentLat === "number" &&
        typeof studentLng === "number" &&
        !isOnboard
      ) {
        const dist = getDistance(
          studentLat,
          studentLng,
          bus.lat,
          bus.lng
        );

        console.log("Distance to bus:", dist);

        // realistic GPS threshold
        if (dist <= 20) {

          fetch("/onboard", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({
              rollNo: rollNoEl.value,
              busRoute: busNoEl.value,
              onboard: true
            })
          });

          if (studentMarker) {
            studentMarker.remove();
            studentMarker = null;
          }

          isOnboard = true;
          statusEl.textContent = `ONBOARD Bus ${busNo}`;
        }
      }
    })
    .catch(() => {
      statusEl.textContent = "Failed to fetch bus locations.";
    });
}

// -----------------------------
// SHOW MAP BUTTON
// -----------------------------
showBtn.addEventListener('click', () => {
  const roll = rollNoEl.value?.trim();
  const bus = busNoEl.value?.trim();

  if (!roll || !bus) {
    alert("Enter Roll Number and Bus Number.");
    return;
  }

  mapEl.style.display = 'block';

  if (!map) initMap([80.2707, 13.0827]);
  map.resize();

  isOnboard = false;

  if (!navigator.geolocation) {
    alert("Geolocation not supported.");
    return;
  }

  // Watch student location continuously
  studentWatchId = navigator.geolocation.watchPosition(
    pos => {
      const { latitude, longitude } = pos.coords;

      map.setCenter([longitude, latitude]);
      placeStudentMarker(latitude, longitude);
      fetchBusLocations(latitude, longitude);
    },
    err => {
      alert("Unable to get location: " + err.message);
      fetchBusLocations();
    },
    {
      enableHighAccuracy: true,
      maximumAge: 1000
    }
  );
});
