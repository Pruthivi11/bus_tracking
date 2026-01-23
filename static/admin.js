console.log("Admin dashboard loaded");

function loadBusData() {
  fetch("/get_locations")
    .then(r => r.json())
    .then(items => {
      const div = document.getElementById("busData");
      div.innerHTML = "";
      if (items.length === 0) {
        div.innerHTML = "<p>No active buses.</p>";
        return;
      }
      items.forEach(bus => {
        const p = document.createElement("p");
        p.textContent = `Route: ${bus.route}, Type: ${bus.busType}, Lat: ${bus.lat}, Lng: ${bus.lng}`;
        div.appendChild(p);
      });
    })
    .catch(() => {
      alert("Failed to load bus data.");
    });
}