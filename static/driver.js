let watchId = null;
let isTripActive = false;

function startTrip() {
  const routeEl = document.getElementById('route');
  const busTypeEl = document.getElementById('busType');

  const route = routeEl.value.trim();
  const busType = busTypeEl.value;

  if (!route || !busType) {
    alert("Enter route & bus type");
    return;
  }

  if (isTripActive) {
    alert("Trip already active.");
    return;
  }

  if (!navigator.geolocation) {
    alert("Geolocation not supported.");
    return;
  }

  navigator.geolocation.getCurrentPosition(
    () => {

      // 🔹 Activate trip in backend
      fetch("/start_trip", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ route: route, busType: busType })
      });

      isTripActive = true;

      watchId = navigator.geolocation.watchPosition(
        pos => {
          fetch("/location", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              route: route,
              busType: busType,
              lat: pos.coords.latitude,
              lng: pos.coords.longitude,
              time: Date.now()
            })
          });
        },
        err => console.warn(err),
        { enableHighAccuracy: true, maximumAge: 1000 }
      );

      alert("Trip Started.");
    },
    err => alert("Location error: " + err.message),
    { enableHighAccuracy: true }
  );
}

function endTrip() {
  const route = document.getElementById('route').value.trim();

  if (!route) {
    alert("Enter route number.");
    return;
  }

  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }

  isTripActive = false;

  // 🔹 Mark bus inactive instead of deleting
  fetch("/end_trip", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ route: route })
  }).then(() => {
    alert("Trip Ended. Bus marked inactive.");
  });
}

function logout() {
  const route = document.getElementById('route').value.trim();

  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }

  if (route) {
    fetch("/end_trip", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ route: route })
    }).finally(() => {
      window.location.href = "/logout";
    });
  } else {
    window.location.href = "/logout";
  }
}
