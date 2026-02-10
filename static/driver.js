let watchId;

function startTrip() {
  const routeEl = document.getElementById('route');
  const busTypeEl = document.getElementById('busType');
  if (!routeEl.value || !busTypeEl.value) {
    alert("Enter route & bus type");
    return;
  }

  navigator.geolocation.getCurrentPosition(pos => {
    watchId = navigator.geolocation.watchPosition(p => {
      const data = {
        route: routeEl.value,
        busType: busTypeEl.value,
        lat: p.coords.latitude,
        lng: p.coords.longitude,
        time: Date.now()
      };

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
  const routeEl = document.getElementById('route');
  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }
  // Mark bus inactive in backend
  fetch("/end_trip", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ route: routeEl.value })
  }).then(() => {
    alert("Trip Ended. Location sharing stopped.");
  });
}

function logout() {
  const routeEl = document.getElementById('route');
  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }
  // Also mark bus inactive when logging out
  fetch("/end_trip", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ route: routeEl.value })
  }).finally(() => {
    window.location.href = "/logout";
  });
}