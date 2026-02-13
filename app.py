from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key")

# ✅ Render HTTPS session fix
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"

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
# PAGES
# -----------------

@app.route("/")
def home():
    return render_template("home.html")

# 🔐 LOGIN PAGE
@app.route("/driver_login", methods=["GET", "POST"])
def driver_login():

    if request.method == "POST":
        phone = request.form.get("phone")
        otp = request.form.get("otp")

        if not phone or not otp:
            return render_template("login.html", error="Enter phone and OTP")

        driver = Driver.query.filter_by(phone=phone, otp=otp).first()

        if driver:
            driver.logged_in = True
            db.session.commit()
            session["driver_phone"] = phone
            return redirect("/driver")
        else:
            return render_template("login.html", error="Invalid OTP")

    return render_template("login.html")

# DRIVER DASHBOARD
@app.route("/driver")
def driver():
    if "driver_phone" not in session:
        return redirect("/driver_login")
    return render_template("driver.html")

@app.route("/student")
def student():
    return render_template("student.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# -----------------
# MOCK OTP (1234)
# -----------------

@app.route("/send_otp", methods=["POST"])
def send_otp():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone")

    if not phone:
        return jsonify({"error": "Phone required"}), 400

    otp = "1234"  # demo OTP

    driver = Driver.query.filter_by(phone=phone).first()

    if not driver:
        driver = Driver(phone=phone, otp=otp, logged_in=False)
        db.session.add(driver)
    else:
        driver.otp = otp

    db.session.commit()
    return jsonify({"msg": f"OTP sent ({otp} for demo)"})

# -----------------------------
# LOCATION UPDATE
# -----------------------------

@app.route("/location", methods=["POST"])
def location():
    data = request.json

    try:
        if all(k in data for k in ("route", "busType", "lat", "lng")):

            bus = BusLocation.query.filter_by(route=data["route"]).first()

            # ❌ Ignore late GPS after end trip
            if bus and not bus.active:
                return jsonify({"status": "trip ended, ignore location"})

            if bus:
                bus.lat = data["lat"]
                bus.lng = data["lng"]
                bus.bus_type = data["busType"]
                bus.timestamp = datetime.utcnow()
                bus.active = True
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

    except Exception as e:
        print("LOCATION ERROR:", e)
        return jsonify({"error": "location failed"}), 500

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

    # 🔴 Clear onboard students for this bus
        Onboard.query.filter_by(bus_route=route).update({
            "onboard": False,
            "timestamp": datetime.utcnow()
    })

    db.session.commit()

    return jsonify({"status": "trip ended"})

# -----------------------------
# HEARTBEAT + STATUS LOGIC
# -----------------------------

@app.route("/get_locations")
def get_locations():

    try:
        buses = BusLocation.query.all()
        result = []

        for bus in buses:
            if bus.timestamp:
                diff = datetime.utcnow() - bus.timestamp
                last_seen = int(diff.total_seconds())

                if last_seen > 60:
                    if bus.active:
                        bus.active = False
                        # 🔴 Auto clear onboard on timeout
                        Onboard.query.filter_by(bus_route=bus.route).update({
                        "onboard": False,
                        "timestamp": datetime.utcnow()
                    })
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

    except Exception as e:
        print("GET LOCATION ERROR:", e)
        return jsonify([])

# -----------------------------
# ONBOARD
# -----------------------------

@app.route("/onboard", methods=["POST"])
def onboard():
    data = request.json

    try:
        roll_no = data["rollNo"]
        bus_route = data["busRoute"]
        onboard_flag = data["onboard"]

        bus = BusLocation.query.filter_by(route=bus_route).first()

        if not bus or not bus.active:
            onboard_flag = False

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

        return jsonify({"status": "ok", "onboard": onboard_flag})

    except Exception as e:
        print("ONBOARD ERROR:", e)
        return jsonify({"error": "onboard failed"}), 500

# -----------------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
