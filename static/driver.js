let map, marker, watchId;

function initMap(lat, lng) {
  mapboxgl.accessToken = 'pk.eyJ1IjoiY29kZXMtMTE3IiwiYSI6ImNta2Y2dzhwdjBnNjAzaHF6Y2tydXY2aXgifQ.Ss1FmjnHljaQc7BgTDvZSQ';
  map = new mapboxgl.Map({
    container: 'map',
    style: 'mapbox://styles/mapbox/streets-v11',
    center: [lng, lat],
    zoom: 16
  });

  marker = new mapboxgl.Marker().setLngLat([lng, lat]).addTo(map);
}

function startTrip() {
  const routeEl = document.getElementById('route');
  const busTypeEl = document.getElementById('busType');
  if (!routeEl.value || !busTypeEl.value) {
    alert("Enter route & bus type");
    return;
  }

  navigator.geolocation.getCurrentPosition(pos => {
    initMap(pos.coords.latitude, pos.coords.longitude);

    watchId = navigator.geolocation.watchPosition(p => {
      const data = {
        route: routeEl.value,
        busType: busTypeEl.value,
        lat: p.coords.latitude,
        lng: p.coords.longitude,
        time: Date.now()
      };

      marker.setLngLat([data.lng, data.lat]);
      map.setCenter([data.lng, data.lat]);

      fetch("/location", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(data)
      }).catch(() => {});
    }, err => {
      console.warn("watchPosition error:", err);
    }, { enableHighAccuracy: true, maximumAge: 1000 });
  }, err => {
    alert("Unable to get location: " + err.message);
  }, { enableHighAccuracy: true, timeout: 10000 });
}

function endTrip() {
  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    alert("Trip Ended");
  }
}

function logout() {
  window.location.href = "/logout";
}