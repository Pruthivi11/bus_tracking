let watchId = null;
let tripActive = false;

const tripCard = document.getElementById("tripStatusCard");
const statusDot = document.getElementById("statusDot");
const startBtn = document.getElementById("startBtn");
const endBtn = document.getElementById("endBtn");

function updateCard(text, color, online = false) {
    tripCard.childNodes[0].nodeValue = text + " ";
    tripCard.style.backgroundColor = color;

    if (online) {
        statusDot.classList.add("online");
    } else {
        statusDot.classList.remove("online");
    }
}

function startTrip() {
    const route = document.getElementById("route").value.trim();
    const busType = document.getElementById("busType").value.trim();

    if (!route || !busType) {
        alert("Enter route and bus type");
        return;
    }

    tripActive = true;

    updateCard("TRIP STARTED", "#28a745", true);

    startBtn.disabled = true;
    endBtn.disabled = false;

    watchId = navigator.geolocation.watchPosition(position => {

        if (!tripActive) return;

        const data = {
            route: route,
            busType: busType,
            lat: position.coords.latitude,
            lng: position.coords.longitude
        };

        fetch("/location", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data)
        }).catch(err => console.log("Location error:", err));

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

    updateCard("TRIP ENDED", "#dc3545", false);

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
