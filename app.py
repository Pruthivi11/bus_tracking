from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_                          # Change 4: explicit or_ import
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
# Change 1: Enforce DATABASE_URL — raise at startup if missing.
#            No SQLite fallback, no silent None.
# ─────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Set it to a PostgreSQL connection string before starting the app."
    )

# Render.com historically returned postgres:// — normalise to postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"]    = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Change 6: Secure session cookies
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["SESSION_COOKIE_SECURE"]   = True        # requires HTTPS in production
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

db = SQLAlchemy(app)

# ─────────────────────────────────────────────
# CORS
#
# FRONTEND_ORIGIN env var controls which origin is allowed.
# Set it to your Render frontend URL in production, e.g.:
#   FRONTEND_ORIGIN=https://commute-assistant.onrender.com
#
# Falls back to "*" (all origins) for local development.
# supports_credentials must be False when origins="*";
# set an explicit origin to enable credentials.
# ─────────────────────────────────────────────

FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*")

_cors_kwargs = {
    "resources":     {r"/*": {"origins": FRONTEND_ORIGIN}},
    "methods":       ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "Authorization"],
}

# credentials (cookies) can only be supported with a specific origin, not "*"
if FRONTEND_ORIGIN != "*":
    _cors_kwargs["supports_credentials"] = True

CORS(app, **_cors_kwargs)

MAPBOX_KEY       = os.environ.get("MAPBOX_KEY")
ADMIN_PASSWORD   = os.environ.get("ADMIN_PASSWORD", "admin@123")
EXCEL_PATH       = "drivers.xlsx"
BUS_DETAILS_PATH = "bus_details.xlsx"   # seed source only — runtime data lives in DB

# ─────────────────────────────────────────────
# BUS INACTIVITY THRESHOLD
#
# A bus is considered inactive when its last location update
# is older than this many seconds.
#
# 300s (5 min) is generous enough to survive Render network
# latency, GPS poll gaps, and brief connectivity drops.
# The /location endpoint sets active=True and updates the timestamp
# on every driver GPS ping — this threshold only governs read logic.
# ─────────────────────────────────────────────

INACTIVITY_THRESHOLD_SECONDS = int(
    os.environ.get("INACTIVITY_THRESHOLD_SECONDS", "300")
)

# ─────────────────────────────────────────────
# BACKEND URL
#
# Used by frontend JS to construct absolute API calls.
# Set BACKEND_URL env var on Render to your service URL, e.g.:
#   BACKEND_URL=https://commute-backend.onrender.com
#
# Falls back to empty string, which means relative URLs —
# correct when frontend and backend are served from the same origin.
# ─────────────────────────────────────────────

BACKEND_URL = os.environ.get("BACKEND_URL", "")

# ─────────────────────────────────────────────
# BUS ROUTE CACHE  (cache-aside, backed by PostgreSQL)
#
# Architecture:
#   1. /get-route calls get_bus_route(bus_no)
#   2. get_bus_route checks BUS_ROUTE_CACHE first  (O(1) dict lookup)
#   3. On cache miss → query BusDetails table → store result in cache
#   4. Cache is pre-warmed at startup from the full BusDetails table
#   5. Admin can force-flush via POST /admin/bus-routes/reload-cache
#   6. Add/edit/delete bus route endpoints update cache immediately (Change 5)
# ─────────────────────────────────────────────

BUS_ROUTE_CACHE: dict = {}   # bus_no (str) → bus_route (str)


# ─────────────────────────────────────────────
# DATA SOURCE DETECTION
# Change 2: Derive source from the configured URI, not from DATABASE_URL env var.
# ─────────────────────────────────────────────

def get_driver_source() -> str:
    """
    Return 'db' when the configured database is PostgreSQL, else 'excel'.
    Reads the actual SQLALCHEMY_DATABASE_URI so it always reflects what
    SQLAlchemy is connected to, not just what was in the environment.
    """
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    return "db" if uri.startswith("postgresql") else "excel"


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
    Seeded automatically from Excel on first run.
    """
    __tablename__ = "authorized_driver"
    id         = db.Column(db.Integer, primary_key=True)
    phone      = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name       = db.Column(db.String(100), default="")
    # Change 3: index=True on created_at — used for ORDER BY in paginated queries
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class BusLocation(db.Model):
    __tablename__ = "bus_location"
    id        = db.Column(db.Integer, primary_key=True)
    route     = db.Column(db.String(20), unique=True, nullable=False, index=True)
    bus_type  = db.Column(db.String(20))
    bus_route = db.Column(db.String(100), nullable=True)   # route area, e.g. "Velachery"
    lat       = db.Column(db.Float)
    lng       = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)   # already indexed
    active    = db.Column(db.Boolean, default=False)


class Onboard(db.Model):
    __tablename__ = "onboard"
    id        = db.Column(db.Integer, primary_key=True)
    roll_no   = db.Column(db.String(20), nullable=False, index=True)
    bus_route = db.Column(db.String(20), nullable=False, index=True)
    onboard   = db.Column(db.Boolean, default=False)
    # Change 3: index=True on timestamp — queried when checking recent onboard events
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class BusDetails(db.Model):
    """
    Master list of bus numbers and their route areas.
    Seeded once from bus_details.xlsx; managed via admin API thereafter.
    PostgreSQL source of truth for route recommendations.
    """
    __tablename__ = "bus_details"
    id        = db.Column(db.Integer, primary_key=True)
    bus_no    = db.Column(db.String(50),  nullable=False, unique=True, index=True)
    bus_route = db.Column(db.String(255), nullable=False)

    def to_dict(self):
        return {"id": self.id, "bus_no": self.bus_no, "bus_route": self.bus_route}


# ─────────────────────────────────────────────
# PHONE UTILITIES
# ─────────────────────────────────────────────

def _normalize_phone(raw) -> str:
    p = str(raw).strip()
    p = p.replace("+91", "").replace(" ", "").replace("-", "").replace(".0", "")
    return p


def _validate_phone(raw):
    """Return (cleaned_phone, error). Valid = 10-digit Indian mobile (starts 6-9)."""
    p = _normalize_phone(raw)
    if not re.fullmatch(r"[6-9]\d{9}", p):
        return None, "Phone must be a valid 10-digit Indian mobile number (starts with 6-9)."
    return p, None


def _normalize_route(raw) -> str:
    """
    Normalise a bus/route number to a canonical form used everywhere:
    strip whitespace, convert to UPPERCASE.

    Applied identically in:
      - /location          (driver write)
      - /end_trip          (driver write)
      - /get_locations     (read + comparison)
      - /get-route         (bus details lookup)
      - student.js         (frontend comparison, same logic mirrored in JS)

    This prevents "12a" vs "12A", "Route 5" vs "route 5",
    or trailing-space mismatches from breaking the active check.
    """
    return str(raw).strip().upper()


def is_bus_active(last_updated) -> bool:
    """
    Single source of truth for bus activity.

    A bus is ACTIVE if it sent a location update within the last
    INACTIVITY_THRESHOLD_SECONDS seconds (default: 300 / 5 minutes).

    Uses timezone-naive UTC comparison throughout.
    Postgres may store timestamps with or without timezone info;
    we strip tzinfo from the stored value before comparing to
    prevent TypeError: can't compare offset-naive and offset-aware datetimes.
    """
    if not last_updated:
        print("[is_bus_active] last_updated is None → inactive")
        return False

    # Strip timezone info if present (Render Postgres returns tz-aware datetimes)
    if hasattr(last_updated, "tzinfo") and last_updated.tzinfo is not None:
        last_updated = last_updated.replace(tzinfo=None)

    age_seconds = (datetime.utcnow() - last_updated).total_seconds()
    active       = age_seconds < INACTIVITY_THRESHOLD_SECONDS

    print(
        f"[is_bus_active] last_updated={last_updated.isoformat()} "
        f"age={age_seconds:.1f}s threshold={INACTIVITY_THRESHOLD_SECONDS}s → {active}"
    )
    return active


# ─────────────────────────────────────────────
# COLUMN MIGRATION HELPER
#
# db.create_all() creates missing tables but NEVER alters existing ones.
# Any column added to a SQLAlchemy model after initial deployment will be
# present in Python but absent from the live PostgreSQL table, causing
# "UndefinedColumn" errors at runtime.
#
# _run_column_migrations() closes this gap by:
#   1. Inspecting the live DB schema via SQLAlchemy's Inspector
#   2. Comparing it against each model's column definitions
#   3. Issuing ALTER TABLE … ADD COLUMN IF NOT EXISTS for any gap
#
# This is idempotent — safe to call on every app restart.
# It preserves all existing data and never drops or modifies columns.
# ─────────────────────────────────────────────

def _run_column_migrations():
    """
    Detect and add any model columns that are missing from the live DB.
    Runs once at startup inside the app context, after db.create_all().
    """
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(db.engine)

    # Map each SQLAlchemy model to its expected columns.
    # Add new entries here whenever a column is added to a model.
    models_to_check = [
        BusLocation,
        AuthorizedDriver,
        BusDetails,
        Onboard,
        Driver,
    ]

    # PostgreSQL type map: SQLAlchemy type class → SQL type string
    _type_map = {
        "String":   lambda col: f"VARCHAR({col.type.length or 255})",
        "Text":     lambda col: "TEXT",
        "Integer":  lambda col: "INTEGER",
        "Float":    lambda col: "DOUBLE PRECISION",
        "Boolean":  lambda col: "BOOLEAN",
        "DateTime": lambda col: "TIMESTAMP WITHOUT TIME ZONE",
    }

    for model in models_to_check:
        table_name = model.__tablename__

        # Skip tables that don't exist yet — db.create_all() will create them
        if not inspector.has_table(table_name):
            print(f"[migration] table '{table_name}' not found — skipping (create_all handles it)")
            continue

        existing_columns = {
            col["name"] for col in inspector.get_columns(table_name)
        }

        for col in model.__table__.columns:
            if col.name in existing_columns:
                continue  # column already present — nothing to do

            # Determine the SQL type string
            type_name = type(col.type).__name__
            sql_type  = _type_map.get(type_name, lambda c: "TEXT")(col)

            nullable_clause = "" if col.nullable else " NOT NULL"

            # Build a safe default clause so NOT NULL columns can be added
            # to tables that may already have rows
            default_clause = ""
            if not col.nullable:
                if   type_name == "Boolean":  default_clause = " DEFAULT FALSE"
                elif type_name == "Integer":  default_clause = " DEFAULT 0"
                elif type_name == "Float":    default_clause = " DEFAULT 0.0"
                else:                         default_clause = " DEFAULT ''"

            alter_sql = (
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN IF NOT EXISTS {col.name} {sql_type}"
                f"{default_clause}{nullable_clause}"
            )

            try:
                db.session.execute(text(alter_sql))
                db.session.commit()
                print(f"[migration] ✅ added column '{table_name}.{col.name}' ({sql_type})")
            except Exception as e:
                db.session.rollback()
                print(f"[migration] ❌ failed to add '{table_name}.{col.name}': {e}")

    print("[migration] column migration check complete")


# ─────────────────────────────────────────────
# BUS DETAILS — SEED + CACHE HELPERS
# ─────────────────────────────────────────────

def _seed_bus_details_from_excel():
    """
    One-time migration: read bus_details.xlsx and insert rows into BusDetails table.
    Skips rows where bus_no already exists (safe to call repeatedly).
    """
    try:
        df = pd.read_excel(BUS_DETAILS_PATH)
        df["bus_no"]    = df["bus_no"].astype(str).str.strip()
        df["bus_route"] = df["bus_route"].astype(str).str.strip()

        inserted = 0
        for _, row in df.iterrows():
            # Change 7: use str().strip() to guard against any NaN/None values
            bus_no    = str(row["bus_no"]).strip()
            bus_route = str(row["bus_route"]).strip()
            if bus_no and bus_route:
                if not BusDetails.query.filter_by(bus_no=bus_no).first():
                    db.session.add(BusDetails(bus_no=bus_no, bus_route=bus_route))
                    inserted += 1

        db.session.commit()
        print(f"BusDetails seeded: {inserted} new rows imported from {BUS_DETAILS_PATH}.")
    except FileNotFoundError:
        print(f"[INFO] {BUS_DETAILS_PATH} not found — BusDetails table not seeded.")
    except Exception as e:
        print(f"BusDetails seed error: {e}")


def _warm_bus_route_cache():
    """
    Pre-warm BUS_ROUTE_CACHE from the BusDetails table.
    Called once at startup and by POST /admin/bus-routes/reload-cache.
    DATABASE_URL is now required, so there is no Excel fallback here.
    """
    global BUS_ROUTE_CACHE
    try:
        rows = BusDetails.query.all()
        BUS_ROUTE_CACHE = {r.bus_no: r.bus_route for r in rows}
        print(f"Bus route cache warmed: {len(BUS_ROUTE_CACHE)} entries from DB.")
    except Exception as e:
        print(f"Cache warm from DB failed: {e}")


def get_bus_route(bus_no) -> str | None:
    """
    Cache-aside lookup for a bus route area.

    1. Normalise input with str().strip()  (Change 7 — prevents NoneType crash)
    2. Check BUS_ROUTE_CACHE              → return immediately on hit  (O(1))
    3. On miss → query BusDetails DB table
    4. Store result in cache for subsequent requests
    5. Return None if not found anywhere
    """
    # Change 7: guard against None / non-string input
    bus_no = str(bus_no).strip()

    if not bus_no:
        return None

    # ── Cache hit ──
    if bus_no in BUS_ROUTE_CACHE:
        return BUS_ROUTE_CACHE[bus_no]

    # ── Cache miss → DB lookup ──
    try:
        record = BusDetails.query.filter_by(bus_no=bus_no).first()
        if record:
            BUS_ROUTE_CACHE[bus_no] = record.bus_route   # populate cache on miss
            return record.bus_route
    except Exception as e:
        print(f"BusDetails DB lookup error: {e}")

    return None


# ─────────────────────────────────────────────
# STARTUP: create tables + load authorised drivers
# ─────────────────────────────────────────────

AUTHORIZED_DRIVERS: set = set()    # in-memory set used by send_otp for fast lookups


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
    """
    Populate AUTHORIZED_DRIVERS in-memory set from the active data source.
    Since DATABASE_URL is now required, 'db' is always the source when
    the URI starts with 'postgresql'. Excel fallback still exists for
    local development without a full Postgres setup.
    """
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

    # ── Step 1: create any tables that are completely missing ──
    db.create_all()

    # ── Step 2: add any columns missing from existing tables ──
    # db.create_all() never alters existing tables, so columns added to
    # SQLAlchemy models after the initial deployment are silently absent
    # from the live DB. This migration function closes that gap safely.
    _run_column_migrations()

    # ── Step 3: seed and cache ──
    load_drivers()
    if BusDetails.query.count() == 0:
        _seed_bus_details_from_excel()
    _warm_bus_route_cache()


# ─────────────────────────────────────────────
# DRIVER CRUD HELPERS  (data-source-aware)
# ─────────────────────────────────────────────

def _drivers_get_all_db(search, sort, page, per_page):
    q = AuthorizedDriver.query
    if search:
        t = f"%{search}%"
        # Change 4: use imported or_() instead of db.or_()
        q = q.filter(or_(
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

    total   = len(df)
    start   = (page - 1) * per_page
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
    others    = df.drop(index=idx)
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
    df        = df.drop(index=idx).reset_index(drop=True)
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
    return render_template("student.html", backend_url=BACKEND_URL)


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
    data       = request.get_json(silent=True) or {}
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
    data           = request.get_json(silent=True) or {}
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
def get_route_api():
    """
    GET /get-route?bus_no=12

    Returns the recommended route area for a given bus number.
    Lookup order: BUS_ROUTE_CACHE (O(1)) → BusDetails DB → not found.

    Response (found):    { "bus_no": "12", "bus_route": "Velachery", "found": true }
    Response (not found):{ "bus_no": "99", "bus_route": "",          "found": false }
    """
    # Change 7: str() + strip() guards against None from missing query param
    bus_no = str(request.args.get("bus_no", "")).strip()

    if not bus_no:
        return jsonify({"error": "bus_no is required"}), 400

    route_area = get_bus_route(bus_no)
    return jsonify({
        "bus_no":    bus_no,
        "bus_route": route_area or "",
        "found":     bool(route_area)
    })


@app.route("/routes")
def routes_autocomplete():
    """
    GET /routes?q=vel

    Returns a sorted list of route area names that contain the query string.
    Used by the driver dashboard autocomplete dropdown.
    Reads from BUS_ROUTE_CACHE (in-memory, O(n) scan) — no DB hit.

    Response: ["Tambaram", "Velachery"] (max 8 results, alphabetically sorted)
    """
    query = str(request.args.get("q", "")).strip().lower()

    if not query:
        # Return all routes when query is empty (useful for initial dropdown)
        matches = sorted(set(BUS_ROUTE_CACHE.values()))[:8]
    else:
        matches = sorted(
            {v for v in BUS_ROUTE_CACHE.values()
             if query in v.lower()},
        )[:8]

    return jsonify(matches)


@app.route("/search_routes")
def search_routes():
    """
    GET /search_routes?q=<input>

    Unified route search for the student dashboard.
    Matches against BOTH route_no and route_area (bidirectional).

    Matching rules:
      - route_no   startsWith(q)   (e.g. "2" → matches "22", "2A")
      - route_area contains(q)     (e.g. "vel" → matches "Velachery")

    Priority order in results:
      1. Currently ACTIVE buses  (driver is live right now)
      2. All other known routes  (from BUS_ROUTE_CACHE / bus_details)

    Response: [
      { "route_no": "22", "route_area": "Karayanchavadi", "is_active": true },
      { "route_no": "5",  "route_area": "Tambaram",       "is_active": false }
    ]
    Max 10 results.
    """
    q = str(request.args.get("q", "")).strip().lower()

    # ── Build full dataset: merge cache with any active bus data ──
    # BUS_ROUTE_CACHE: { route_no → route_area }
    # Start with all known routes from cache
    all_routes = {
        str(rno).upper(): str(area)
        for rno, area in BUS_ROUTE_CACHE.items()
    }

    # Overlay live bus data from DB so active buses reflect any runtime changes
    try:
        live_buses = BusLocation.query.all()
        active_route_nos = set()
        for bus in live_buses:
            rno = str(bus.route).upper()
            # Include route area from live data if it exists
            if bus.bus_route:
                all_routes[rno] = bus.bus_route
            elif rno not in all_routes:
                all_routes[rno] = ""
            if is_bus_active(bus.timestamp) and bool(bus.active):
                active_route_nos.add(rno)
    except Exception as e:
        print(f"[search_routes] live bus query error (non-fatal): {e}")
        active_route_nos = set()

    # ── Filter by query ──
    results = []
    for route_no, route_area in all_routes.items():
        if not q:
            # Empty query → return all (up to limit)
            results.append((route_no, route_area))
        else:
            matches_no   = route_no.lower().startswith(q)
            matches_area = q in route_area.lower()
            if matches_no or matches_area:
                results.append((route_no, route_area))

    # ── Sort: active buses first, then alphabetically by route_no ──
    def sort_key(item):
        rno, _ = item
        return (0 if rno in active_route_nos else 1, rno)

    results.sort(key=sort_key)
    results = results[:10]

    return jsonify([
        {
            "route_no":   rno,
            "route_area": area,
            "is_active":  rno in active_route_nos
        }
        for rno, area in results
    ])


# ─────────────────────────────────────────────
# ADMIN — BUS DETAILS MANAGEMENT
# ─────────────────────────────────────────────

@app.route("/admin/bus-routes")
@admin_required
def admin_get_bus_routes():
    """
    GET /admin/bus-routes?search=&page=1&limit=20
    Returns paginated bus details with current cache size.
    """
    search   = request.args.get("search", "").strip()
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(100, int(request.args.get("limit", 20))))

    q = BusDetails.query

    if search:
        term = f"%{search}%"
        # Change 4: use imported or_() instead of db.or_()
        q = q.filter(or_(
            BusDetails.bus_no.ilike(term),
            BusDetails.bus_route.ilike(term)
        ))

    q     = q.order_by(BusDetails.bus_no.asc())
    total = q.count()
    rows  = q.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "routes":      [r.to_dict() for r in rows],
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": max(1, -(-total // per_page)),
        "cache_size":  len(BUS_ROUTE_CACHE)
    })


@app.route("/admin/bus-routes/add", methods=["POST"])
@admin_required
def admin_add_bus_route():
    """
    POST /admin/bus-routes/add
    Body: { "bus_no": "16", "bus_route": "Sholinganallur" }

    Inserts a new BusDetails record and immediately updates BUS_ROUTE_CACHE
    so the cache stays consistent without requiring a manual reload. (Change 5)
    """
    data      = request.get_json(silent=True) or {}
    # Change 7: str().strip() on both fields
    bus_no    = str(data.get("bus_no",    "")).strip()
    bus_route = str(data.get("bus_route", "")).strip()

    if not bus_no or not bus_route:
        return jsonify({"error": "bus_no and bus_route are required"}), 400

    if BusDetails.query.filter_by(bus_no=bus_no).first():
        return jsonify({"error": f"Bus number '{bus_no}' already exists."}), 409

    record = BusDetails(bus_no=bus_no, bus_route=bus_route)
    db.session.add(record)
    db.session.commit()

    # Change 5: update cache immediately on write
    BUS_ROUTE_CACHE[bus_no] = bus_route

    return jsonify({"status": "ok", "bus_no": bus_no, "bus_route": bus_route})


@app.route("/admin/bus-routes/<int:route_id>", methods=["PUT"])
@admin_required
def admin_edit_bus_route(route_id):
    """
    PUT /admin/bus-routes/<id>
    Body: { "bus_no": "16", "bus_route": "New Area" }

    Updates a BusDetails record and immediately syncs BUS_ROUTE_CACHE. (Change 5)
    Removes the old bus_no key from cache if it changed.
    """
    data      = request.get_json(silent=True) or {}
    # Change 7: str().strip() on both fields
    new_bus_no    = str(data.get("bus_no",    "")).strip()
    new_bus_route = str(data.get("bus_route", "")).strip()

    if not new_bus_no or not new_bus_route:
        return jsonify({"error": "bus_no and bus_route are required"}), 400

    record = BusDetails.query.get(route_id)
    if not record:
        return jsonify({"error": "Bus route not found."}), 404

    # Check for duplicate bus_no (excluding this record)
    dup = BusDetails.query.filter(
        BusDetails.bus_no == new_bus_no,
        BusDetails.id != route_id
    ).first()
    if dup:
        return jsonify({"error": f"Bus number '{new_bus_no}' already used by another route."}), 409

    old_bus_no = record.bus_no
    record.bus_no    = new_bus_no
    record.bus_route = new_bus_route
    db.session.commit()

    # Change 5: update cache immediately — remove old key, set new key
    if old_bus_no != new_bus_no:
        BUS_ROUTE_CACHE.pop(old_bus_no, None)
    BUS_ROUTE_CACHE[new_bus_no] = new_bus_route

    return jsonify({"status": "ok", "bus_no": new_bus_no, "bus_route": new_bus_route})


@app.route("/admin/bus-routes/<int:route_id>", methods=["DELETE"])
@admin_required
def admin_delete_bus_route(route_id):
    """
    DELETE /admin/bus-routes/<id>

    Deletes a BusDetails record and removes it from BUS_ROUTE_CACHE. (Change 5)
    """
    record = BusDetails.query.get(route_id)
    if not record:
        return jsonify({"error": "Bus route not found."}), 404

    bus_no = record.bus_no
    db.session.delete(record)
    db.session.commit()

    # Change 5: remove from cache immediately on delete
    BUS_ROUTE_CACHE.pop(bus_no, None)

    return jsonify({"status": "ok", "deleted_bus_no": bus_no})


@app.route("/admin/bus-routes/reload-cache", methods=["POST"])
@admin_required
def admin_reload_bus_cache():
    """
    POST /admin/bus-routes/reload-cache
    Flushes and re-warms BUS_ROUTE_CACHE from the DB.
    Useful after bulk edits performed outside the API.
    """
    _warm_bus_route_cache()
    return jsonify({
        "status":     "ok",
        "cache_size": len(BUS_ROUTE_CACHE),
        "message":    f"Cache reloaded — {len(BUS_ROUTE_CACHE)} routes cached."
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

    # Pass backend_url so driver.js can build absolute fetch URLs,
    # matching the same pattern used in student.html.
    return render_template("driver.html", backend_url=BACKEND_URL)


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
            print(f"[location] REJECTED — missing fields. Got keys: {list(data.keys())}")
            return jsonify({"error": "Invalid data"}), 400

        # Normalise route: strip + uppercase (consistent with student-side comparison)
        route     = _normalize_route(data["route"])
        bus_route = str(data.get("busRoute", "")).strip()
        bus_type  = str(data.get("busType",  "")).strip()
        lat       = data["lat"]
        lng       = data["lng"]
        now       = datetime.utcnow()

        print(
            f"[location] RECEIVED — route={route!r} type={bus_type!r} "
            f"lat={lat} lng={lng} utcnow={now.isoformat()}"
        )

        bus = BusLocation.query.filter_by(route=route).first()

        if bus:
            bus.lat       = lat
            bus.lng       = lng
            bus.bus_type  = bus_type
            bus.bus_route = bus_route
            bus.timestamp = now
            bus.active    = True
            print(f"[location] UPDATING existing row id={bus.id} route={route!r}")
        else:
            bus = BusLocation(
                route=route, bus_type=bus_type,
                bus_route=bus_route,
                lat=lat, lng=lng,
                timestamp=now,
                active=True
            )
            db.session.add(bus)
            print(f"[location] INSERTING new row route={route!r}")

        db.session.commit()
        print(f"[location] COMMITTED — route={route!r} active=True timestamp={now.isoformat()}")
        return jsonify({"status": "ok", "route": route, "timestamp": now.isoformat()})

    except Exception as e:
        print(f"[location] ERROR: {e}")
        import traceback; traceback.print_exc()
        db.session.rollback()
        return jsonify({"error": "location failed"}), 500


# ─────────────────────────────────────────────
# GET BUS LOCATIONS
# ─────────────────────────────────────────────

@app.route("/get_locations")
def get_locations():
    """
    READ-ONLY endpoint. Never writes to the database.

    Activity is determined SOLELY by is_bus_active(bus.timestamp).
    The DB `active` flag is NOT used in the activity calculation here.

    Rationale:
      - /location updates timestamp on every GPS ping → is_bus_active = True
      - /end_trip rewinds timestamp past the threshold → is_bus_active = False
      - Combining the flag AND the timestamp created false negatives when the
        flag was stale (e.g. app restart, missed end_trip call).
    """
    print("[get_locations] ── endpoint hit ──")

    try:
        buses  = BusLocation.query.all()
        result = []

        print(f"[get_locations] {len(buses)} row(s) in bus_location table")

        for bus in buses:
            try:
                # Timestamp alone determines activity — single source of truth
                active = is_bus_active(bus.timestamp)

                print(
                    f"[get_locations] route={bus.route!r} "
                    f"db_active={bus.active} "
                    f"timestamp={bus.timestamp!r} "
                    f"computed_active={active}"
                )

                result.append({
                    "route":     bus.route,
                    "busType":   bus.bus_type  or "",
                    "busRoute":  bus.bus_route or "",
                    "lat":       bus.lat,
                    "lng":       bus.lng,
                    "lastSeen":  int((datetime.utcnow() - bus.timestamp).total_seconds())
                                 if bus.timestamp else None,
                    "active":    active,
                    "timestamp": bus.timestamp.isoformat() if bus.timestamp else None,
                })

            except Exception as row_err:
                print(f"[get_locations] ERROR on row id={bus.id}: {row_err}")
                import traceback; traceback.print_exc()
                continue

        print(f"[get_locations] returning {len(result)} record(s)")
        return jsonify(result)

    except Exception as e:
        print(f"[get_locations] FATAL ERROR: {e}")
        import traceback; traceback.print_exc()
        return jsonify([]), 200


# ─────────────────────────────────────────────
# END TRIP
# ─────────────────────────────────────────────

@app.route("/end_trip", methods=["POST"])
def end_trip():
    data  = request.get_json(silent=True) or {}
    route = _normalize_route(data.get("route", ""))

    print(f"[end_trip] route={route!r}")

    if not route:
        return jsonify({"error": "route is required"}), 400

    bus = BusLocation.query.filter_by(route=route).first()
    if bus:
        # Rewind the timestamp far enough past INACTIVITY_THRESHOLD_SECONDS so
        # is_bus_active(bus.timestamp) returns False on the very next student poll.
        # This keeps /end_trip consistent with the timestamp-only activity model.
        bus.timestamp = datetime.utcnow() - timedelta(seconds=INACTIVITY_THRESHOLD_SECONDS + 10)
        bus.active    = False
        bus.lat       = None
        bus.lng       = None
        db.session.commit()
        print(
            f"[end_trip] route={route!r} — timestamp rewound, active=False, "
            f"lat/lng cleared"
        )
    else:
        print(f"[end_trip] route={route!r} not found in DB — nothing to update")

    return jsonify({"status": "trip ended"})


# ─────────────────────────────────────────────
# ONBOARD STATUS
# Called by student.js when proximity detection triggers.
# Previously missing — caused silent 404 errors in the frontend.
# ─────────────────────────────────────────────

@app.route("/onboard", methods=["POST"])
def onboard():
    """
    Records that a student has boarded a bus.
    POST body: { rollNo, busRoute, onboard }
    """
    try:
        data      = request.get_json(silent=True) or {}
        roll_no   = str(data.get("rollNo",   "")).strip()
        bus_route = str(data.get("busRoute", "")).strip()
        is_onboard = bool(data.get("onboard", False))

        if not roll_no or not bus_route:
            return jsonify({"error": "rollNo and busRoute are required"}), 400

        # Upsert: update existing record or create new one
        record = Onboard.query.filter_by(
            roll_no=roll_no, bus_route=bus_route
        ).first()

        if record:
            record.onboard   = is_onboard
            record.timestamp = datetime.utcnow()
        else:
            record = Onboard(
                roll_no=roll_no,
                bus_route=bus_route,
                onboard=is_onboard,
                timestamp=datetime.utcnow()
            )
            db.session.add(record)

        db.session.commit()
        print(f"[onboard] roll={roll_no} route={bus_route} onboard={is_onboard}")
        return jsonify({"status": "ok"})

    except Exception as e:
        print(f"[onboard] ERROR: {e}")
        db.session.rollback()
        return jsonify({"error": "onboard update failed"}), 500
