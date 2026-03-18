let map;
let busMarkers = {};
let studentMarker = null;
let destinationMarker = null;
let isOnboard = false;
let lastBusPosition = {};

let destination = null;

let reminders = {
km2:false,
km1:false,
m500:false
};

let tripMode = "morning";

const rollNoEl = document.getElementById("rollNo");
const busNoEl = document.getElementById("busNo");
const showBtn = document.getElementById("showMap");
const mapEl = document.getElementById("map");
const statusEl = document.getElementById("statusMsg");

const selectDestBtn = document.getElementById("selectDestination");

const destinationBlock = document.getElementById("destinationBlock");
const reminderBox = document.getElementById("reminderBox");

const rem2km = document.getElementById("rem2km");
const rem1km = document.getElementById("rem1km");
const rem500m = document.getElementById("rem500m");

document.querySelectorAll("input[name='tripMode']").forEach(el=>{
el.addEventListener("change",()=>{
tripMode = el.value;

if(tripMode==="evening"){
destinationBlock.style.display="block";
}else{
destinationBlock.style.display="none";
}
});
});

selectDestBtn?.addEventListener("click",()=>{
alert("Click on map to select destination");
});

rem2km?.addEventListener("change",()=>reminders.km2=rem2km.checked);
rem1km?.addEventListener("change",()=>reminders.km1=rem1km.checked);
rem500m?.addEventListener("change",()=>reminders.m500=rem500m.checked);

function setStatus(text){
statusEl.textContent=text;
}

function updateBusStatus(bus,busNo){

if(!bus.active||bus.lastSeen>60){
setStatus(`🔴 Bus ${busNo} is not active`);
}

else if(bus.lastSeen>20){
setStatus(`🟡 Bus ${busNo} updating…`);
}

else{
setStatus(`🟢 Bus ${busNo} (${bus.busType}) active`);
}

}

function initMap(center){

mapboxgl.accessToken="pk.eyJ1IjoiY29kZXMtMTE3IiwiYSI6ImNtbXRsNnAwazFza2UycXNkeTBsdHZqd2YifQ.BM0XjTixeeXYFc3S-Jrm5A";

map=new mapboxgl.Map({
container:"map",
style:"mapbox://styles/mapbox/streets-v11",
center:center||[80.2707,13.0827],
zoom:15
});

map.on("click",(e)=>{

if(tripMode!=="evening") return;

const{lng,lat}=e.lngLat;

destination={lat,lng};

if(destinationMarker) destinationMarker.remove();

destinationMarker=new mapboxgl.Marker({color:"green"})
.setLngLat([lng,lat])
.addTo(map);

reminderBox.style.display="block";

});

}

function placeStudentMarker(lat,lng){

if(isOnboard) return;

if(!studentMarker){

studentMarker=new mapboxgl.Marker({color:"blue"})
.setLngLat([lng,lat])
.addTo(map);

}else{

studentMarker.setLngLat([lng,lat]);
}

}

function getDistance(lat1,lon1,lat2,lon2){

const R=6371000;
const toRad=deg=>deg*Math.PI/180;

const dLat=toRad(lat2-lat1);
const dLon=toRad(lon2-lon1);

const a=
Math.sin(dLat/2)**2+
Math.cos(toRad(lat1))*
Math.cos(toRad(lat2))*
Math.sin(dLon/2)**2;

const c=2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));

return R*c;
}

function createBusMarker(route,lat,lng){

const el=document.createElement("div");

el.innerHTML=`
<i class="fa-solid fa-bus" style="color:red;font-size:20px;"></i>
<div style="font-size:12px;text-align:center;">${route}</div>
`;

return new mapboxgl.Marker(el)
.setLngLat([lng,lat])
.addTo(map);

}

function animateMarker(route,newLat,newLng){

const marker=busMarkers[route];
if(!marker) return;

const start=lastBusPosition[route]||marker.getLngLat();

const startLng=start.lng;
const startLat=start.lat;

const duration=1000;
const startTime=performance.now();

function animate(time){

const progress=Math.min((time-startTime)/duration,1);

const lng=startLng+(newLng-startLng)*progress;
const lat=startLat+(newLat-startLat)*progress;

marker.setLngLat([lng,lat]);

if(progress<1) requestAnimationFrame(animate);

}

requestAnimationFrame(animate);

lastBusPosition[route]={lat:newLat,lng:newLng};

}

function checkDestinationReminder(bus){

if(!destination) return;

const dist=getDistance(bus.lat,bus.lng,destination.lat,destination.lng);

if(reminders.km2 && dist<=2000){
alert("Bus is 2 km from your stop");
reminders.km2=false;
}

if(reminders.km1 && dist<=1000){
alert("Bus is 1 km from your stop");
reminders.km1=false;
}

if(reminders.m500 && dist<=500){
alert("Bus is 500 m from your stop");
reminders.m500=false;
}

}

function loadBusLocations(studentLat,studentLng){

const busNo=busNoEl.value.trim();

fetch("/get_locations")
.then(r=>r.json())
.then(data=>{

data.forEach(bus=>{

if(String(bus.route)!==String(busNo)) return;

if(!bus.active){

if(busMarkers[bus.route]){
busMarkers[bus.route].remove();
delete busMarkers[bus.route];
}

if(!isOnboard){
setStatus(`🔴 Bus ${busNo} is not active`);
}

return;

}

if(!busMarkers[bus.route]){

busMarkers[bus.route]=createBusMarker(bus.route,bus.lat,bus.lng);

}else{

animateMarker(bus.route,bus.lat,bus.lng);

}

if(!isOnboard){
updateBusStatus(bus,busNo);
}

if(tripMode==="evening"){
checkDestinationReminder(bus);
}

if(
typeof studentLat==="number" &&
typeof studentLng==="number" &&
!isOnboard &&
tripMode==="morning"
){

const dist=getDistance(studentLat,studentLng,bus.lat,bus.lng);

if(dist<=20){

fetch("/onboard",{
method:"POST",
headers:{"Content-Type":"application/json"},
body:JSON.stringify({
rollNo:rollNoEl.value,
busRoute:busNo,
onboard:true
})
});

if(studentMarker){
studentMarker.remove();
studentMarker=null;
}

isOnboard=true;

setStatus(`🟢 ONBOARD Bus ${busNo} (${bus.busType})`);
}

}

});

})
.catch(()=>setStatus("⚠️ Failed to fetch bus locations"));

}

showBtn.addEventListener("click",()=>{

const roll=rollNoEl.value.trim();
const bus=busNoEl.value.trim();

if(!roll||!bus){
alert("Enter Roll No & Bus No.");
return;
}

mapEl.style.display="block";

if(!map) initMap([80.2707,13.0827]);

map.resize();

isOnboard=false;

navigator.geolocation.watchPosition(

pos=>{

const{latitude,longitude}=pos.coords;

map.setCenter([longitude,latitude]);

placeStudentMarker(latitude,longitude);

loadBusLocations(latitude,longitude);

},

err=>alert("Location error: "+err.message),

{
enableHighAccuracy:true,
maximumAge:1000
}

);

setInterval(()=>{

if(studentMarker){

const pos=studentMarker.getLngLat();

loadBusLocations(pos.lat,pos.lng);

}

},5000);

});
