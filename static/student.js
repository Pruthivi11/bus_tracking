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
const sessionToggle = document.getElementById('sessionToggle');
const destinationField = document.getElementById('destinationField');
const remainderOptions = document.getElementById('remainderOptions');
const destinationLabel = document.getElementById('destinationLabel');
let destinationCoords = null;

// Toggle logic
sessionToggle.addEventListener('change', () => {
  if (sessionToggle.value === 'evening') {
    destinationField.style.display = 'block';
    remainderOptions.style.display = 'block';
  } else {
    destinationField.style.display = 'none';
    remainderOptions.style.display = 'none';
    destinationCoords = null;
  }
});
// Trigger once on load
sessionToggle.dispatchEvent(new Event('change'));

function initMap(center) {
  mapboxgl.accessToken = 'pk.eyJ1IjoiY29kZXMtMTE3IiwiYSI6ImNta2Y2dzhwdjBnNjAzaHF6Y2tydXY2aXgifQ.Ss1FmjnHljaQc7BgTDvZSQ';
  map = new mapboxgl.Map({
    container: 'map',
    style: 'mapbox://styles/mapbox/streets-v11',
    center: center || [80.2707, 13.0827],
    zoom: 14
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

function createBusMarker(route, lat, lng) {
  const el = document.createElement('div');
  el.className = 'bus-marker';
  el.innerHTML = `<i class="fa-solid fa-bus"></i>
                  <div class="route-label">${route}</div>`;
  return new mapboxgl.Marker(el)
    .setLngLat([lng, lat])
    .setPopup(new mapboxgl.Popup().setText(`Bus ${route}`))
    .addTo(map);
}

document.getElementById('selectDestination').addEventListener('click', () => {
  alert("Click on the map to set your destination.");
  map.once('click', (e) => {
    destinationCoords = e.lngLat;
    destinationLabel.textContent = `Destination set: ${destinationCoords.lat.toFixed(5)}, ${destinationCoords.lng.toFixed(5)}`;
  });
});

function checkProximity(studentLat, studentLng, busLat, busLng, route) {
  let targetLat, targetLng;
  if (sessionToggle.value === 'morning') {
    targetLat = studentLat;
    targetLng = studentLng;
  } else if (sessionToggle.value === 'evening' && destinationCoords) {
    targetLat = destinationCoords.lat;
    targetLng = destinationCoords.lng;
  } else {
    return;
  }

  const dist = getDistance(targetLat, targetLng, busLat, busLng);

  if (dist <= 2000 && !notified2km && document.getElementById('rem2km').checked) {
    alert(`Bus ${route} is within 2 km!`);
    notified2km = true;
  }
  if (dist <= 1000 && !notified1km && document.getElementById('rem1km').checked) {
    alert(`Bus ${route} is within 1 km!`);
    notified1km = true;
  }
  if (dist <= 500 && !notified500m && document.getElementById('rem500m').checked) {
    alert(`Bus ${route} is within 500 m!`);
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
          checkProximity(studentLat, studentLng, bus.lat, bus.lng, bus.route);

          if (sessionToggle.value === 'morning') {
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

              isOnboard = true;
            }
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

  isOnboard = false;
  notified2km = false;
  notified1km = false;
  notified500m = false;

  mapEl.style.display = 'block';

  if (!map) initMap([80.2707, 13.0827]);
  map.resize();

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

  setInterval(() => {
    if (studentMarker) {
      const coords = studentMarker.getLngLat();
      fetchBusLocations(coords.lat, coords.lng);
    } else {
      fetchBusLocations();
    }
  }, 1000);
});