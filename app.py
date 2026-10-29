from flask import Flask, render_template, request, jsonify
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import closing
import heapq
import os

app = Flask(__name__)
DB_PATH = Path(os.environ.get("VOLTQUEUE_DB", Path(__file__).with_name("voltqueue.db")))

CHARGER_SEED = [
    ("C01", "AC", 7.4), ("C02", "AC", 7.4), ("C03", "AC", 7.4),
    ("C04", "AC", 7.4), ("C05", "DC", 22.0), ("C06", "DC", 22.0),
]

def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con

def now():
    return datetime.now().replace(microsecond=0)

def parse_dt(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS chargers (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, power REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'Available'
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle TEXT NOT NULL, owner TEXT NOT NULL, vehicle_type TEXT NOT NULL,
            battery INTEGER NOT NULL, priority TEXT NOT NULL,
            mode TEXT NOT NULL, charger_id TEXT,
            start_at TEXT NOT NULL, end_at TEXT NOT NULL,
            duration INTEGER NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(charger_id) REFERENCES chargers(id)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        for charger in CHARGER_SEED:
            con.execute("INSERT OR IGNORE INTO chargers(id,kind,power,status) VALUES(?,?,?,'Available')", charger)
        con.commit()

def event(con, message):
    con.execute("INSERT INTO events(message,created_at) VALUES(?,?)", (message, now().isoformat(sep=" ")))

def active_statuses():
    return ("Charging", "Reserved", "Waiting")

def has_overlap(con, charger_id, start, end, ignore_id=None):
    sql = """SELECT id FROM bookings
             WHERE charger_id=? AND status IN ('Reserved','Charging')
             AND start_at < ? AND end_at > ?"""
    params = [charger_id, end.isoformat(sep=" "), start.isoformat(sep=" ")]
    if ignore_id is not None:
        sql += " AND id != ?"
        params.append(ignore_id)
    return con.execute(sql, params).fetchone() is not None

def allocate_waiting(con):
    """Use a priority heap: Emergency first, then lowest battery, then oldest request."""
    rows = con.execute("""SELECT * FROM bookings WHERE status='Waiting'
                          ORDER BY created_at ASC, id ASC""").fetchall()
    heap = []
    priority_rank = {"Emergency": 0, "High": 1, "Normal": 2}
    for row in rows:
        heapq.heappush(heap, (priority_rank.get(row["priority"], 2), row["battery"], row["created_at"], row["id"], row))
    while heap:
        _, _, _, _, row = heapq.heappop(heap)
        start = now()
        end = start + timedelta(minutes=row["duration"])
        chargers = con.execute("SELECT * FROM chargers WHERE status!='Offline' ORDER BY power DESC, id").fetchall()
        chosen = None
        for charger in chargers:
            if not has_overlap(con, charger["id"], start, end):
                chosen = charger
                break
        if chosen:
            con.execute("""UPDATE bookings SET status='Charging', charger_id=?, mode='Queue',
                           start_at=?, end_at=? WHERE id=?""",
                        (chosen["id"], start.isoformat(sep=" "), end.isoformat(sep=" "), row["id"]))
            event(con, f"{row['vehicle']} allocated to {chosen['id']} ({row['priority']} priority).")

def refresh_sessions(con):
    stamp = now()
    rows = con.execute("SELECT * FROM bookings WHERE status='Charging'").fetchall()
    for row in rows:
        end = parse_dt(row["end_at"])
        if end and stamp >= end:
            con.execute("UPDATE bookings SET status='Completed' WHERE id=?", (row["id"],))
            event(con, f"Charging completed for {row['vehicle']} on {row['charger_id']}.")
    allocate_waiting(con)

def booking_dict(row):
    item = dict(row)
    start, end = parse_dt(item["start_at"]), parse_dt(item["end_at"])
    if item["status"] == "Charging" and start and end:
        total = max((end-start).total_seconds(), 1)
        elapsed = max(0, min((now()-start).total_seconds(), total))
        item["progress"] = round(elapsed / total * 100)
        item["remaining_minutes"] = max(0, int((end-now()).total_seconds() / 60 + 0.99))
    else:
        item["progress"] = 0
        item["remaining_minutes"] = None
    return item

@app.route("/")
def home():
    return render_template("index.html")

@app.get("/api/state")
def state():
    with closing(db()) as con:
        refresh_sessions(con)
        con.commit()
        chargers = [dict(r) for r in con.execute("SELECT * FROM chargers ORDER BY id").fetchall()]
        bookings = [booking_dict(r) for r in con.execute(
            "SELECT * FROM bookings ORDER BY id DESC LIMIT 40").fetchall()]
        events = [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12").fetchall()]
        for charger in chargers:
            active = con.execute("SELECT vehicle FROM bookings WHERE charger_id=? AND status='Charging' LIMIT 1",
                                 (charger["id"],)).fetchone()
            charger["live_status"] = "Offline" if charger["status"] == "Offline" else ("Charging" if active else "Available")
            charger["vehicle"] = active["vehicle"] if active else None
        stats = {
            "total": len(chargers),
            "available": sum(c["live_status"] == "Available" for c in chargers),
            "charging": sum(c["live_status"] == "Charging" for c in chargers),
            "waiting": con.execute("SELECT COUNT(*) FROM bookings WHERE status='Waiting'").fetchone()[0],
            "completed": con.execute("SELECT COUNT(*) FROM bookings WHERE status='Completed'").fetchone()[0],
        }
        return jsonify({"chargers": chargers, "bookings": bookings, "events": events, "stats": stats})

@app.get("/api/bookings")
def get_bookings():
    with closing(db()) as con:
        refresh_sessions(con)
        con.commit()
        rows = con.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT 100").fetchall()
        return jsonify([booking_dict(r) for r in rows])

@app.post("/api/bookings")
def create_booking():
    data = request.get_json(silent=True) or {}
    vehicle = str(data.get("vehicle", "")).strip().upper()
    owner = str(data.get("owner", "")).strip()
    vehicle_type = str(data.get("vehicle_type", "Car")).strip()
    priority = str(data.get("priority", "Normal"))
    mode = str(data.get("mode", "Queue"))
    battery = data.get("battery", 30)
    duration = data.get("duration", 30)
    charger_id = str(data.get("charger_id", "")).strip() or None
    if not vehicle or not owner:
        return jsonify(error="Enter both vehicle number and owner name."), 400
    if priority not in ("Emergency", "High", "Normal") or mode not in ("Queue", "Reservation"):
        return jsonify(error="Invalid priority or booking mode."), 400
    try:
        battery, duration = int(battery), int(duration)
    except (TypeError, ValueError):
        return jsonify(error="Battery and duration must be numbers."), 400
    if not 0 <= battery <= 100 or not 10 <= duration <= 240:
        return jsonify(error="Battery must be 0–100%; duration must be 10–240 minutes."), 400

    start = parse_dt(data.get("start_at")) if mode == "Reservation" else now()
    if not start:
        return jsonify(error="Choose a valid reservation date and time."), 400
    if mode == "Reservation" and not (now() + timedelta(minutes=5) <= start <= now() + timedelta(days=30)):
        return jsonify(error="Reservations must be at least 5 minutes ahead and within 30 days."), 400
    end = start + timedelta(minutes=duration)

    with closing(db()) as con:
        duplicate = con.execute("""SELECT id FROM bookings WHERE vehicle=? AND status IN ('Waiting','Charging','Reserved')""",
                                 (vehicle,)).fetchone()
        if duplicate:
            return jsonify(error="This vehicle already has an active request."), 409
        if mode == "Reservation":
            if not charger_id:
                return jsonify(error="Select a charger for an advance reservation."), 400
            charger = con.execute("SELECT * FROM chargers WHERE id=?", (charger_id,)).fetchone()
            if not charger or charger["status"] == "Offline":
                return jsonify(error="That charger is unavailable. Choose another charger."), 409
            if has_overlap(con, charger_id, start, end):
                return jsonify(error="That charger is already reserved or occupied during this time."), 409
            con.execute("""INSERT INTO bookings(vehicle,owner,vehicle_type,battery,priority,mode,charger_id,start_at,end_at,duration,status,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (vehicle, owner, vehicle_type, battery, priority, mode, charger_id,
                         start.isoformat(sep=" "), end.isoformat(sep=" "), duration, "Reserved", now().isoformat(sep=" ")))
            event(con, f"Advance reservation created for {vehicle} on {charger_id}.")
        else:
            con.execute("""INSERT INTO bookings(vehicle,owner,vehicle_type,battery,priority,mode,charger_id,start_at,end_at,duration,status,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (vehicle, owner, vehicle_type, battery, priority, "Queue", None,
                         start.isoformat(sep=" "), end.isoformat(sep=" "), duration, "Waiting", now().isoformat(sep=" ")))
            event(con, f"Queue request received for {vehicle} ({priority} priority).")
            allocate_waiting(con)
        con.commit()
        row = con.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT 1").fetchone()
        return jsonify(message="Booking request saved.", booking=booking_dict(row)), 201

@app.post("/api/bookings/<int:booking_id>/cancel")
def cancel_booking(booking_id):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row:
            return jsonify(error="Booking not found."), 404
        if row["status"] not in ("Waiting", "Reserved", "Charging"):
            return jsonify(error="Only waiting, reserved, or active bookings can be cancelled."), 409
        con.execute("UPDATE bookings SET status='Cancelled' WHERE id=?", (booking_id,))
        event(con, f"Booking #{booking_id} for {row['vehicle']} was cancelled.")
        allocate_waiting(con)
        con.commit()
        return jsonify(message="Booking cancelled.")

@app.post("/api/bookings/<int:booking_id>/complete")
def complete_booking(booking_id):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row or row["status"] != "Charging":
            return jsonify(error="No active charging session found."), 404
        con.execute("UPDATE bookings SET status='Completed' WHERE id=?", (booking_id,))
        event(con, f"Session for {row['vehicle']} completed by station operator.")
        allocate_waiting(con)
        con.commit()
        return jsonify(message="Session marked complete.")

@app.post("/api/chargers/<charger_id>/status")
def set_charger_status(charger_id):
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ("Available", "Offline"):
        return jsonify(error="Invalid charger status."), 400
    with closing(db()) as con:
        charger = con.execute("SELECT * FROM chargers WHERE id=?", (charger_id,)).fetchone()
        if not charger:
            return jsonify(error="Charger not found."), 404
        if status == "Offline":
            active = con.execute("SELECT * FROM bookings WHERE charger_id=? AND status='Charging'", (charger_id,)).fetchall()
            for row in active:
                con.execute("UPDATE bookings SET status='Waiting', charger_id=NULL, mode='Queue' WHERE id=?", (row["id"],))
                event(con, f"{charger_id} went offline; {row['vehicle']} returned to the queue.")
        con.execute("UPDATE chargers SET status=? WHERE id=?", (status, charger_id))
        event(con, f"Charger {charger_id} marked {status}.")
        allocate_waiting(con)
        con.commit()
        return jsonify(message=f"{charger_id} is now {status}.")

@app.get("/api/search")
def search_vehicle():
    vehicle = str(request.args.get("vehicle", "")).strip().upper()
    if not vehicle:
        return jsonify(error="Enter a vehicle number."), 400
    with closing(db()) as con:
        refresh_sessions(con)
        con.commit()
        rows = con.execute("SELECT * FROM bookings WHERE vehicle=? ORDER BY id DESC LIMIT 10", (vehicle,)).fetchall()
        return jsonify([booking_dict(r) for r in rows])

@app.get("/api/health")
def health():
    return jsonify(status="ok", telemetry="SIMULATED", message="No physical charger connection is configured.")

if __name__ == "__main__":
    init_db()
    app.run(debug=True, use_reloader=False)
else:
    init_db()
