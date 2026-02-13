from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key")
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///bus_tracker.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# -----------------
# MODELS
# -----------------

class Driver(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    otp = db.Column(db.String(10))
    logged_in = db.Column(db.Boolean, default=False)

class BusLocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route = db.Column(db.String(20), unique=True, nullable=False)
    bus_type = db.Column(db.String(20))
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    active = db.Column(db.Boolean, default=False)

class Onboard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_no = db.Column(db.String(20), nullable=False)
    bus_route = db.Column(db.String(20), nullable=False)
    onboard = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# -----------------
# ROUTES
# -----------------

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/driver")
def driver():
    if "driver_phone" not in session:
        return redirect("/driver_login")
    return render_template("driver.html")

@app.route("/student")
def student():
    return render_template("student.html")

# -----------------------------
# LOCATION UPDATE (SET ACTIVE)
# -----------------------------

@app.route("/location", methods=["POST"])
def location():
    data = request.json

    if all(k in data for k in ("route", "busType", "lat", "lng")):

        bus = BusLocation.query.filter_by(route=data["route"]).first()

        if bus:
            bus.lat = data["lat"]
            bus.lng = data["lng"]
            bus.bus_type = data["busType"]
            bus.timestamp = datetime.utcnow()
            bus.active = True  # ✅ auto reactivate
        else:
            bus = BusLocation(
                route=data["route"],
                bus_type=data["busType"],
                lat=data["lat"],
                lng=data["lng"],
                timestamp=datetime.utcnow(),
                active=True
            )
            db.session.add(bus)

        db.session.commit()

    return jsonify({"status": "ok"})

# -----------------------------
# END TRIP
# -----------------------------

@app.route("/end_trip", methods=["POST"])
def end_trip():
    data = request.json
    route = data.get("route")

    bus = BusLocation.query.filter_by(route=route).first()

    if bus:
        bus.active = False
        db.session.commit()

    return jsonify({"status": "trip ended"})

# -----------------------------
# HEARTBEAT + STATUS LOGIC
# -----------------------------

@app.route("/get_locations")
def get_locations():

    buses = BusLocation.query.all()
    result = []

    for bus in buses:
        if bus.timestamp:
            diff = datetime.utcnow() - bus.timestamp
            last_seen = int(diff.total_seconds())

            if last_seen > 60:
                bus.active = False
            else:
                bus.active = True

            result.append({
                "route": bus.route,
                "busType": bus.bus_type,
                "lat": bus.lat,
                "lng": bus.lng,
                "lastSeen": last_seen,
                "active": bus.active
            })

    db.session.commit()
    return jsonify(result)

# -----------------------------
# ONBOARD
# -----------------------------

@app.route("/onboard", methods=["POST"])
def onboard():
    data = request.json
    roll_no = data["rollNo"]
    bus_route = data["busRoute"]
    onboard_flag = data["onboard"]

    bus = BusLocation.query.filter_by(route=bus_route).first()

    if not bus or not bus.active:
        onboard_flag = False  # force off if inactive

    record = Onboard.query.filter_by(
        roll_no=roll_no,
        bus_route=bus_route
    ).first()

    if record:
        record.onboard = onboard_flag
        record.timestamp = datetime.utcnow()
    else:
        record = Onboard(
            roll_no=roll_no,
            bus_route=bus_route,
            onboard=onboard_flag
        )
        db.session.add(record)

    db.session.commit()
    return jsonify({"status": "ok"})

# -----------------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
