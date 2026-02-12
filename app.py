from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key")
CORS(app)

# -----------------------------
# DATABASE CONFIG (Render Safe)
# -----------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(BASE_DIR, "bus_tracker.db")

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{db_path}"
)
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
# INIT DB (IMPORTANT FOR RENDER)
# -----------------
with app.app_context():
    db.create_all()

# -----------------
# ROUTES
# -----------------

@app.route("/")
def home():
    return render_template("home.html")

# -----------------
# DRIVER LOGIN
# -----------------

@app.route("/driver_login", methods=["GET", "POST"])
def driver_login():
    if "driver_phone" in session:
        return redirect("/driver")

    if request.method == "POST":
        phone = request.form.get("phone")
        otp = request.form.get("otp")

        driver = Driver.query.filter_by(phone=phone, otp=otp).first()

        if driver:
            driver.logged_in = True
            db.session.commit()
            session["driver_phone"] = phone
            return redirect("/driver")

    return render_template("login.html")

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

@app.route("/driver")
def driver():
    if "driver_phone" not in session:
        return redirect("/driver_login")
    return render_template("driver.html")

# -----------------------------
# LOCATION UPDATE (SET ACTIVE)
# -----------------------------

@app.route("/location", methods=["POST"])
def location():
    data = request.get_json(silent=True) or {}

    route = data.get("route")
    bus_type = data.get("busType")
    lat = data.get("lat")
    lng = data.get("lng")

    if not all([route, bus_type, lat, lng]):
        return jsonify({"error": "Missing fields"}), 400

    bus = BusLocation.query.filter_by(route=route).first()

    if bus:
        bus.lat = lat
        bus.lng = lng
        bus.bus_type = bus_type
        bus.timestamp = datetime.utcnow()
        bus.active = True
    else:
        bus = BusLocation(
            route=route,
            bus_type=bus_type,
            lat=lat,
            lng=lng,
            timestamp=datetime.utcnow(),
            active=True
        )
        db.session.add(bus)

    db.session.commit()
    return jsonify({"status": "ok"})

# -----------------------------
# END TRIP (SET ACTIVE FALSE)
# -----------------------------

@app.route("/end_trip", methods=["POST"])
def end_trip():
    data = request.get_json(silent=True) or {}
    route = data.get("route")

    if not route:
        return jsonify({"error": "Route required"}), 400

    bus = BusLocation.query.filter_by(route=route).first()

    if bus:
        bus.active = False
        db.session.commit()

    return jsonify({"status": "trip ended"})

# -----------------------------
# LOGOUT
# -----------------------------

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# -----------------------------
# STUDENT PAGE
# -----------------------------

@app.route("/student")
def student():
    return render_template("student.html")

# -----------------------------
# GET ONLY ACTIVE BUSES
# -----------------------------

@app.route("/get_locations")
def get_locations():
    locations = BusLocation.query.filter_by(active=True).all()

    return jsonify([
        {
            "route": l.route,
            "busType": l.bus_type,
            "lat": l.lat,
            "lng": l.lng,
            "time": l.timestamp.isoformat()
        }
        for l in locations
    ])

# -----------------------------
# ONBOARD SYSTEM
# -----------------------------

@app.route("/onboard", methods=["POST"])
def onboard():
    data = request.get_json(silent=True) or {}

    roll_no = data.get("rollNo")
    bus_route = data.get("busRoute")
    onboard_flag = data.get("onboard")

    if not all([roll_no, bus_route]) or onboard_flag is None:
        return jsonify({"error": "Missing fields"}), 400

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
# ADMIN
# -----------------------------

@app.route("/admin")
def admin():
    onboard_records = Onboard.query.all()
    return render_template("admin.html", onboard=onboard_records)
