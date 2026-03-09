from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os
import pandas as pd
import random
import requests
from dotenv import load_dotenv
load_dotenv()

# -----------------
# KEYS
# -----------------

MAPBOX_KEY = os.environ.get("MAPBOX_KEY")
FAST2SMS_API_KEY = os.environ.get("FAST2SMS_API_KEY")


# -----------------
# APP SETUP
# -----------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key")

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"

CORS(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///bus_tracker.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# -----------------
# LOAD DRIVER WHITELIST
# -----------------

drivers_df = pd.read_excel("drivers.xlsx")
AUTHORIZED_DRIVERS = set(drivers_df["phone"].astype(str))

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
    route = db.Column(db.String(20), unique=True)
    bus_type = db.Column(db.String(20))
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    active = db.Column(db.Boolean, default=False)

class Onboard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_no = db.Column(db.String(20))
    bus_route = db.Column(db.String(20))
    onboard = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# -----------------
# PAGES
# -----------------

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/student")
def student():
    return render_template("student.html", mapbox_key=MAPBOX_KEY)

@app.route("/admin")
def admin():
    return render_template("admin.html")

# -----------------
# DRIVER LOGIN
# -----------------

@app.route("/driver_login", methods=["GET","POST"])
def driver_login():

    if request.method == "GET":
        session.clear()
        return render_template("login.html")

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

    return render_template("login.html", error="Invalid OTP")

# -----------------
# PROTECTED DRIVER PAGE
# -----------------

@app.route("/driver")
def driver():

    phone = session.get("driver_phone")

    if not phone:
        return redirect("/driver_login")

    driver = Driver.query.filter_by(phone=phone).first()

    if not driver or not driver.logged_in:
        session.clear()
        return redirect("/driver_login")

    return render_template("driver.html")

# -----------------
# LOGOUT
# -----------------

@app.route("/logout")
def logout():

    phone = session.get("driver_phone")

    if phone:
        driver = Driver.query.filter_by(phone=phone).first()
        if driver:
            driver.logged_in = False
            db.session.commit()

    session.clear()
    return redirect("/")

# -----------------
# SEND OTP
# -----------------

@app.route("/send_otp", methods=["POST"])
def send_otp():

    try:

        data = request.get_json()
        phone = str(data.get("phone"))

        if not phone:
            return jsonify({"error":"Phone required"}),400

        # check whitelist
        if phone not in AUTHORIZED_DRIVERS:
            return jsonify({"error":"Driver not authorized"}),403

        # generate otp
        otp = str(random.randint(1000,9999))

        driver = Driver.query.filter_by(phone=phone).first()

        if not driver:
            driver = Driver(phone=phone, otp=otp, logged_in=False)
            db.session.add(driver)
        else:
            driver.otp = otp

        db.session.commit()

        # -----------------
        # FAST2SMS REQUEST
        # -----------------

        url = "https://www.fast2sms.com/dev/bulkV2"

        payload = {
            "route":"otp",
            "variables_values":otp,
            "numbers":phone
        }

        headers = {
            "authorization":FAST2SMS_API_KEY
        }

        response = requests.get(url,headers=headers,params=payload)

        print("FAST2SMS RESPONSE:",response.text)

        return jsonify({"msg":"OTP sent"})

    except Exception as e:
        print("OTP ERROR:",e)
        return jsonify({"error":"OTP failed"}),500

# -----------------
# LOCATION UPDATE
# -----------------

@app.route("/location", methods=["POST"])
def location():

    try:

        data = request.get_json()

        route = data["route"]
        busType = data["busType"]
        lat = data["lat"]
        lng = data["lng"]

        bus = BusLocation.query.filter_by(route=route).first()

        if bus:

            bus.lat = lat
            bus.lng = lng
            bus.bus_type = busType
            bus.timestamp = datetime.utcnow()
            bus.active = True

        else:

            bus = BusLocation(
                route=route,
                bus_type=busType,
                lat=lat,
                lng=lng,
                active=True
            )

            db.session.add(bus)

        db.session.commit()

        return jsonify({"status":"ok"})

    except Exception as e:

        print("LOCATION ERROR:",e)
        return jsonify({"error":"location failed"}),500

# -----------------
# END TRIP
# -----------------

@app.route("/end_trip", methods=["POST"])
def end_trip():

    data = request.get_json()
    route = data.get("route")

    bus = BusLocation.query.filter_by(route=route).first()

    if bus:

        bus.active = False

        Onboard.query.filter_by(bus_route=route).update({
            "onboard":False,
            "timestamp":datetime.utcnow()
        })

    db.session.commit()

    return jsonify({"status":"trip ended"})

# -----------------
# GET LOCATIONS
# -----------------

@app.route("/get_locations")
def get_locations():

    buses = BusLocation.query.all()
    result = []

    for bus in buses:

        diff = datetime.utcnow() - bus.timestamp
        last_seen = int(diff.total_seconds())

        if last_seen > 60:
            bus.active = False
        else:
            bus.active = True

        result.append({
            "route":bus.route,
            "busType":bus.bus_type,
            "lat":bus.lat,
            "lng":bus.lng,
            "lastSeen":last_seen,
            "active":bus.active
        })

    db.session.commit()

    return jsonify(result)

# -----------------
# RUN SERVER
# -----------------

if __name__ == "__main__":

    with app.app_context():
        db.create_all()

    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port)