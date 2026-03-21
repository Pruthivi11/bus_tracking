from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from functools import wraps
import os, re
import pandas as pd
import random

# ─────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key")

# ─────────────────────────────────────────────
# DATABASE CONFIG
# ─────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db   = SQLAlchemy(app)
CORS(app)

MAPBOX_KEY          = os.environ.get("MAPBOX_KEY")
ADMIN_PASSWORD      = os.environ.get("ADMIN_PASSWORD", "admin@123")
EXCEL_PATH          = "drivers.xlsx"
BUS_DETAILS_PATH    = "bus_details.xlsx"

# ─────────────────────────────────────────────
# BUS ROUTE LOOKUP  (in-memory from bus_details.xlsx)
#
# Loaded once at startup; refreshed by load_bus_details().
# Maps bus_no (str) → bus_route (str), e.g. {"12": "Velachery"}
# Gracefully empty if the file doesn't exist.
# ─────────────────────────────────────────────

BUS_ROUTE_MAP: dict = {}

def load_bus_details():
    """Load bus_no → bus_route mapping from bus_details.xlsx into memory."""
    global BUS_ROUTE_MAP
    try:
        df = pd.read_excel(BUS_DETAILS_PATH)
        df["bus_no"]    = df["bus_no"].astype(str).str.strip()
        df["bus_route"] = df["bus_route"].astype(str).str.strip()
        BUS_ROUTE_MAP   = dict(zip(df["bus_no"], df["bus_route"]))
        print(f"Bus details loaded: {len(BUS_ROUTE_MAP)} routes from {BUS_DETAILS_PATH}")
    except FileNotFoundError:
        print(f"[INFO] {BUS_DETAILS_PATH} not found — route recommendation disabled.")
    except Exception as e:
        print(f"Bus details load error: {e}")


# ─────────────────────────────────────────────
# DATA SOURCE DETECTION
#
#   "db"    →  DATABASE_URL env var is set  →  use PostgreSQL / AuthorizedDriver table
#   "excel" →  no DATABASE_URL             →  use drivers.xlsx
#
# Every driver-management function calls get_driver_source() first so the
# active source is always resolved from one place.
# ─────────────────────────────────────────────

def get_driver_source():
    """Return 'db' if a database URL is configured, else 'excel'."""
    return "db" if DATABASE_URL else "excel"


# ─────────────────────────────────────────────
# DATABASE MODELS
# ─────────────────────────────────────────────

class Driver(db.Model):
    """OTP / session authentication records — one row per registered driver login."""
    __tablename__ = "driver"
    id          = db.Column(db.Integer, primary_key=True)
    phone       = db.Column(db.String(20), unique=True, nullable=False, index=True)
    otp         = db.Column(db.String(10))
    otp_created = db.Column(db.DateTime)
    logged_in   = db.Column(db.Boolean, default=False)


class AuthorizedDriver(db.Model):
    """
    Authorisation whitelist – phone numbers permitted to drive.
    Replaces / mirrors the data previously stored only in drivers.xlsx.
    Seeded automatically from Excel on first run when DATABASE_URL is set.
    """
    __tablename__ = "authorized_driver"
    id         = db.Column(db.Integer, primary_key=True)
    phone      = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name       = db.Column(db.String(100), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class BusLocation(db.Model):
    __tablename__ = "bus_location"
    id        = db.Column(db.Integer, primary_key=True)
    route     = db.Column(db.String(20), unique=True, nullable=False, index=True)
    bus_type  = db.Column(db.String(20))
    bus_route = db.Column(db.String(100), nullable=True)   # route area, e.g. "Velachery"
    lat       = db.Column(db.Float)
    lng       = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    active    = db.Column(db.Boolean, default=False)


class Onboard(db.Model):
    __tablename__ = "onboard"
    id        = db.Column(db.Integer, primary_key=True)
    roll_no   = db.Column(db.String(20), nullable=False, index=True)
    bus_route = db.Column(db.String(20), nullable=False, index=True)
    onboard   = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# PHONE UTILITIES
# ─────────────────────────────────────────────

def _normalize_phone(raw):
    p = str(raw).strip()
    p = p.replace("+91", "").replace(" ", "").replace("-", "").replace(".0", "")
    return p


def _validate_phone(raw):
    """Return (cleaned_phone, error). Valid = 10-digit Indian mobile (starts 6-9)."""
    p = _normalize_phone(raw)
    if not re.fullmatch(r"[6-9]\d{9}", p):
        return None, "Phone must be a valid 10-digit Indian mobile number (starts with 6-9)."
    return p, None


# ─────────────────────────────────────────────
# STARTUP: create tables + load authorised drivers
# ─────────────────────────────────────────────

AUTHORIZED_DRIVERS = set()    # in-memory set used by send_otp for fast lookups


def _seed_db_from_excel():
    """One-time import: copy phones from drivers.xlsx into AuthorizedDriver table."""
    try:
        df = pd.read_excel(EXCEL_PATH)
        for _, row in df.iterrows():
            phone = _normalize_phone(row["phone"])
            name  = str(row.get("name", "")).strip()
            if phone and not AuthorizedDriver.query.filter_by(phone=phone).first():
                db.session.add(AuthorizedDriver(phone=phone, name=name))
        db.session.commit()
        print("Seeded AuthorizedDriver table from Excel.")
    except Exception as e:
        print("Excel seed error:", e)


def load_drivers():
    """Populate AUTHORIZED_DRIVERS from the active data source."""
    global AUTHORIZED_DRIVERS
    source = get_driver_source()

    if source == "db":
        if AuthorizedDriver.query.count() == 0:
            _seed_db_from_excel()
        AUTHORIZED_DRIVERS = {_normalize_phone(r.phone)
                               for r in AuthorizedDriver.query.all()}
    else:
        try:
            df = pd.read_excel(EXCEL_PATH)
            AUTHORIZED_DRIVERS = set(df["phone"].astype(str).apply(_normalize_phone))
        except Exception as e:
            print("Excel load error:", e)

    print(f"[{source.upper()}] Authorized drivers loaded: {AUTHORIZED_DRIVERS}")


with app.app_context():
    db.create_all()
    load_drivers()
    load_bus_details()


# ─────────────────────────────────────────────
# DRIVER CRUD HELPERS  (data-source-aware)
# ─────────────────────────────────────────────

def _drivers_get_all_db(search, sort, page, per_page):
    q = AuthorizedDriver.query
    if search:
        t = f"%{search}%"
        q = q.filter(db.or_(
            AuthorizedDriver.phone.ilike(t),
            AuthorizedDriver.name.ilike(t)
        ))
    q = q.order_by(
        AuthorizedDriver.created_at.asc() if sort == "oldest"
        else AuthorizedDriver.created_at.desc()
    )
    total   = q.count()
    records = q.offset((page - 1) * per_page).limit(per_page).all()
    return [
        {"id": r.id, "phone": r.phone, "name": r.name,
         "created_at": r.created_at.strftime("%d %b %Y") if r.created_at else ""}
        for r in records
    ], total


def _drivers_get_all_excel(search, sort, page, per_page):
    try:
        df = pd.read_excel(EXCEL_PATH)
    except Exception:
        return [], 0

    df["phone"] = df["phone"].astype(str).apply(_normalize_phone)
    if "name" not in df.columns:
        df["name"] = ""
    df["name"] = df["name"].fillna("").astype(str)

    if search:
        mask = (
            df["phone"].str.contains(search, case=False, na=False) |
            df["name"].str.contains(search, case=False, na=False)
        )
        df = df[mask].reset_index(drop=True)

    if sort != "oldest":
        df = df.iloc[::-1].reset_index(drop=True)

    total = len(df)
    start = (page - 1) * per_page
    page_df = df.iloc[start: start + per_page]

    return [
        {"id": start + i + 1, "phone": r["phone"], "name": r["name"], "created_at": ""}
        for i, r in enumerate(page_df.to_dict("records"))
    ], total


def _add_driver_db(phone, name):
    if AuthorizedDriver.query.filter_by(phone=phone).first():
        return False, "Phone number already exists."
    db.session.add(AuthorizedDriver(phone=phone, name=name))
    db.session.commit()
    return True, None


def _add_driver_excel(phone, name):
    try:
        df = pd.read_excel(EXCEL_PATH)
    except Exception:
        df = pd.DataFrame(columns=["phone", "name"])

    df["phone"] = df["phone"].astype(str).apply(_normalize_phone)
    if phone in df["phone"].values:
        return False, "Phone number already exists."

    df = pd.concat([df, pd.DataFrame([{"phone": phone, "name": name}])], ignore_index=True)
    df.to_excel(EXCEL_PATH, index=False)
    return True, None


def _update_driver_db(driver_id, new_phone, new_name):
    rec = AuthorizedDriver.query.get(driver_id)
    if not rec:
        return False, "Driver not found.", None

    old_phone = rec.phone
    if new_phone != old_phone:
        if AuthorizedDriver.query.filter_by(phone=new_phone).first():
            return False, "Phone number already used by another driver.", None

    rec.phone = new_phone
    rec.name  = new_name
    db.session.commit()
    return True, None, old_phone


def _update_driver_excel(driver_id, new_phone, new_name):
    try:
        df = pd.read_excel(EXCEL_PATH)
    except Exception:
        return False, "Could not read Excel file.", None

    df["phone"] = df["phone"].astype(str).apply(_normalize_phone)
    idx = driver_id - 1

    if idx < 0 or idx >= len(df):
        return False, "Driver not found.", None

    old_phone = df.iloc[idx]["phone"]

    others = df.drop(index=idx)
    if new_phone in others["phone"].values:
        return False, "Phone number already used by another driver.", None

    df.at[idx, "phone"] = new_phone
    if "name" in df.columns:
        df.at[idx, "name"] = new_name
    df.to_excel(EXCEL_PATH, index=False)
    return True, None, old_phone


def _delete_driver_db(driver_id):
    rec = AuthorizedDriver.query.get(driver_id)
    if not rec:
        return False, "Driver not found.", None
    old_phone = rec.phone
    db.session.delete(rec)
    db.session.commit()
    return True, None, old_phone


def _delete_driver_excel(driver_id):
    try:
        df = pd.read_excel(EXCEL_PATH)
    except Exception:
        return False, "Could not read Excel file.", None

    df["phone"] = df["phone"].astype(str).apply(_normalize_phone)
    idx = driver_id - 1

    if idx < 0 or idx >= len(df):
        return False, "Driver not found.", None

    old_phone = df.iloc[idx]["phone"]
    df = df.drop(index=idx).reset_index(drop=True)
    df.to_excel(EXCEL_PATH, index=False)
    return True, None, old_phone


# ─────────────────────────────────────────────
# AUTH DECORATOR
# ─────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            if request.is_json or request.method != "GET":
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/admin_login")
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────
# PUBLIC PAGES
# ─────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("home.html")


@app.route("/student")
def student():
    return render_template("student.html")


# ─────────────────────────────────────────────
# ADMIN AUTH
# ─────────────────────────────────────────────

@app.route("/admin_login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect("/admin")

    if request.method == "GET":
        return render_template("admin_login.html")

    password = request.form.get("password", "").strip()
    if not password:
        return render_template("admin_login.html", error="Enter the admin password")
    if password != ADMIN_PASSWORD:
        return render_template("admin_login.html", error="Incorrect password. Please try again.")

    session["admin_logged_in"] = True
    return redirect("/admin")


@app.route("/admin_logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    return redirect("/admin_login")


# ─────────────────────────────────────────────
# ADMIN DASHBOARD
# ─────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html")


# ─────────────────────────────────────────────
# DRIVER MANAGEMENT API
# ─────────────────────────────────────────────

@app.route("/admin/drivers")
@admin_required
def admin_get_drivers():
    search   = request.args.get("search", "").strip()
    sort     = request.args.get("sort", "newest")
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(50, int(request.args.get("limit", 10))))

    source = get_driver_source()
    if source == "db":
        drivers, total = _drivers_get_all_db(search, sort, page, per_page)
    else:
        drivers, total = _drivers_get_all_excel(search, sort, page, per_page)

    return jsonify({
        "source":      source,
        "drivers":     drivers,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": max(1, -(-total // per_page))
    })


@app.route("/admin/add-driver", methods=["POST"])
@admin_required
def admin_add_driver():
    data  = request.get_json(silent=True) or {}
    phone, err = _validate_phone(data.get("phone", ""))
    if err:
        return jsonify({"error": err}), 400

    name   = str(data.get("name", "")).strip()
    source = get_driver_source()
    ok, err = (_add_driver_db if source == "db" else _add_driver_excel)(phone, name)

    if not ok:
        return jsonify({"error": err}), 409

    AUTHORIZED_DRIVERS.add(phone)
    return jsonify({"status": "ok", "phone": phone, "source": source})


@app.route("/admin/edit-driver/<int:driver_id>", methods=["PUT"])
@admin_required
def admin_edit_driver(driver_id):
    data      = request.get_json(silent=True) or {}
    new_phone, err = _validate_phone(data.get("phone", ""))
    if err:
        return jsonify({"error": err}), 400

    new_name = str(data.get("name", "")).strip()
    source   = get_driver_source()

    if source == "db":
        ok, err, old_phone = _update_driver_db(driver_id, new_phone, new_name)
    else:
        ok, err, old_phone = _update_driver_excel(driver_id, new_phone, new_name)

    if not ok:
        return jsonify({"error": err}), 404 if "not found" in (err or "").lower() else 409

    if old_phone:
        AUTHORIZED_DRIVERS.discard(old_phone)
    AUTHORIZED_DRIVERS.add(new_phone)
    return jsonify({"status": "ok", "source": source})


@app.route("/admin/delete-driver/<int:driver_id>", methods=["DELETE"])
@admin_required
def admin_delete_driver(driver_id):
    source = get_driver_source()

    if source == "db":
        ok, err, old_phone = _delete_driver_db(driver_id)
    else:
        ok, err, old_phone = _delete_driver_excel(driver_id)

    if not ok:
        return jsonify({"error": err}), 404

    if old_phone:
        AUTHORIZED_DRIVERS.discard(old_phone)
    return jsonify({"status": "ok", "source": source})


@app.route("/admin/driver-source")
@admin_required
def admin_driver_source():
    return jsonify({"source": get_driver_source()})


# ─────────────────────────────────────────────
# BUS ROUTE RECOMMENDATION
# ─────────────────────────────────────────────

@app.route("/get-route")
def get_route():
    """
    GET /get-route?bus_no=12

    Returns the recommended route area for a given bus number,
    looked up from bus_details.xlsx (loaded into BUS_ROUTE_MAP at startup).

    Response (found):    { "bus_no": "12", "bus_route": "Velachery", "found": true }
    Response (not found):{ "bus_no": "99", "bus_route": "",          "found": false }
    """
    bus_no = str(request.args.get("bus_no", "")).strip()

    if not bus_no:
        return jsonify({"error": "bus_no is required"}), 400

    route_area = BUS_ROUTE_MAP.get(bus_no, "")
    return jsonify({
        "bus_no":    bus_no,
        "bus_route": route_area,
        "found":     bool(route_area)
    })


# ─────────────────────────────────────────────
# DRIVER LOGIN / PAGE / LOGOUT
# ─────────────────────────────────────────────

@app.route("/driver_login", methods=["GET", "POST"])
def driver_login():
    if request.method == "GET":
        session.clear()
        return render_template("login.html")

    phone = request.form.get("phone")
    otp   = request.form.get("otp")

    if not phone or not otp:
        return render_template("login.html", error="Enter phone and OTP")

    drv = Driver.query.filter_by(phone=phone).first()
    if not drv or drv.otp != otp:
        return render_template("login.html", error="Invalid OTP")

    if drv.otp_created and datetime.utcnow() - drv.otp_created > timedelta(minutes=5):
        return render_template("login.html", error="OTP expired")

    drv.logged_in = True
    db.session.commit()
    session["driver_phone"] = phone
    return redirect("/driver")


@app.route("/driver")
def driver_page():
    phone = session.get("driver_phone")
    if not phone:
        return redirect("/driver_login")

    drv = Driver.query.filter_by(phone=phone).first()
    if not drv or not drv.logged_in:
        session.clear()
        return redirect("/driver_login")

    return render_template("driver.html")


@app.route("/logout")
def logout():
    phone = session.get("driver_phone")
    if phone:
        drv = Driver.query.filter_by(phone=phone).first()
        if drv:
            drv.logged_in = False
            db.session.commit()
    session.clear()
    return redirect("/")


# ─────────────────────────────────────────────
# SEND OTP
# ─────────────────────────────────────────────

@app.route("/send_otp", methods=["POST"])
def send_otp():
    try:
        data  = request.get_json(silent=True) or {}
        phone = _normalize_phone(data.get("phone", ""))

        if not phone:
            return jsonify({"error": "Phone required"}), 400
        if phone not in AUTHORIZED_DRIVERS:
            return jsonify({"error": "Driver not authorized"}), 403

        otp = str(random.randint(1000, 9999))
        drv = Driver.query.filter_by(phone=phone).first()
        if not drv:
            drv = Driver(phone=phone, otp=otp,
                         otp_created=datetime.utcnow(), logged_in=False)
            db.session.add(drv)
        else:
            drv.otp         = otp
            drv.otp_created = datetime.utcnow()

        db.session.commit()
        return jsonify({"msg": "OTP generated", "otp": otp})

    except Exception as e:
        print("OTP ERROR:", e)
        return jsonify({"error": "OTP failed"}), 500


# ─────────────────────────────────────────────
# BUS LOCATION UPDATE
# ─────────────────────────────────────────────

@app.route("/location", methods=["POST"])
def location():
    try:
        data     = request.get_json(silent=True) or {}
        required = ("route", "busType", "lat", "lng")

        if not all(k in data for k in required):
            return jsonify({"error": "Invalid data"}), 400

        route     = data["route"]
        bus_route = data.get("busRoute", "")   # optional route area
        bus       = BusLocation.query.filter_by(route=route).first()

        if bus:
            bus.lat       = data["lat"]
            bus.lng       = data["lng"]
            bus.bus_type  = data["busType"]
            bus.bus_route = bus_route
            bus.timestamp = datetime.utcnow()
            bus.active    = True
        else:
            bus = BusLocation(
                route=route, bus_type=data["busType"],
                bus_route=bus_route,
                lat=data["lat"], lng=data["lng"],
                timestamp=datetime.utcnow(), active=True
            )
            db.session.add(bus)

        db.session.commit()
        return jsonify({"status": "ok"})

    except Exception as e:
        print("LOCATION ERROR:", e)
        return jsonify({"error": "location failed"}), 500


# ─────────────────────────────────────────────
# GET BUS LOCATIONS
# ─────────────────────────────────────────────

@app.route("/get_locations")
def get_locations():
    buses  = BusLocation.query.all()
    result = []

    for bus in buses:
        diff      = datetime.utcnow() - bus.timestamp
        last_seen = int(diff.total_seconds())

        if last_seen > 60:
            if bus.active:
                bus.active = False
        else:
            bus.active = True

        result.append({
            "route":    bus.route,
            "busType":  bus.bus_type,
            "busRoute": bus.bus_route or "",
            "lat":      bus.lat,
            "lng":      bus.lng,
            "lastSeen": last_seen,
            "active":   bus.active
        })

    db.session.commit()
    return jsonify(result)


# ─────────────────────────────────────────────
# END TRIP
# ─────────────────────────────────────────────

@app.route("/end_trip", methods=["POST"])
def end_trip():
    data  = request.get_json(silent=True) or {}
    route = data.get("route")

    bus = BusLocation.query.filter_by(route=route).first()
    if bus:
        bus.active = False
        bus.lat    = None
        bus.lng    = None

    db.session.commit()
    return jsonify({"status": "trip ended"})
