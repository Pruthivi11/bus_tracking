let map;
let busMarkers = {};
let studentMarker = null;

const rollNoEl = document.getElementById('rollNo');
const busNoEl = document.getElementById('busNo');
const showBtn = document.getElementById('showMap');
const mapEl = document.getElementById('map');

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
  if (!studentMarker) {
    studentMarker = new mapboxgl.Marker({ color: "blue" })
      .setLngLat([lng, lat])
      .setPopup(new mapboxgl.Popup().setText("Your Location"))
      .addTo(map);
  } else {
    studentMarker.setLngLat([lng, lat]);
  }
}

function fetchBusLocations() {
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
          busMarkers[key] = new mapboxgl.Marker({ color: "red" })
            .setLngLat([bus.lng, bus.lat])
            .setPopup(new mapboxgl.Popup().setText(`Route: ${bus.route}, Type: ${bus.busType}`))
            .addTo(map);
        } else {
          busMarkers[key].setLngLat([bus.lng, bus.lat]);
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

  mapEl.style.display = 'block';

  navigator.geolocation.getCurrentPosition(
    pos => {
      const { latitude, longitude } = pos.coords;
      if (!map) initMap([longitude, latitude]);
      placeStudentMarker(latitude, longitude);

      fetchBusLocations();
      setInterval(fetchBusLocations, 5000);
    },
    err => {
      if (!map) initMap([80.2707, 13.0827]);
      alert("Unable to get your location: " + err.message);
      fetchBusLocations();
      setInterval(fetchBusLocations, 3000);
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
});