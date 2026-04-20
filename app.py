from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from datetime import datetime, timedelta
from functools import wraps
import os, re, logging, threading
import pandas as pd
import random
import json as _json

# ─────────────────────────────────────────────
# LOGGING
#
# Structured logger used throughout the app instead of print().
# Logs to stderr so Render's log aggregator captures everything,
# AND to app.log for persistent local storage.
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                 # stderr → Render logs
        logging.FileHandler("app.log"),          # persistent local log file
    ],
)
logger = logging.getLogger("commute")

# ─────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key")

# ─────────────────────────────────────────────
# DATABASE CONFIG
# Enforce DATABASE_URL — raise at startup if missing.
# No SQLite fallback, no silent None.
# Supabase (PostgreSQL) requires SSL — sslmode=require is mandatory.
# ─────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Set it to a PostgreSQL connection string before starting the app."
    )

# Normalise postgres:// → postgresql:// (Render + Supabase both use the older prefix)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"]        = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Connection pooling + Supabase SSL config.
#
# pool_size:    persistent connections kept open
# max_overflow: extra connections allowed above pool_size
# pool_timeout: seconds to wait for a connection before raising
# pool_recycle: recycle after this many seconds (avoids stale/idle-timeout drops)
# connect_args: sslmode=require is mandatory for Supabase; safe for any
#               PostgreSQL provider that supports SSL (Render, Neon, etc.)
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_size":    5,
    "max_overflow": 10,
    "pool_timeout": 30,
    "pool_recycle": 1800,
    "connect_args": {
        "sslmode": "require",
    },
}

# Secure session cookies
app.config["SESSION_COOKIE_HTTPONLY"]  = True
app.config["SESSION_COOKIE_SECURE"]   = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

db = SQLAlchemy(app)

# ─────────────────────────────────────────────
# RATE LIMITING  (Change 6)
#
# flask-limiter protects OTP endpoints from abuse.
# Falls back gracefully when flask-limiter is not installed or
# storage is unavailable — the app still functions, just without limits.
# Set RATELIMIT_STORAGE_URL env var to your Redis URL for distributed limiting.
# ─────────────────────────────────────────────

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _limiter_storage = os.environ.get("RATELIMIT_STORAGE_URL") or os.environ.get("REDIS_URL")
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        storage_uri=_limiter_storage or "memory://",
        default_limits=[],       # no default — only apply where explicitly decorated
    )
    logger.info("Rate limiter enabled (storage: %s)",
                "redis" if _limiter_storage else "memory")
except ImportError:
    limiter = None
    logger.warning("flask-limiter not installed — rate limiting disabled")

# ─────────────────────────────────────────────
# REDIS CACHE  (optional — graceful fallback if not configured)
#
# Set REDIS_URL env var to enable caching.
# Render's Redis add-on exposes this automatically.
# When absent or unreachable, all Redis operations are silently
# skipped and the app falls back to direct DB queries.
#
# Cached keys:
#   "bus_locations"          → /get_locations response, TTL 4s
#                              Invalidated by /location and /end_trip writes.
#
# Keys intentionally NOT cached:
#   BUS_ROUTE_CACHE          → already an in-memory Python dict (faster than Redis)
#   /search_routes per-query → live data already from BUS_ROUTE_CACHE + one DB call
# ─────────────────────────────────────────────

redis_client = None

_REDIS_URL = os.environ.get("REDIS_URL")
if _REDIS_URL:
    try:
        import redis as _redis_lib
        redis_client = _redis_lib.from_url(
            _REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        redis_client.ping()
        logger.info("[redis] connected — caching enabled")
    except Exception as _e:
        logger.warning("[redis] connection failed (%s) — running without cache", _e)
        redis_client = None
else:
    logger.info("[redis] REDIS_URL not set — running without cache")

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
# EXCEL SYNC  (DB → Excel, Excel → DB)
#
# Architecture:
#   DB (PostgreSQL) is always the single source of truth.
#   Excel is a mirror/backup that is:
#     • Written automatically after every admin CREATE / UPDATE / DELETE
#     • Read only when the admin explicitly triggers an import
#
# Thread safety:
#   _excel_lock serialises concurrent writes so parallel requests
#   cannot corrupt the file. Writes are dispatched to a background
#   daemon thread so the API response is never blocked.
#
# Exported columns (drivers.xlsx):
#   phone | name | created_at | updated_at
# ─────────────────────────────────────────────

_excel_lock = threading.Lock()


def sync_drivers_to_excel():
    """
    Export all AuthorizedDriver rows to drivers.xlsx.

    Runs in the calling thread — callers use _async_sync_drivers_to_excel()
    when they want fire-and-forget background behaviour.

    PostgreSQL → Excel direction only.  Never reads from Excel.
    """
    try:
        with app.app_context():
            rows = AuthorizedDriver.query.order_by(
                AuthorizedDriver.created_at.asc()
            ).all()

            data = [
                {
                    "phone":      r.phone,
                    "name":       r.name,
                    "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S")
                                  if r.created_at else "",
                    "updated_at": r.updated_at.strftime("%Y-%m-%d %H:%M:%S")
                                  if r.updated_at else "",
                }
                for r in rows
            ]

            df = pd.DataFrame(data, columns=["phone", "name", "created_at", "updated_at"])

            with _excel_lock:
                df.to_excel(EXCEL_PATH, index=False)

            logger.info("[excel-sync] exported %d driver(s) to %s", len(data), EXCEL_PATH)

    except Exception as e:
        logger.error("[excel-sync] export failed: %s", e)


def _async_sync_drivers_to_excel():
    """
    Dispatch sync_drivers_to_excel() on a daemon thread so the API
    response is returned immediately and the Excel write happens in
    the background.  Errors are logged but never raise to the caller.
    """
    t = threading.Thread(target=sync_drivers_to_excel, daemon=True)
    t.start()


def import_drivers_from_excel():
    """
    Read drivers.xlsx and upsert rows into AuthorizedDriver.

    Excel → DB direction only.  This is the MANUAL import path —
    never called automatically.  Returns (inserted, updated, skipped, errors).

    Required columns: phone
    Optional columns: name, created_at, updated_at
    All other columns are silently ignored.

    Validation:
      • phone must be a valid 10-digit Indian mobile number
      • rows with invalid / missing phones are skipped
      • duplicate phones in the file are deduplicated (last row wins)
    """
    inserted = updated = skipped = 0
    errors   = []

    try:
        df = pd.read_excel(EXCEL_PATH)
    except FileNotFoundError:
        return 0, 0, 0, [f"{EXCEL_PATH} not found"]
    except Exception as e:
        return 0, 0, 0, [f"Could not read {EXCEL_PATH}: {e}"]

    # Normalise column names (lowercase, strip)
    df.columns = [c.strip().lower() for c in df.columns]

    if "phone" not in df.columns:
        return 0, 0, 0, ["Excel file must have a 'phone' column"]

    # Fill optional columns with defaults if absent
    if "name" not in df.columns:
        df["name"] = ""

    df["phone"] = df["phone"].astype(str).apply(_normalize_phone)
    df["name"]  = df["name"].fillna("").astype(str).str.strip()

    # Deduplicate: keep last occurrence per phone
    df = df.drop_duplicates(subset="phone", keep="last")

    for _, row in df.iterrows():
        phone = row["phone"]
        name  = row["name"]

        # Validate phone
        import re as _re
        if not _re.fullmatch(r"[6-9]\d{9}", phone):
            skipped += 1
            errors.append(f"Skipped invalid phone: {phone!r}")
            continue

        try:
            existing = AuthorizedDriver.query.filter_by(phone=phone).first()
            now = datetime.utcnow()

            if existing:
                existing.name       = name
                existing.updated_at = now
                updated += 1
            else:
                db.session.add(AuthorizedDriver(
                    phone=phone, name=name,
                    created_at=now, updated_at=now
                ))
                inserted += 1

        except Exception as row_err:
            skipped += 1
            errors.append(f"Row error ({phone}): {row_err}")
            db.session.rollback()
            continue

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return 0, 0, skipped, [f"DB commit failed: {e}"]

    logger.info(
        "[excel-sync] import complete — inserted=%d updated=%d skipped=%d",
        inserted, updated, skipped
    )
    return inserted, updated, skipped, errors

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    # updated_at tracks the last admin change; added via _run_migrations()
    # so it appears safely on pre-existing tables without data loss.
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


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

    __table_args__ = (
        # Composite index accelerates the combined (route, timestamp) access pattern
        # used by /get_locations when ordering or filtering recent updates per route.
        # Created via _run_migrations() at startup using CREATE INDEX IF NOT EXISTS
        # so it is added safely to pre-existing tables without data loss.
        db.Index("idx_bus_location_route_ts", "route", "timestamp"),
    )


class Onboard(db.Model):
    __tablename__ = "onboard"
    id        = db.Column(db.Integer, primary_key=True)
    roll_no   = db.Column(db.String(20), nullable=False, index=True)
    bus_route = db.Column(db.String(20), nullable=False, index=True)
    onboard   = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        # Change 8: Composite index — covers the exact filter used by the upsert
        # query in /onboard: filter_by(roll_no=x, bus_route=y).
        # Faster than two separate single-column index scans.
        db.Index("idx_onboard_roll_route", "roll_no", "bus_route"),
    )


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

    __table_args__ = (
        # Standard B-tree index on bus_route for equality and prefix lookups.
        # A GIN trigram index (for fast ILIKE '%...%') is created separately
        # in _run_migrations() after ensuring the pg_trgm extension is enabled.
        db.Index("idx_bus_details_route_area", "bus_route"),
    )

    def to_dict(self):
        return {"id": self.id, "bus_no": self.bus_no, "bus_route": self.bus_route}


class RouteMapping(db.Model):
    """
    Stores the recorded GPS path for a bus route.

    Lifecycle:
      1. Admin sets is_mapping_allowed=True  → driver can record
      2. Driver starts  → is_mapping_active=True, raw_points accumulates
      3. Driver stops   → raw_points compressed → polyline stored → raw_points cleared
      4. version increments on every completed recording

    raw_points: temporary list of [lat, lng] pairs during an active mapping session.
                Cleared to None after polyline encoding.
    polyline:   Google-encoded polyline string — compact permanent storage.
    """
    __tablename__ = "route_mapping"

    id        = db.Column(db.Integer, primary_key=True)
    route     = db.Column(db.String(20), nullable=False, unique=True, index=True)
    bus_route = db.Column(db.String(100))          # area name, e.g. "Velachery"

    polyline   = db.Column(db.Text)                # encoded polyline — final storage
    raw_points = db.Column(db.JSON)                # [[lat,lng], ...] during mapping only

    is_mapping_allowed = db.Column(db.Boolean, default=False, nullable=False)
    is_mapping_active  = db.Column(db.Boolean, default=False, nullable=False)

    version    = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    """
    return str(raw).strip().upper()


# ─────────────────────────────────────────────
# ROUTE MAPPING UTILITIES
# ─────────────────────────────────────────────

import math as _math

def _haversine_metres(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Return the great-circle distance in metres between two GPS points.
    Uses the Haversine formula — accurate to within ~0.5 % for short distances.
    """
    R = 6_371_000.0          # Earth radius in metres
    phi1 = _math.radians(lat1)
    phi2 = _math.radians(lat2)
    dphi = _math.radians(lat2 - lat1)
    dlam = _math.radians(lng2 - lng1)
    a = (_math.sin(dphi / 2) ** 2
         + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2)
    return R * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1 - a))


def _bearing(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the compass bearing (0–360°) from point 1 to point 2."""
    phi1 = _math.radians(lat1)
    phi2 = _math.radians(lat2)
    dlam = _math.radians(lng2 - lng1)
    x = _math.sin(dlam) * _math.cos(phi2)
    y = (_math.cos(phi1) * _math.sin(phi2)
         - _math.sin(phi1) * _math.cos(phi2) * _math.cos(dlam))
    return (_math.degrees(_math.atan2(x, y)) + 360) % 360


def _bearing_diff(b1: float, b2: float) -> float:
    """Return the absolute angular difference between two bearings (0–180°)."""
    d = abs(b1 - b2) % 360
    return d if d <= 180 else 360 - d


def _should_record_point(raw_points: list,
                          lat: float, lng: float,
                          min_dist_m: float = 15.0,
                          min_turn_deg: float = 10.0) -> bool:
    """
    Return True if the new GPS point is worth recording.

    A point is kept if:
      • It is the very first point, OR
      • Distance from the last saved point ≥ min_dist_m (15 m default), OR
      • Direction change from the previous segment ≥ min_turn_deg (10° default)

    This eliminates GPS noise on straight roads while preserving turn details.
    """
    if not raw_points:
        return True

    last = raw_points[-1]           # [lat, lng]
    dist = _haversine_metres(last[0], last[1], lat, lng)

    if dist >= min_dist_m:
        return True

    if len(raw_points) >= 2:
        prev = raw_points[-2]
        old_bearing = _bearing(prev[0], prev[1], last[0], last[1])
        new_bearing = _bearing(last[0], last[1], lat, lng)
        if _bearing_diff(old_bearing, new_bearing) >= min_turn_deg:
            return True

    return False



def _decode_polyline(polyline_str: str) -> list:
    """
    Decode a Google Encoded Polyline string to a list of [lat, lng] pairs.
    Inverse of _encode_polyline.  Used by /get-route-mapping so the
    frontend receives a plain coordinate array with no JS decoder needed.
    """
    result  = []
    index   = 0
    lat     = 0
    lng     = 0
    n       = len(polyline_str)

    while index < n:
        shift, val = 0, 0
        while True:
            b     = ord(polyline_str[index]) - 63
            index += 1
            val   |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(val >> 1) if (val & 1) else (val >> 1)
        lat += dlat

        shift, val = 0, 0
        while True:
            b     = ord(polyline_str[index]) - 63
            index += 1
            val   |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(val >> 1) if (val & 1) else (val >> 1)
        lng += dlng

        result.append([lat / 1e5, lng / 1e5])

    return result

def _encode_polyline(points: list) -> str:
    """
    Encode a list of [lat, lng] pairs using the Google Encoded Polyline Algorithm.

    The algorithm encodes coordinate deltas as a variable-length ASCII string.
    Each value is multiplied by 1e5, rounded, XOR-shifted, and encoded in
    chunks of 5 bits plus an ASCII offset of 63.

    Reference: https://developers.google.com/maps/documentation/utilities/polylinealgorithm
    """
    def _encode_value(value: int) -> str:
        value = value << 1
        if value < 0:
            value = ~value
        chunks = []
        while value >= 0x20:
            chunks.append(chr((0x20 | (value & 0x1F)) + 63))
            value >>= 5
        chunks.append(chr(value + 63))
        return "".join(chunks)

    result   = []
    prev_lat = 0
    prev_lng = 0

    for point in points:
        lat = round(point[0] * 1e5)
        lng = round(point[1] * 1e5)
        result.append(_encode_value(lat - prev_lat))
        result.append(_encode_value(lng - prev_lng))
        prev_lat, prev_lng = lat, lng

    return "".join(result)


def is_bus_active(last_updated) -> bool:
    """
    Single source of truth for bus activity.

    A bus is ACTIVE if its last location update is within
    INACTIVITY_THRESHOLD_SECONDS + 10 seconds ago.
    The +10 buffer (Change 13) prevents flickering at the boundary
    where a bus updates at exactly the threshold interval.

    Strips tzinfo before comparison to handle both tz-aware
    (Render Postgres) and tz-naive datetimes safely.
    """
    if not last_updated:
        logger.debug("[is_bus_active] last_updated is None → inactive")
        return False

    if hasattr(last_updated, "tzinfo") and last_updated.tzinfo is not None:
        last_updated = last_updated.replace(tzinfo=None)

    age_seconds = (datetime.utcnow() - last_updated).total_seconds()
    active       = age_seconds < (INACTIVITY_THRESHOLD_SECONDS + 10)

    logger.debug(
        "[is_bus_active] last_updated=%s age=%.1fs threshold=%ds → %s",
        last_updated.isoformat(), age_seconds, INACTIVITY_THRESHOLD_SECONDS, active
    )
    return active


# ─────────────────────────────────────────────
# MIGRATION HELPER
#
# Handles two classes of schema drift that db.create_all() cannot fix:
#
#   1. MISSING COLUMNS — columns added to SQLAlchemy models after the
#      initial deployment are absent from the live DB table.
#      Fixed by: ALTER TABLE … ADD COLUMN IF NOT EXISTS
#
#   2. MISSING INDEXES — composite or specialised indexes (GIN trigram)
#      defined in __table_args__ are only created by db.create_all() when
#      the table is first made. Pre-existing tables need explicit SQL.
#      Fixed by: CREATE INDEX IF NOT EXISTS + CREATE EXTENSION IF NOT EXISTS
#
# Both operations are idempotent — safe to run on every app restart.
# No data is dropped or modified.
# ─────────────────────────────────────────────

def _run_migrations():
    """
    Apply any schema changes (columns + indexes) that db.create_all() missed.
    Runs once at startup inside the app context, after db.create_all().
    """
    from sqlalchemy import inspect as sa_inspect, text

    inspector = sa_inspect(db.engine)

    # ── Part A: add missing columns ──────────────────────────────────

    models_to_check = [BusLocation, AuthorizedDriver, BusDetails, Onboard, Driver]

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

        if not inspector.has_table(table_name):
            logger.info("[migration] table '%s' not found — db.create_all() will handle it", table_name)
            continue

        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}

        for col in model.__table__.columns:
            if col.name in existing_columns:
                continue

            type_name       = type(col.type).__name__
            sql_type        = _type_map.get(type_name, lambda c: "TEXT")(col)
            nullable_clause = "" if col.nullable else " NOT NULL"
            default_clause  = ""
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
                logger.info("[migration] ✅ added column '%s.%s' (%s)", table_name, col.name, sql_type)
            except Exception as e:
                db.session.rollback()
                logger.error("[migration] ❌ failed to add '%s.%s': %s", table_name, col.name, e)

    # ── Part B: create missing indexes ───────────────────────────────

    _indexes = [
        (
            "idx_bus_location_route_ts",
            "CREATE INDEX IF NOT EXISTS idx_bus_location_route_ts "
            "ON bus_location (route, timestamp)"
        ),
        (
            "idx_bus_details_route_area",
            "CREATE INDEX IF NOT EXISTS idx_bus_details_route_area "
            "ON bus_details (bus_route)"
        ),
        (
            # Composite index on onboard table — covers filter_by(roll_no, bus_route)
            "idx_onboard_roll_route",
            "CREATE INDEX IF NOT EXISTS idx_onboard_roll_route "
            "ON onboard (roll_no, bus_route)"
        ),
        (
            # Performance index on bus_location.route (single-column).
            # Accelerates /location upsert lookup and /get_locations filter.
            # Supabase/PostgreSQL uses this for row-level scans on active routes.
            "idx_bus_location_route",
            "CREATE INDEX IF NOT EXISTS idx_bus_location_route "
            "ON bus_location (route)"
        ),
    ]

    _trgm_enabled = False
    try:
        db.session.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        db.session.commit()
        _trgm_enabled = True
        logger.info("[migration] pg_trgm extension ready")
    except Exception as e:
        db.session.rollback()
        logger.warning("[migration] pg_trgm not available (non-fatal): %s", e)

    if _trgm_enabled:
        _indexes.append((
            "idx_bus_details_route_area_trgm",
            "CREATE INDEX IF NOT EXISTS idx_bus_details_route_area_trgm "
            "ON bus_details USING gin (bus_route gin_trgm_ops)"
        ))

    existing_indexes = set()
    for table in ("bus_location", "bus_details", "authorized_driver", "onboard", "driver"):
        if inspector.has_table(table):
            for idx in inspector.get_indexes(table):
                existing_indexes.add(idx["name"])

    for index_name, create_sql in _indexes:
        if index_name in existing_indexes:
            logger.info("[migration] index '%s' already exists — skipping", index_name)
            continue
        try:
            db.session.execute(text(create_sql))
            db.session.commit()
            logger.info("[migration] ✅ created index '%s'", index_name)
        except Exception as e:
            db.session.rollback()
            logger.error("[migration] ❌ failed to create index '%s': %s", index_name, e)

    # ── Part C: enforce critical constraints ─────────────────────────
    #
    # Ensure bus_location.route is NOT NULL.
    # The column is declared nullable=False in the model, but pre-existing
    # rows created before this constraint was enforced may contain NULLs.
    # This cleans them up and then sets the column constraint directly in
    # PostgreSQL — safe to run repeatedly (SET NOT NULL is idempotent when
    # the constraint already exists, and the DELETE is a no-op when no NULLs
    # are present).
    #
    # Why route must be NOT NULL:
    #   /location upsert uses filter_by(route=route) — a NULL route row
    #   would never match, accumulate silently, and corrupt tracking state.
    if inspector.has_table("bus_location"):
        try:
            # Remove any null-route rows (safety net before enforcing constraint)
            db.session.execute(text(
                "DELETE FROM bus_location WHERE route IS NULL"
            ))
            db.session.commit()

            # Enforce NOT NULL at DB level (idempotent — no-op if already set)
            db.session.execute(text(
                "ALTER TABLE bus_location "
                "ALTER COLUMN route SET NOT NULL"
            ))
            db.session.commit()
            logger.info("[migration] ✅ bus_location.route NOT NULL constraint enforced")
        except Exception as e:
            db.session.rollback()
            logger.warning("[migration] bus_location.route constraint (non-fatal): %s", e)

    logger.info("[migration] ── migration check complete ──")


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
        logger.info("BusDetails seeded: %d new rows imported from %s", inserted, BUS_DETAILS_PATH)
    except FileNotFoundError:
        logger.info("%s not found — BusDetails table not seeded.", BUS_DETAILS_PATH)
    except Exception as e:
        logger.error("BusDetails seed error: %s", e)


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
        logger.info("Bus route cache warmed: %d entries from DB.", len(BUS_ROUTE_CACHE))
    except Exception as e:
        logger.error("Cache warm from DB failed: %s", e)


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
        logger.warning("BusDetails DB lookup error: %s", e)

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
        logger.info("Seeded AuthorizedDriver table from Excel.")
    except Exception as e:
        logger.error("Excel seed error: %s", e)


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
            logger.error("Excel load error: %s", e)

    logger.info("[%s] Authorized drivers loaded: %s", source.upper(), AUTHORIZED_DRIVERS)


with app.app_context():

    # ── Step 1: create any tables that are completely missing ──
    db.create_all()

    # ── Step 2: apply schema migrations (missing columns + indexes) ──
    # db.create_all() never alters existing tables. _run_migrations() adds
    # any missing columns and creates any missing indexes safely and idempotently.
    _run_migrations()

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
    now = datetime.utcnow()
    db.session.add(AuthorizedDriver(phone=phone, name=name, created_at=now, updated_at=now))
    db.session.commit()
    _async_sync_drivers_to_excel()   # mirror to Excel in background
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

    rec.phone      = new_phone
    rec.name       = new_name
    rec.updated_at = datetime.utcnow()
    db.session.commit()
    _async_sync_drivers_to_excel()   # mirror to Excel in background
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
    _async_sync_drivers_to_excel()   # mirror to Excel in background
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
# DRIVER EXCEL IMPORT / EXPORT
# ─────────────────────────────────────────────

@app.route("/admin/import-drivers", methods=["POST"])
@admin_required
def admin_import_drivers():
    """
    POST /admin/import-drivers

    Reads drivers.xlsx and upserts rows into AuthorizedDriver.
    PostgreSQL is the authority — this is a one-way import tool.

    Response:
      { "status": "ok", "inserted": N, "updated": N, "skipped": N, "errors": [...] }
    """
    inserted, updated, skipped, errors = import_drivers_from_excel()

    # Refresh the in-memory auth set from the updated DB
    global AUTHORIZED_DRIVERS
    try:
        AUTHORIZED_DRIVERS = {
            _normalize_phone(r.phone)
            for r in AuthorizedDriver.query.all()
        }
        logger.info("[import] AUTHORIZED_DRIVERS refreshed — %d entries", len(AUTHORIZED_DRIVERS))
    except Exception as e:
        logger.error("[import] failed to refresh AUTHORIZED_DRIVERS: %s", e)

    status = "ok" if not errors or (inserted + updated) > 0 else "error"
    http_code = 200 if status == "ok" else 400

    return jsonify({
        "status":   status,
        "inserted": inserted,
        "updated":  updated,
        "skipped":  skipped,
        "errors":   errors[:10],   # cap error list to avoid huge response
    }), http_code


@app.route("/admin/export-drivers")
@admin_required
def admin_export_drivers():
    """
    GET /admin/export-drivers

    Generates a fresh drivers.xlsx from the DB and returns it as a
    file download.  This is the on-demand export path — the automatic
    background sync (_async_sync_drivers_to_excel) keeps the file
    current, but this endpoint lets admins pull a guaranteed-fresh copy.
    """
    from flask import send_file
    import io

    try:
        rows = AuthorizedDriver.query.order_by(
            AuthorizedDriver.created_at.asc()
        ).all()

        data = [
            {
                "phone":      r.phone,
                "name":       r.name,
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S")
                              if r.created_at else "",
                "updated_at": r.updated_at.strftime("%Y-%m-%d %H:%M:%S")
                              if r.updated_at else "",
            }
            for r in rows
        ]

        df = pd.DataFrame(data, columns=["phone", "name", "created_at", "updated_at"])

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Drivers")
        buf.seek(0)

        logger.info("[export] sending drivers.xlsx (%d rows) to admin", len(data))

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="drivers.xlsx",
        )

    except Exception as e:
        logger.error("[export] failed: %s", e)
        return jsonify({"error": "Export failed — see server logs"}), 500


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

    # Overlay live bus data from DB — Change 7: with_entities fetches only
    # the four columns we need, avoiding loading lat/lng/id into Python memory.
    try:
        live_buses = BusLocation.query.with_entities(
            BusLocation.route,
            BusLocation.bus_route,
            BusLocation.timestamp,
            BusLocation.active,
        ).all()
        active_route_nos = set()
        for bus in live_buses:
            rno = str(bus.route).upper()
            if bus.bus_route:
                all_routes[rno] = bus.bus_route
            elif rno not in all_routes:
                all_routes[rno] = ""
            if is_bus_active(bus.timestamp) and bool(bus.active):
                active_route_nos.add(rno)
    except Exception as e:
        logger.warning("[search_routes] live bus query error (non-fatal): %s", e)
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

def _rate_limit(limit_str):
    """
    Decorator factory that applies a flask-limiter rate limit when available,
    and is a silent no-op when flask-limiter is not installed.
    """
    def decorator(f):
        if limiter:
            return limiter.limit(limit_str)(f)
        return f
    return decorator


@app.route("/send_otp", methods=["POST"])
@_rate_limit("5 per minute")
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
        logger.error("OTP ERROR: %s", e)
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
            logger.warning("[location] REJECTED — missing fields. Got keys: %s", list(data.keys()))
            return jsonify({"error": "Invalid data"}), 400

        # Normalise route: strip + uppercase (consistent with student-side comparison)
        route     = _normalize_route(data["route"])
        bus_route = str(data.get("busRoute", "")).strip()
        bus_type  = str(data.get("busType",  "")).strip()
        lat       = data["lat"]
        lng       = data["lng"]
        now       = datetime.utcnow()

        logger.info("[location] RECEIVED — route=%r type=%r lat=%s lng=%s utcnow=%s", route, bus_type, lat, lng, now.isoformat())

        # Change 11: with_for_update() prevents a race condition when two GPS
        # pings for the same route arrive simultaneously — the second query
        # waits for the first transaction to commit before reading the row.
        bus = BusLocation.query.filter_by(route=route).with_for_update().first()

        if bus:
            bus.lat       = lat
            bus.lng       = lng
            bus.bus_type  = bus_type
            bus.bus_route = bus_route
            bus.timestamp = now
            bus.active    = True
            logger.info("[location] UPDATING existing row id=%s route=%r", bus.id, route)
        else:
            bus = BusLocation(
                route=route, bus_type=bus_type,
                bus_route=bus_route,
                lat=lat, lng=lng,
                timestamp=now,
                active=True
            )
            db.session.add(bus)
            logger.info("[location] INSERTING new row route=%r", route)

        db.session.commit()
        # Invalidate the bus_locations cache so the next student poll
        # fetches the fresh position from DB rather than stale cached data.
        _cache_delete(_BUS_LOCATIONS_KEY)
        logger.info("[location] COMMITTED — route=%r active=True timestamp=%s", route, now.isoformat())

        # ── Route mapping side-effect (non-blocking) ──────────────────
        # If an active mapping session exists for this route, conditionally
        # append the new GPS point using the smart compression filter.
        # Any error here is logged but NEVER propagates to the response —
        # live tracking must not be affected by mapping logic.
        try:
            from sqlalchemy.orm.attributes import flag_modified
            mapping = RouteMapping.query.filter_by(
                route=route, is_mapping_active=True
            ).first()

            if mapping is not None:
                raw = mapping.raw_points or []
                if _should_record_point(raw, lat, lng):
                    raw = list(raw)   # copy so SQLAlchemy detects the change
                    raw.append([lat, lng])
                    mapping.raw_points = raw
                    mapping.updated_at = now
                    flag_modified(mapping, "raw_points")
                    db.session.commit()
                    logger.debug(
                        "[mapping] route=%r appended point #%d (%.5f, %.5f)",
                        route, len(raw), lat, lng
                    )
        except Exception as map_err:
            db.session.rollback()
            logger.error("[mapping] side-effect error (non-fatal): %s", map_err)

        return jsonify({"status": "ok", "route": route, "timestamp": now.isoformat()})

    except Exception as e:
        logger.error("[location] ERROR: %s", e, exc_info=True)
        import traceback; traceback.print_exc()
        db.session.rollback()
        return jsonify({"error": "location failed"}), 500


# ─────────────────────────────────────────────
# REDIS CACHE HELPERS
# ─────────────────────────────────────────────

import json as _json

# TTL for the bus_locations cache key.
# Slightly shorter than the student polling interval (5s) so that at most
# one DB query fires per student poll cycle when many students are watching.
_BUS_LOCATIONS_TTL  = 4    # seconds
_BUS_LOCATIONS_KEY  = "bus_locations"


def _cache_get(key: str):
    """
    Return parsed JSON from Redis, or None on miss / error / Redis absent.
    Always safe to call — silently returns None if Redis is not configured.
    """
    if not redis_client:
        return None
    try:
        raw = redis_client.get(key)
        return _json.loads(raw) if raw else None
    except Exception as e:
        logger.warning("[redis] GET '%s' error (non-fatal): %s", key, e)
        return None


def _cache_set(key: str, value, ttl: int):
    """
    Serialise value to JSON and store in Redis with TTL.
    Silent no-op if Redis is not configured or unavailable.
    """
    if not redis_client:
        return
    try:
        redis_client.setex(key, ttl, _json.dumps(value))
    except Exception as e:
        logger.warning("[redis] SET '%s' error (non-fatal): %s", key, e)


def _cache_delete(key: str):
    """
    Delete a key from Redis. Silent no-op if Redis is absent.
    Called by write endpoints (/location, /end_trip) to invalidate stale cache.
    """
    if not redis_client:
        return
    try:
        redis_client.delete(key)
        logger.debug("[redis] invalidated '%s'", key)
    except Exception as e:
        logger.warning("[redis] DELETE '%s' error (non-fatal): %s", key, e)


# ─────────────────────────────────────────────
# GET BUS LOCATIONS
# ─────────────────────────────────────────────

@app.route("/get_locations")
def get_locations():
    """
    READ-ONLY endpoint. Never writes to the database.

    Cache strategy:
      - Checks Redis for "bus_locations" key first (TTL 4s).
      - On cache hit: returns immediately — zero DB queries.
      - On cache miss: queries DB, builds result, stores in Redis.
      - Cache is invalidated by /location and /end_trip writes.

    Activity is determined SOLELY by is_bus_active(bus.timestamp).
    The DB `active` flag is NOT used in the activity calculation here.
    """
    logger.debug("[get_locations] ── endpoint hit ──")

    # ── Cache read ──
    cached = _cache_get(_BUS_LOCATIONS_KEY)
    if cached is not None:
        logger.debug("[get_locations] cache HIT — returning %d record(s)", len(cached))
        return jsonify(cached)

    logger.debug("[get_locations] cache MISS — querying DB")

    try:
        # Change 3: filter by cutoff timestamp so ended/old buses that
        # haven't been updated in far longer than the threshold are excluded
        # from the query entirely, reducing per-row work at the Python level.
        cutoff = datetime.utcnow() - timedelta(seconds=INACTIVITY_THRESHOLD_SECONDS + 60)
        buses  = BusLocation.query.filter(BusLocation.timestamp >= cutoff).all()
        result = []

        logger.debug("[get_locations] %d row(s) within active window", len(buses))

        for bus in buses:
            try:
                active = is_bus_active(bus.timestamp)

                logger.debug(
                    "[get_locations] route=%r db_active=%s timestamp=%r computed_active=%s",
                    bus.route, bus.active, bus.timestamp, active
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
                logger.error("[get_locations] ERROR on row id=%s: %s", bus.id, row_err, exc_info=True)
                continue

        # ── Cache write ──
        _cache_set(_BUS_LOCATIONS_KEY, result, _BUS_LOCATIONS_TTL)

        logger.info("[get_locations] returning %d record(s) (stored in cache)", len(result))
        return jsonify(result)

    except Exception as e:
        logger.error("[get_locations] FATAL ERROR: %s", e, exc_info=True)
        return jsonify([]), 200


# Change 12: versioned alias — same view function, both URLs work
# Clients can migrate to /api/v1/get_locations at their own pace.
app.add_url_rule("/api/v1/get_locations", endpoint="get_locations_v1", view_func=get_locations)


# ─────────────────────────────────────────────
# HEALTH CHECK  (Change 10)
# ─────────────────────────────────────────────

@app.route("/health")
def health():
    """
    GET /health

    Returns {"status": "ok"} when the app is reachable.
    Used by Render health checks, load balancers, and monitoring.
    Does NOT query the DB — a shallow liveness probe only.
    """
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────────
# ROUTE MAPPING — ADMIN ENDPOINTS
# ─────────────────────────────────────────────

@app.route("/admin/enable-mapping", methods=["POST"])
@admin_required
def admin_enable_mapping():
    """
    POST /admin/enable-mapping
    Body: { "route": "22", "bus_route": "Karayanchavadi" }

    Grants permission for the driver of this route to record a route mapping.
    Creates the RouteMapping record if it does not yet exist.
    Sets is_mapping_allowed=True, is_mapping_active=False.
    """
    data      = request.get_json(silent=True) or {}
    route     = _normalize_route(data.get("route", ""))
    bus_route = str(data.get("bus_route", "")).strip()

    if not route:
        return jsonify({"error": "route is required"}), 400

    mapping = RouteMapping.query.filter_by(route=route).first()
    now     = datetime.utcnow()

    if mapping:
        mapping.is_mapping_allowed = True
        mapping.is_mapping_active  = False
        mapping.bus_route          = bus_route or mapping.bus_route
        mapping.updated_at         = now
        action = "updated"
    else:
        mapping = RouteMapping(
            route=route,
            bus_route=bus_route,
            is_mapping_allowed=True,
            is_mapping_active=False,
            version=0,
            created_at=now,
            updated_at=now,
        )
        db.session.add(mapping)
        action = "created"

    db.session.commit()
    logger.info("[mapping] admin enabled mapping for route=%r (%s)", route, action)

    return jsonify({
        "status":    "ok",
        "route":     route,
        "action":    action,
        "has_polyline": bool(mapping.polyline),
        "version":   mapping.version,
    })


@app.route("/admin/mapping-status")
@admin_required
def admin_mapping_status():
    """
    GET /admin/mapping-status

    Returns all RouteMapping records for the admin Route Mapping tab.
    Also merges in known routes from BusLocation so routes without a
    mapping record still appear (with allow button).
    """
    # All routes that have ever sent a location
    bus_routes = {
        b.route: b.bus_route or ""
        for b in BusLocation.query.with_entities(
            BusLocation.route, BusLocation.bus_route
        ).all()
    }

    mappings_by_route = {
        m.route: m
        for m in RouteMapping.query.all()
    }

    # Merge: start with all known bus routes, overlay mapping records
    all_routes = dict(bus_routes)
    for r in mappings_by_route:
        if r not in all_routes:
            all_routes[r] = mappings_by_route[r].bus_route or ""

    result = []
    for route, area in sorted(all_routes.items()):
        m = mappings_by_route.get(route)
        result.append({
            "route":               route,
            "bus_route":           area,
            "has_mapping":         m is not None,
            "has_polyline":        bool(m and m.polyline),
            "is_mapping_allowed":  m.is_mapping_allowed if m else False,
            "is_mapping_active":   m.is_mapping_active  if m else False,
            "version":             m.version             if m else 0,
            "point_count":         len(m.raw_points)     if (m and m.raw_points) else 0,
            "updated_at":          m.updated_at.isoformat() if (m and m.updated_at) else None,
        })

    return jsonify(result)


# ─────────────────────────────────────────────
# ROUTE MAPPING — DRIVER ENDPOINTS
# ─────────────────────────────────────────────

@app.route("/driver/mapping-status")
def driver_mapping_status():
    """
    GET /driver/mapping-status?route=22

    Called by driver.js after the driver enters a route number.
    Returns whether mapping is allowed and the current session state.
    No auth required — route number is the key; no sensitive data returned.
    """
    route = _normalize_route(request.args.get("route", ""))
    if not route:
        return jsonify({"allowed": False})

    mapping = RouteMapping.query.filter_by(route=route).first()
    if not mapping or not mapping.is_mapping_allowed:
        return jsonify({"allowed": False})

    return jsonify({
        "allowed":      True,
        "is_active":    mapping.is_mapping_active,
        "has_polyline": bool(mapping.polyline),
        "version":      mapping.version,
        "point_count":  len(mapping.raw_points) if mapping.raw_points else 0,
    })


@app.route("/driver/start-mapping", methods=["POST"])
def driver_start_mapping():
    """
    POST /driver/start-mapping
    Body: { "route": "22" }

    Starts a new mapping session for the given route.
    Resets raw_points and sets is_mapping_active=True.
    Requires is_mapping_allowed=True (set by admin).
    """
    data  = request.get_json(silent=True) or {}
    route = _normalize_route(data.get("route", ""))

    if not route:
        return jsonify({"error": "route is required"}), 400

    mapping = RouteMapping.query.filter_by(route=route).first()

    if not mapping:
        return jsonify({"error": "Mapping not enabled for this route"}), 403
    if not mapping.is_mapping_allowed:
        return jsonify({"error": "Mapping not permitted for this route"}), 403

    mapping.is_mapping_active = True
    mapping.raw_points        = []
    mapping.updated_at        = datetime.utcnow()
    db.session.commit()

    logger.info("[mapping] started for route=%r", route)
    return jsonify({"status": "ok", "route": route, "message": "Mapping started"})


@app.route("/driver/stop-mapping", methods=["POST"])
def driver_stop_mapping():
    """
    POST /driver/stop-mapping
    Body: { "route": "22" }

    Stops the active mapping session.
    Encodes raw_points → polyline, increments version, clears raw_points.
    Requires at least 2 recorded points to produce a valid polyline.
    """
    data  = request.get_json(silent=True) or {}
    route = _normalize_route(data.get("route", ""))

    if not route:
        return jsonify({"error": "route is required"}), 400

    mapping = RouteMapping.query.filter_by(route=route).first()

    if not mapping:
        return jsonify({"error": "No mapping record for this route"}), 404
    if not mapping.is_mapping_active:
        return jsonify({"error": "No active mapping session"}), 400

    raw = mapping.raw_points or []
    if len(raw) < 2:
        # Not enough points — reset without saving polyline
        mapping.is_mapping_active = False
        mapping.raw_points        = None
        mapping.updated_at        = datetime.utcnow()
        db.session.commit()
        logger.warning("[mapping] route=%r stopped with too few points (%d)", route, len(raw))
        return jsonify({
            "status":      "ok",
            "route":       route,
            "point_count": len(raw),
            "polyline":    None,
            "message":     "Too few points recorded — mapping discarded",
        })

    # Encode and store
    polyline_str          = _encode_polyline(raw)
    mapping.polyline      = polyline_str
    mapping.raw_points    = None
    mapping.is_mapping_active  = False
    mapping.is_mapping_allowed = False   # require admin to re-enable for next update
    mapping.version       += 1
    mapping.updated_at    = datetime.utcnow()
    db.session.commit()

    logger.info(
        "[mapping] route=%r completed — %d points → polyline len=%d, version=%d",
        route, len(raw), len(polyline_str), mapping.version
    )

    return jsonify({
        "status":      "ok",
        "route":       route,
        "point_count": len(raw),
        "polyline":    polyline_str,
        "version":     mapping.version,
        "message":     f"Route recorded — {len(raw)} points compressed to polyline",
    })


# ─────────────────────────────────────────────
# ROUTE MAPPING — PUBLIC READ ENDPOINT
# ─────────────────────────────────────────────

@app.route("/get-route-mapping")
def get_route_mapping():
    """
    GET /get-route-mapping?route=22

    Returns the recorded route path as a decoded coordinate array so the
    frontend needs no polyline decoder.

    Response (found):
      { "route":"22", "bus_route":"Karayanchavadi", "version":3,
        "coordinates":[{"lat":13.04,"lng":80.11}, ...] }

    Response (not found):
      { "route":"22", "bus_route":"", "version":0, "coordinates":[] }
    """
    route   = _normalize_route(request.args.get("route", ""))
    mapping = RouteMapping.query.filter_by(route=route).first() if route else None

    coordinates = []
    if mapping and mapping.polyline:
        try:
            raw = _decode_polyline(mapping.polyline)
            coordinates = [{"lat": p[0], "lng": p[1]} for p in raw]
        except Exception as e:
            logger.warning("[get-route-mapping] decode error route=%r: %s", route, e)

    return jsonify({
        "route":       route,
        "bus_route":   mapping.bus_route if mapping else "",
        "version":     mapping.version   if mapping else 0,
        "coordinates": coordinates,
    })


# ─────────────────────────────────────────────
# END TRIP
# ─────────────────────────────────────────────

@app.route("/end_trip", methods=["POST"])
def end_trip():
    data  = request.get_json(silent=True) or {}
    route = _normalize_route(data.get("route", ""))

    logger.info("[end_trip] route=%r", route)

    if not route:
        return jsonify({"error": "route is required"}), 400

    bus = BusLocation.query.filter_by(route=route).first()
    if bus:
        bus.timestamp = datetime.utcnow() - timedelta(seconds=INACTIVITY_THRESHOLD_SECONDS + 10)
        bus.active    = False
        bus.lat       = None
        bus.lng       = None
        db.session.commit()
        _cache_delete(_BUS_LOCATIONS_KEY)
        logger.info("[end_trip] route=%r — timestamp rewound, active=False, lat/lng cleared", route)
    else:
        logger.info("[end_trip] route=%r not found in DB — nothing to update", route)

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
        logger.info("[onboard] roll=%s route=%s onboard=%s", roll_no, bus_route, is_onboard)
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.error("[onboard] ERROR: %s", e)
        db.session.rollback()
        return jsonify({"error": "onboard update failed"}), 500
