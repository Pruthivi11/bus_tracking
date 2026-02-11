# app.py
from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.secret_key = "driver_secret"
CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///bus_tracker.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Models ---
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
    active = db.Column(db.Boolean, default=True)

class Onboard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_no = db.Column(db.String(20), nullable=False)
    bus_route = db.Column(db.String(20), nullable=False)
    onboard = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# --- Routes ---
@app.route("/")
def home():
    return render_template("home.html")

@app.route("/driver_login", methods=["GET", "POST"])
def driver_login():
    if "driver_phone" in session:
        return redirect("/driver")
    if request.method == "POST":
        phone = request.form["phone"]
        otp = request.form["otp"]
        driver = Driver.query.filter_by(phone=phone, otp=otp).first()
        if driver:
            driver.logged_in = True
            db.session.commit()
            session["driver_phone"] = phone
            return redirect("/driver")
    return render_template("login.html")

@app.route("/send_otp", methods=["POST"])
def send_otp():
    phone = request.json.get("phone")
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

@app.route("/location", methods=["POST"])
def location():
    data = request.json
    if not data:
        return jsonify({"status": "error", "msg": "No data received"}), 400

    required = ("route", "busType", "lat", "lng", "time")
    if all(k in data for k in required):
        bus = BusLocation.query.filter_by(route=data["route"]).first()
        if bus:
            bus.lat = data["lat"]
            bus.lng = data["lng"]
            bus.bus_type = data["busType"]
            bus.timestamp = datetime.utcnow()
            bus.active = True   # always mark active when updating
        else:
            bus = BusLocation(
                route=data["route"],
                bus_type=data["busType"],
                lat=data["lat"],
                lng=data["lng"],
                active=True
            )
            db.session.add(bus)
        db.session.commit()
        return jsonify({"status": "ok"})
    else:
        return jsonify({"status": "error", "msg": "Missing fields"}), 400

@app.route("/end_trip", methods=["POST"])
def end_trip():
    data = request.json
    if not data or "route" not in data:
        return jsonify({"status": "error", "msg": "No route provided"}), 400

    route = data["route"]
    bus = BusLocation.query.filter_by(route=route).first()
    if bus:
        bus.active = False
        # Optionally clear lat/lng so students don’t see stale location
        bus.lat = None
        bus.lng = None
        db.session.commit()
        return jsonify({"status": "ended", "msg": f"Bus {route} marked inactive"})
    else:
        return jsonify({"status": "error", "msg": f"No bus found for route {route}"}), 404

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/student")
def student():
    return render_template("student.html")

@app.route("/get_locations")
def get_locations():
    # Only return buses that are active
    locations = BusLocation.query.filter_by(active=True).all()
    return jsonify([
        {
            "route": l.route,
            "busType": l.bus_type,
            "lat": l.lat,
            "lng": l.lng,
            "time": l.timestamp.isoformat()
        } for l in locations if l.active
    ])

@app.route("/onboard", methods=["POST"])
def onboard():
    data = request.json
    if not data:
        return jsonify({"status": "error", "msg": "No data received"}), 400

    roll_no = data.get("rollNo")
    bus_route = data.get("busRoute")
    onboard_flag = data.get("onboard")

    if not roll_no or not bus_route:
        return jsonify({"status": "error", "msg": "Missing rollNo or busRoute"}), 400

    record = Onboard.query.filter_by(roll_no=roll_no, bus_route=bus_route).first()
    if record:
        record.onboard = onboard_flag
        record.timestamp = datetime.utcnow()
    else:
        record = Onboard(roll_no=roll_no, bus_route=bus_route, onboard=onboard_flag)
        db.session.add(record)
    db.session.commit()

    return jsonify({"status": "ok"})

@app.route("/admin")
def admin():
    onboard_records = Onboard.query.all()
    return render_template("admin.html", onboard=onboard_records)

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)