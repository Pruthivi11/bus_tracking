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
    alert("Geolocation not supported by your browser.");
    return;
  }

  navigator.geolocation.getCurrentPosition(
    pos => {
      isTripActive = true;

      // Start continuous tracking
      watchId = navigator.geolocation.watchPosition(
        p => {
          const data = {
            route: route,
            busType: busType,
            lat: p.coords.latitude,
            lng: p.coords.longitude,
            time: Date.now()
          };

          fetch("/location", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data)
          }).catch(err => {
            console.warn("Location send failed:", err);
          });

        },
        err => {
          console.warn("watchPosition error:", err);
        },
        {
          enableHighAccuracy: true,
          maximumAge: 1000
        }
      );

      alert("Trip started. Location sharing active.");
    },
    err => {
      alert("Unable to get location: " + err.message);
    },
    {
      enableHighAccuracy: true,
      timeout: 10000
    }
  );
}

function endTrip() {
  const routeEl = document.getElementById('route');
  const route = routeEl.value.trim();

  if (!route) {
    alert("Enter route number before ending trip.");
    return;
  }

  // Stop GPS tracking
  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }

  isTripActive = false;

  // Inform backend to remove bus from active list
  fetch("/end_trip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ route: route })
  })
  .then(res => res.json())
  .then(() => {
    alert("Trip Ended. Bus is now inactive.");
  })
  .catch(() => {
    alert("Trip ended locally, but server update failed.");
  });
}

function logout() {
  const routeEl = document.getElementById('route');
  const route = routeEl.value.trim();

  // Stop tracking if active
  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }

  isTripActive = false;

  // Also remove bus if trip was active
  if (route) {
    fetch("/end_trip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ route: route })
    }).finally(() => {
      window.location.href = "/logout";
    });
  } else {
    window.location.href = "/logout";
  }
}
