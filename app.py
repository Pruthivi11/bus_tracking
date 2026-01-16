from flask import Flask, render_template, request, redirect, session, jsonify
from flask_cors import CORS

app = Flask(__name__)
app.secret_key = "driver_secret"
CORS(app)

otp_store = {}
bus_locations = []  # simple in-memory store (MVP)

# ---------------- HOME ----------------
@app.route("/")
def home():
    return render_template("home.html")

# ---------------- DRIVER FLOW ----------------
@app.route("/driver_login", methods=["GET", "POST"])
def driver_login():
    if "driver_phone" in session:
        return redirect("/driver")
    if request.method == "POST":
        phone = request.form["phone"]
        otp = request.form["otp"]
        if otp_store.get(phone) == otp:
            session["driver_phone"] = phone
            return redirect("/driver")
    return render_template("login.html")

@app.route("/send_otp", methods=["POST"])
def send_otp():
    phone = request.json["phone"]
    otp_store[phone] = "1234"  # DEMO OTP
    return jsonify({"msg": "OTP sent (1234 for demo)"})

@app.route("/driver")
def driver():
    if "driver_phone" not in session:
        return redirect("/driver_login")
    return render_template("driver.html")

@app.route("/location", methods=["POST"])
def location():
    data = request.json
    # Expected: {route, busType, lat, lng, time}
    if all(k in data for k in ("route", "busType", "lat", "lng", "time")):
        bus_locations.append(data)
    print("Live Data:", data)
    return jsonify({"status": "ok"})

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------------- STUDENT FLOW (APK-like UX) ----------------
@app.route("/student")
def student():
    return render_template("student.html")

@app.route("/get_locations")
def get_locations():
    # Return recent bus updates (MVP). In production, filter by bus/route.
    return jsonify(bus_locations[-20:])

# ---------------- ADMIN FLOW ----------------
@app.route("/admin")
def admin():
    return render_template("admin.html")

if __name__ == "__main__":
    app.run(debug=True)