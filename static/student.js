let map, busMarkers = {}, studentMarker = null, studentWatchId = null, pollInterval = null, isOnboard = false;
const rollNoEl = document.getElementById("rollNo"),
      busNoEl = document.getElementById("busNo"),
      showBtn = document.getElementById("showMap"),
      mapEl = document.getElementById("map"),
      statusEl = document.getElementById("statusMsg");

function initMap(c) {
  mapboxgl.accessToken = "pk.eyJ1IjoiY29kZXMtMTE3IiwiYSI6ImNta2Y2dzhwdjBnNjAzaHF6Y2tydXY2aXgifQ.Ss1FmjnHljaQc7BgTDvZSQ"; // replace with your real token
  map = new mapboxgl.Map({
    container: "map",
    style: "mapbox://styles/mapbox/streets-v11",
    center: c || [80.2707, 13.0827],
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

// Haversine distance in meters
function dist(a, b, c, d) {
  const R = 6371000, t = x => x * Math.PI / 180;
  const A = Math.sin(t(c - a) / 2) ** 2 +
            Math.cos(t(a)) * Math.cos(t(c)) *
            Math.sin(t(d - b) / 2) ** 2;
  return 2 * R * Math.atan2(Math.sqrt(A), Math.sqrt(1 - A));
}

function busMarker(r, lat, lng) {
  const e = document.createElement("div");
  e.innerHTML = `<i class="fa-solid fa-bus" style="color:red"></i><div>${r}</div>`;
  return new mapboxgl.Marker(e).setLngLat([lng, lat]).addTo(map);
}

function fetchBus(sl, sg) {
  fetch("/get_locations")
    .then(r => {
      if (!r.ok) throw new Error("Network response was not ok");
      return r.json();
    })
    .then(it => {
      // Clear old bus markers
      Object.values(busMarkers).forEach(m => m.remove());
      busMarkers = {};

      const b = busNoEl.value?.trim();
      if (!b) {
        statusEl.textContent = "Enter bus number";
        return;
      }

      const bus = it.find(x => String(x.route) === b);
      if (!bus) {
        statusEl.textContent = `Bus ${b} inactive`;
        return;
      }

      statusEl.textContent = `Bus ${bus.route} active`;
      busMarkers[b] = busMarker(bus.route, bus.lat, bus.lng);

      // Onboard logic
      if (sl && sg && !isOnboard && dist(sl, sg, bus.lat, bus.lng) <= 5) {
        fetch("/onboard", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rollNo: rollNoEl.value, busRoute: b, onboard: true })
        });
        studentMarker?.remove();
        studentMarker = null;
        isOnboard = true;
        statusEl.textContent = `Onboard Bus ${b}`;
      }
    })
    .catch(err => {
      console.error("Error fetching bus locations:", err);
      statusEl.textContent = "Failed to fetch bus locations.";
    });
}

showBtn.onclick = () => {
  if (!rollNoEl.value || !busNoEl.value) return alert("Enter details");
  mapEl.style.display = "block";
  if (!map) initMap();
  map.resize();
  if (pollInterval) clearInterval(pollInterval);
  isOnboard = false;

  studentWatchId = navigator.geolocation.watchPosition(p => {
    const { latitude, longitude } = p.coords;
    map.setCenter([longitude, latitude]);
    placeStudentMarker(latitude, longitude);
    fetchBus(latitude, longitude);
  }, err => {
    alert("Unable to get your location: " + err.message);
    fetchBus();
  }, { enableHighAccuracy: true, maximumAge: 1000 });

  pollInterval = setInterval(() => {
    if (studentMarker) {
      const c = studentMarker.getLngLat();
      fetchBus(c.lat, c.lng);
    } else {
      fetchBus();
    }
  }, 1000);
};