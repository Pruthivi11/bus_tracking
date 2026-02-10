let map;
let busMarkers = {};
let studentMarker = null;
let studentWatchId = null;
let pollInterval = null;
let isOnboard = false;

const rollNoEl = document.getElementById('rollNo');
const busNoEl = document.getElementById('busNo');
const showBtn = document.getElementById('showMap');
const mapEl = document.getElementById('map');

// Message area for feedback
const statusEl = document.getElementById('statusMsg');

function initMap(center) {
  mapboxgl.accessToken = 'pk.eyJ1IjoiY29kZXMtMTE3IiwiYSI6ImNta2Y2dzhwdjBnNjAzaHF6Y2tydXY2aXgifQ.Ss1FmjnHljaQc7BgTDvZSQ'; // replace with your real token
  map = new mapboxgl.Map({
    container: 'map',
    style: 'mapbox://styles/mapbox/streets-v11',
    center: center || [80.2707, 13.0827],
    zoom: 14
  });
}

function placeStudentMarker(lat, lng) {
  if (isOnboard) return; // don't place marker if onboard
  if (!studentMarker) {
    studentMarker = new mapboxgl.Marker({ color: "blue" })
      .setLngLat([lng, lat])
      .setPopup(new mapboxgl.Popup().setText("Your Location"))
      .addTo(map);
  } else {
    studentMarker.setLngLat([lng, lat]);
  }
}

// Haversine formula for distance in meters
function getDistance(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const toRad = deg => deg * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
            Math.sin(dLon/2) * Math.sin(dLon/2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
  return R * c;
}

// Custom bus marker with Font Awesome icon + route number
function createBusMarker(route, lat, lng) {
  const el = document.createElement('div');
  el.className = 'bus-marker';
  el.innerHTML = `<i class="fa-solid fa-bus" style="color:red;font-size:20px;"></i>
                  <div class="route-label">${route}</div>`;
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
      // Clear old bus markers
      Object.values(busMarkers).forEach(m => m.remove());
      busMarkers = {};

      if (busNo) {
        // Look for the specific bus
        const bus = items.find(b => b.route === busNo || String(b.route) === busNo);

        if (bus) {
          // ✅ Guard against undefined busType
          const busTypeText = bus.busType ? bus.busType : "Unknown";
          statusEl.textContent = `Bus ${bus.route} (${busTypeText}) is active.`;

          const key = bus.route + "-" + bus.busType;
          busMarkers[key] = createBusMarker(bus.route, bus.lat, bus.lng);

          // Check distance for onboard logic
          if (studentLat && studentLng && !isOnboard) {
            const dist = getDistance(studentLat, studentLng, bus.lat, bus.lng);
            if (dist <= 5) {
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
              statusEl.textContent = `You are onboard Bus ${busNo}.`;
            }
          }
        } else {
          // Bus not active → only show student location
          statusEl.textContent = `Bus ${busNo} is not active. Showing only your location.`;
        }
      } else {
        // No bus number entered → only show student location
        statusEl.textContent = "No bus number entered. Showing only your location.";
      }
    })
    .catch(err => {
      console.error("Error fetching bus locations:", err);
      statusEl.textContent = "Failed to fetch bus locations.";
    });
}

showBtn.addEventListener('click', () => {
  const roll = rollNoEl.value?.trim();
  const bus = busNoEl.value?.trim();

  if (!roll || !bus) {
    alert("Enter Roll Number and Bus Number.");
    return;
  }

  mapEl.style.display = 'block';

  // Initialize map immediately
  if (!map) initMap([80.2707, 13.0827]);
  map.resize();

  // Clear any old polling loop
  if (pollInterval) clearInterval(pollInterval);

  // Reset flags
  isOnboard = false;

  // Start watching student location
  studentWatchId = navigator.geolocation.watchPosition(
    pos => {
      const { latitude, longitude } = pos.coords;
      map.setCenter([longitude, latitude]);
      placeStudentMarker(latitude, longitude);
      fetchBusLocations(latitude, longitude);
    },
    err => {
      alert("Unable to get your location: " + err.message);
      fetchBusLocations();
    },
    { enableHighAccuracy: true, maximumAge: 1000 }
  );

  // Poll bus locations every second
  pollInterval = setInterval(() => {
    if (studentMarker) {
      const coords = studentMarker.getLngLat();
      fetchBusLocations(coords.lat, coords.lng);
    } else {
      fetchBusLocations();
    }
  }, 1000);
});