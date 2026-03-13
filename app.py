from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import os
import pandas as pd
import random

# -----------------
# APP SETUP
# -----------------

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "dev_key")

# -----------------
# DATABASE CONFIG
# -----------------

DATABASE_URL = os.environ.get("DATABASE_URL")

# Render sometimes gives postgres:// instead of postgresql://
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

CORS(app)

MAPBOX_KEY = os.environ.get("MAPBOX_KEY")

# -----------------
# AUTHORIZED DRIVERS
# -----------------

AUTHORIZED_DRIVERS = set()

def load_drivers():
    global AUTHORIZED_DRIVERS

    try:
        drivers_df = pd.read_excel("drivers.xlsx")

        AUTHORIZED_DRIVERS = set(
            drivers_df["phone"]
            .astype(str)
            .str.replace(".0","", regex=False)
            .str.strip()
        )

        print("Authorized drivers loaded:", AUTHORIZED_DRIVERS)

    except Exception as e:
        print("Driver Excel load error:", e)

# Load drivers when app starts
load_drivers()


# -----------------
# DATABASE MODELS
# -----------------

class Driver(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False, index=True)
    otp = db.Column(db.String(10))
    otp_created = db.Column(db.DateTime)
    logged_in = db.Column(db.Boolean, default=False)


class BusLocation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    route = db.Column(db.String(20), unique=True, nullable=False, index=True)
    bus_type = db.Column(db.String(20))
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    active = db.Column(db.Boolean, default=False)


class Onboard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    roll_no = db.Column(db.String(20), nullable=False, index=True)
    bus_route = db.Column(db.String(20), nullable=False, index=True)
    onboard = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


# -----------------
# CREATE DATABASE TABLES
# -----------------

with app.app_context():
    db.create_all()


# -----------------
# PAGES
# -----------------

@app.route("/")
def home():
    return render_template("home.html")


@app.route("/student")
def student():
    return render_template("student.html")


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

    driver = Driver.query.filter_by(phone=phone).first()

    if not driver or driver.otp != otp:
        return render_template("login.html", error="Invalid OTP")

    if driver.otp_created and datetime.utcnow() - driver.otp_created > timedelta(minutes=5):
        return render_template("login.html", error="OTP expired")

    driver.logged_in = True
    db.session.commit()

    session["driver_phone"] = phone
    return redirect("/driver")


# -----------------
# DRIVER PAGE
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

        data = request.get_json(silent=True) or {}
        phone = str(data.get("phone","")).strip()

        phone = phone.replace("+91","")
        phone = phone.replace(" ","")
        phone = phone.replace("-","")

        if not phone:
            return jsonify({"error":"Phone required"}),400

        if phone not in AUTHORIZED_DRIVERS:
            return jsonify({"error":"Driver not authorized"}),403

        otp = str(random.randint(1000,9999))

        driver = Driver.query.filter_by(phone=phone).first()

        if not driver:

            driver = Driver(
                phone=phone,
                otp=otp,
                otp_created=datetime.utcnow(),
                logged_in=False
            )

            db.session.add(driver)

        else:
            driver.otp = otp
            driver.otp_created = datetime.utcnow()

        db.session.commit()

        return jsonify({
            "msg":"OTP generated",
            "otp":otp
        })

    except Exception as e:
        print("OTP ERROR:",e)
        return jsonify({"error":"OTP failed"}),500


# -----------------
# BUS LOCATION UPDATE
# -----------------

@app.route("/location", methods=["POST"])
def location():

    try:

        data = request.get_json(silent=True) or {}

        required = ("route","busType","lat","lng")

        if not all(k in data for k in required):
            return jsonify({"error":"Invalid data"}),400

        route = data["route"]

        bus = BusLocation.query.filter_by(route=route).first()

        if bus:

            bus.lat = data["lat"]
            bus.lng = data["lng"]
            bus.bus_type = data["busType"]
            bus.timestamp = datetime.utcnow()
            bus.active = True

        else:

            bus = BusLocation(
                route=route,
                bus_type=data["busType"],
                lat=data["lat"],
                lng=data["lng"],
                timestamp=datetime.utcnow(),
                active=True
            )

            db.session.add(bus)

        db.session.commit()

        return jsonify({"status":"ok"})

    except Exception as e:

        print("LOCATION ERROR:",e)
        return jsonify({"error":"location failed"}),500


# -----------------
# GET BUS LOCATIONS
# -----------------

@app.route("/get_locations")
def get_locations():

    buses = BusLocation.query.all()
    result = []

    for bus in buses:

        diff = datetime.utcnow() - bus.timestamp
        last_seen = int(diff.total_seconds())

        if last_seen > 60:

            if bus.active:
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
# END TRIP
# -----------------

@app.route("/end_trip", methods=["POST"])
def end_trip():

    data = request.get_json(silent=True) or {}
    route = data.get("route")

    bus = BusLocation.query.filter_by(route=route).first()

    if bus:
        bus.active = False

    db.session.commit()

    return jsonify({"status":"trip ended"})
