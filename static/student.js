let map;
let busMarkers = {};
let studentMarker = null;
let studentWatchId = null;

// Flags
let isOnboard = false;
let notified2km = false;
let notified1km = false;
let notified500m = false;

const rollNoEl = document.getElementById('rollNo');
const busNoEl = document.getElementById('busNo');
const showBtn = document.getElementById('showMap');
const mapEl = document.getElementById('map');

function initMap(center) {
  mapboxgl.accessToken = 'YOUR_MAPBOX_ACCESS_TOKEN';
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

// Proximity notifications
function checkProximity(studentLat, studentLng, busLat, busLng, route) {
  const dist = getDistance(studentLat, studentLng, busLat, busLng);

  if (dist <= 2000 && !notified2km) {
    alert(`Bus ${route} is within 2 km of you!`);
    notified2km = true;
  }
  if (dist <= 1000 && !notified1km) {
    alert(`Bus ${route} is within 1 km of you!`);
    notified1km = true;
  }
  if (dist <= 500 && !notified500m) {
    alert(`Bus ${route} is within 500 meters of you!`);
    notified500m = true;
  }
}

function fetchBusLocations(studentLat, studentLng) {
  const busNo = busNoEl.value?.trim();
  fetch("/get_locations")
    .then(r => r.json())
    .then(items => {
      items.forEach(bus => {
        if (busNo && busNo !== bus.route && busNo !== String(bus.route)) {
          return;
        }
        const key = bus.route + "-" + bus.busType;
        if (!busMarkers[key]) {
          busMarkers[key] = createBusMarker(bus.route, bus.lat, bus.lng);
        } else {
          busMarkers[key].setLngLat([bus.lng, bus.lat]);
        }

        if (studentLat && studentLng) {
          // Proximity notifications
          checkProximity(studentLat, studentLng, bus.lat, bus.lng, bus.route);

          // Onboard logic
          const dist = getDistance(studentLat, studentLng, bus.lat, bus.lng);
          if (dist <= 5 && !isOnboard) {
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

            isOnboard = true; // prevent re-adding marker
          }
        }
      });
    })
    .catch(() => {});
}

showBtn.addEventListener('click', () => {
  const roll = rollNoEl.value?.trim();
  const bus = busNoEl.value?.trim();

  if (!roll || !bus) {
    alert("Enter Roll Number and Bus Number.");
    return;
  }

  // Reset flags for new session
  isOnboard = false;
  notified2km = false;
  notified1km = false;
  notified500m = false;

  mapEl.style.display = 'block';

  // Start watching student location
  studentWatchId = navigator.geolocation.watchPosition(
    pos => {
      const { latitude, longitude } = pos.coords;
      if (!map) initMap([longitude, latitude]);
      placeStudentMarker(latitude, longitude);

      fetchBusLocations(latitude, longitude);
    },
    err => {
      if (!map) initMap([80.2707, 13.0827]);
      alert("Unable to get your location: " + err.message);
      fetchBusLocations();
    },
    { enableHighAccuracy: true, maximumAge: 1000 }
  );

  // Poll bus locations every 5 seconds
  setInterval(() => {
    if (studentMarker) {
      const coords = studentMarker.getLngLat();
      fetchBusLocations(coords.lat, coords.lng);
    } else {
      fetchBusLocations();
    }
  }, 1000);
});