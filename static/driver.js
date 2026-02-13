let watchId = null;
let tripActive = false;

const tripStatus = document.getElementById("tripStatus");
const startBtn = document.getElementById("startBtn");
const endBtn = document.getElementById("endBtn");
const card = document.querySelector(".card");

function setStatus(text, color) {
  tripStatus.textContent = text;
  tripStatus.style.background = color;
  card.style.borderTopColor = color;
}

function startTrip() {

  const route = document.getElementById("route").value.trim();
  const busType = document.getElementById("busType").value;

  if (!route || !busType) {
    alert("Enter route and select bus type");
    return;
  }

  tripActive = true;

  setStatus("TRIP STARTED", "#09f443");

  startBtn.disabled = true;
  endBtn.disabled = false;

  watchId = navigator.geolocation.watchPosition(pos => {

    if (!tripActive) return;

    fetch("/location", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        route: route,
        busType: busType,
        lat: pos.coords.latitude,
        lng: pos.coords.longitude
      })
    }).catch(err => console.log("Location send error:", err));

  }, err => {
    console.log("GPS error:", err);
    alert("Enable GPS for tracking");
  }, {
    enableHighAccuracy: true,
    maximumAge: 0,
    timeout: 5000
  });
}

function endTrip() {

  const route = document.getElementById("route").value.trim();

  tripActive = false;

  setStatus("TRIP ENDED", "#dc3545");

  startBtn.disabled = false;
  endBtn.disabled = true;

  if (watchId !== null) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }

  fetch("/end_trip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ route: route })
  }).catch(err => console.log("End trip error:", err));
}

function logout() {
  endTrip();
  window.location.href = "/logout";
}