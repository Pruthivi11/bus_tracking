from flask import Flask, render_template, request, redirect, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import os
import random

app = Flask(__name__)
app.secret_key = "driver_secret"

# ================= CONFIG =================
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///bus_tracker.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ================ MODELS ==================
class Driver(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(15), unique=True)
    otp = db.Column(db.String(6))
    otp_expiry = db.Column(db.DateTime)
    bus_number = db.Column(db.String(10))
    trip_active = db.Column(db.Boolean, default=False)
    last_seen = db.Column(db.DateTime)

class BusLocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bus_number = db.Column(db.String(10))
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# ============ CREATE DB (Flask 3.x way) ============
with app.app_context():
    db.create_all()

# ================= ROUTES =================

@app.route("/")
def home():
    return redirect("home.html")

# ---------- DRIVER LOGIN PAGE ----------
@app.route("/driver_login", methods=["GET", "POST"])
def driver_login():
    if request.method == "POST":
        phone = request.form["phone"]
        otp = request.form["otp"]

        driver = Driver.query.filter_by(phone=phone).first()

        if driver and driver.otp == otp and driver.otp_expiry > datetime.utcnow():
            session["driver"] = driver.phone
            return redirect("/driver_dashboard")
        else:
            return "Invalid OTP"

    return render_template("driver.html")

# ---------- SEND OTP ----------
@app.route("/send_otp", methods=["POST"])
def send_otp():
    data = request.get_json()
    phone = data.get("phone")

    if not phone:
        return jsonify({"msg": "Phone required"}), 400

    otp = str(random.randint(100000, 999999))

    driver = Driver.query.filter_by(phone=phone).first()
    if not driver:
        driver = Driver(phone=phone, bus_number="Bus X")
        db.session.add(driver)

    driver.otp = otp
    driver.otp_expiry = datetime.utcnow() + timedelta(minutes=5)
    db.session.commit()

    print("OTP for", phone, "is", otp)  # visible in Render logs

    return jsonify({"msg": "OTP sent (check logs)"})

# ---------- DRIVER DASHBOARD ----------
@app.route("/driver_dashboard")
def driver_dashboard():
    if "driver" not in session:
        return redirect("/driver_login")
    return render_template("driver_dashboard.html")

# ---------- START TRIP ----------
@app.route("/start_trip", methods=["POST"])
def start_trip():
    if "driver" not in session:
        return jsonify({"msg": "Unauthorized"}), 401

    driver = Driver.query.filter_by(phone=session["driver"]).first()
    driver.trip_active = True
    driver.last_seen = datetime.utcnow()
    db.session.commit()

    return jsonify({"msg": "TRIP STARTED"})

# ---------- END TRIP ----------
@app.route("/end_trip", methods=["POST"])
def end_trip():
    if "driver" not in session:
        return jsonify({"msg": "Unauthorized"}), 401

    driver = Driver.query.filter_by(phone=session["driver"]).first()
    driver.trip_active = False
    driver.last_seen = None
    db.session.commit()

    session.pop("driver", None)

    return jsonify({"msg": "TRIP ENDED"})

# ---------- UPDATE LOCATION ----------
@app.route("/update_location", methods=["POST"])
def update_location():
    if "driver" not in session:
        return jsonify({"msg": "Unauthorized"}), 401

    data = request.get_json()
    lat = data.get("lat")
    lng = data.get("lng")

    driver = Driver.query.filter_by(phone=session["driver"]).first()

    if not driver.trip_active:
        return jsonify({"msg": "Trip not active"}), 400

    driver.last_seen = datetime.utcnow()

    loc = BusLocation(
        bus_number=driver.bus_number,
        lat=lat,
        lng=lng
    )
    db.session.add(loc)
    db.session.commit()

    return jsonify({"msg": "Location updated"})

# ---------- STUDENT LOGIN ----------
@app.route("/student_login")
def student_login():
    return render_template("student.html")

# ---------- GET BUS STATUS ----------
@app.route("/bus_status")
def bus_status():
    driver = Driver.query.filter_by(bus_number="Bus X").first()

    if not driver or not driver.trip_active:
        return jsonify({"status": "inactive"})

    # Network fluctuation handling (60 sec grace)
    if driver.last_seen and datetime.utcnow() - driver.last_seen > timedelta(seconds=60):
        return jsonify({"status": "delayed"})  # network issue

    return jsonify({"status": "active"})

# ---------- GET BUS LOCATION ----------
@app.route("/bus_location")
def bus_location():
    driver = Driver.query.filter_by(bus_number="Bus X").first()

    if not driver or not driver.trip_active:
        return jsonify({"msg": "Bus inactive"}), 400

    loc = BusLocation.query.filter_by(bus_number="Bus X") \
        .order_by(BusLocation.timestamp.desc()).first()

    if not loc:
        return jsonify({"msg": "No location yet"}), 400

    return jsonify({
        "lat": loc.lat,
        "lng": loc.lng
    })

# ================= RUN (Render ready) =================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)