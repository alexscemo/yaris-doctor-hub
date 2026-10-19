"""Yaris Doctor home hub.

Phones on the home Wi-Fi POST their log to /api/sync; this keeps the merged SQLite database (the single source
of truth for every phone that drives the car) and serves the dashboard. LAN only, no accounts: a phone is just a
random install id plus a driver name. Standard library only, so it runs on a stock python image.
"""
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DB_PATH = os.environ.get("DB_PATH", "hub.db")
PORT = int(os.environ.get("PORT", "8765"))
HERE = Path(__file__).parent
MAX_BODY = 8 * 1024 * 1024
MAX_ROWS = 10_000
DEVICE_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MERGE_SLACK_MS = 2 * 60_000  # regens seen by two phones (dongle hand-over) closer than this are one regen
LIVE_MS = 60_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS device(id TEXT PRIMARY KEY, name TEXT NOT NULL, last_sync INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sample(
    device TEXT NOT NULL, phone_rowid INTEGER NOT NULL, t INTEGER NOT NULL,
    soot INTEGER, temp INTEGER, regen INTEGER NOT NULL, odo_m REAL,
    PRIMARY KEY(device, phone_rowid));
CREATE INDEX IF NOT EXISTS sample_t ON sample(t);
CREATE INDEX IF NOT EXISTS sample_device_t ON sample(device, t);
CREATE TABLE IF NOT EXISTS regen(
    device TEXT NOT NULL, id INTEGER NOT NULL, start_t INTEGER NOT NULL, end_t INTEGER,
    odo_start REAL, odo_end REAL, soot_start INTEGER, soot_end INTEGER,
    PRIMARY KEY(device, id));
"""

write_lock = threading.Lock()  # SQLite takes one writer; uploads are rare, so serialise them


def connect(path=None):
    db = sqlite3.connect(path or DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


# ---------- ingest (trust boundary: everything from the phone is checked) ----------

def _num(v, kind=int, required=False):
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"expected a number, got {v!r}")
    return kind(v)


def _rows(value, width, name):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise ValueError(f"{name} must be a list of at most {MAX_ROWS} rows")
    for r in value:
        if not isinstance(r, list) or len(r) != width:
            raise ValueError(f"each {name} row must have {width} values")
    return value


def ingest(db, body, now_ms):
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    dev = body.get("device_id")
    if not isinstance(dev, str) or not DEVICE_RE.match(dev):
        raise ValueError("bad device_id")
    name = body.get("name")
    name = name.strip()[:40] if isinstance(name, str) and name.strip() else "Unnamed phone"

    samples = [
        (dev, _num(r[0], required=True), _num(r[1], required=True), _num(r[2]), _num(r[3]), 1 if r[4] else 0, _num(r[5], float))
        for r in _rows(body.get("samples", []), 6, "samples")
    ]
    regens = [
        (dev, _num(r[0], required=True), _num(r[1], required=True), _num(r[2]), _num(r[3], float), _num(r[4], float), _num(r[5]), _num(r[6]))
        for r in _rows(body.get("regens", []), 7, "regens")
    ]
    with write_lock, db:
        # Re-sent rows are ignored, so a phone that lost our reply can simply upload again
        db.executemany("INSERT OR IGNORE INTO sample VALUES (?,?,?,?,?,?,?)", samples)
        # Regens change after upload (end_t is set when they finish), so the phone resends them and the latest wins
        db.executemany("INSERT OR REPLACE INTO regen VALUES (?,?,?,?,?,?,?,?)", regens)
        db.execute(
            "INSERT INTO device VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, last_sync = excluded.last_sync",
            (dev, name, now_ms),
        )
    return {"ok": True, "samples": len(samples), "regens": len(regens)}


# ---------- summary for the dashboard ----------

def merge_regens(rows):
    """rows: (device, start, end, soot_start, soot_end) sorted by start. Overlapping regens from different
    phones are the same burn seen twice; merge them. end None = still running."""
    merged = []
    for dev, start, end, s0, s1 in rows:
        last = merged[-1] if merged else None
        last_end = None if last is None else (last["end"] if last["end"] is not None else float("inf"))
        if last is not None and start <= last_end + MERGE_SLACK_MS:
            if end is None or last["end"] is None:
                last["end"] = None
            else:
                if end > last["end"]:
                    last["end"], last["soot_end"] = end, s1
            last["devices"].add(dev)
        else:
            merged.append({"start": start, "end": end, "soot_start": s0, "soot_end": s1, "devices": {dev}})
    return merged


def close_if_stale(db, row, now_ms):
    """A phone killed mid-regen never writes end_t. Once that phone has gone quiet, end the regen at its last
    regen reading so it doesn't swallow every later regen in merge_regens."""
    dev, start, end, s0, s1 = row
    if end is not None:
        return row
    last = db.execute("SELECT MAX(t) FROM sample WHERE device = ? AND t >= ?", (dev, start)).fetchone()[0]
    if last is not None and now_ms - last <= LIVE_MS:
        return row  # still running
    last_regen = db.execute(
        "SELECT MAX(t) FROM sample WHERE device = ? AND t >= ? AND regen = 1", (dev, start)
    ).fetchone()[0]
    return dev, start, last_regen or start, s0, s1


def distance_m(db, t0, t1):
    """Metres the car drove between two instants, summed over every phone's own GPS odometer.
    Each phone's odo_m only grows, so its progress is (last reading by t1) - (last reading by t0)."""
    total = 0.0
    for (dev,) in db.execute("SELECT id FROM device"):
        base = db.execute("SELECT MIN(odo_m) FROM sample WHERE device = ?", (dev,)).fetchone()[0]
        if base is None:
            continue
        at = lambda t: db.execute(
            "SELECT odo_m FROM sample WHERE device = ? AND t <= ? ORDER BY t DESC LIMIT 1", (dev, t)
        ).fetchone()
        a, b = at(t0), at(t1)
        total += max(0.0, (b[0] if b else base) - (a[0] if a else base))
    return total


def summary(db, now_ms):
    names = dict(db.execute("SELECT id, name FROM device"))
    rows = [close_if_stale(db, r, now_ms) for r in
            db.execute("SELECT device, start_t, end_t, soot_start, soot_end FROM regen ORDER BY start_t")]
    regens = merge_regens(rows)
    prev_end = None
    for r in regens:
        r["gap_m"] = distance_m(db, prev_end, r["start"]) if prev_end is not None else None
        r["drivers"] = sorted(names.get(d, "?") for d in r.pop("devices"))
        if r["end"] is not None:
            prev_end = r["end"]

    last = db.execute(
        "SELECT s.t, s.soot, s.temp, s.regen, d.name FROM sample s JOIN device d ON d.id = s.device ORDER BY s.t DESC LIMIT 1"
    ).fetchone()
    gaps = [r["gap_m"] for r in regens if r["gap_m"] is not None]
    soot = db.execute(
        "SELECT (t / 1800000) * 1800000 AS b, ROUND(AVG(soot), 1) FROM sample "
        "WHERE t >= ? AND soot IS NOT NULL GROUP BY b ORDER BY b",
        (now_ms - 30 * 86_400_000,),
    ).fetchall()
    devices = [
        {"name": n, "last_sync": ls, "samples": c, "distance_m": (hi or 0) - (lo or 0), "first_t": ft, "last_t": lt}
        for n, ls, c, lo, hi, ft, lt in db.execute(
            "SELECT d.name, d.last_sync, COUNT(s.t), MIN(s.odo_m), MAX(s.odo_m), MIN(s.t), MAX(s.t) "
            "FROM device d LEFT JOIN sample s ON s.device = d.id GROUP BY d.id ORDER BY d.last_sync DESC"
        )
    ]
    return {
        "now": now_ms,
        "latest": None if last is None else {
            "t": last[0], "soot": last[1], "temp": last[2], "regen": bool(last[3]), "driver": last[4],
            "live": now_ms - last[0] <= LIVE_MS,
        },
        "since_regen_m": distance_m(db, prev_end, now_ms) if prev_end is not None else None,
        "avg_gap_m": sum(gaps) / len(gaps) if gaps else None,
        "regens": regens[::-1],
        "soot_series": soot,
        "devices": devices,
    }


# ---------- export: what the other phones logged, so each phone can show the whole car ----------

EXPORT_PAGE = 5000


def export(db, device, after, now_ms):
    """Samples of every phone except `device` with hub rowid > `after` (one page), plus all their regens with
    stale open ones closed. The phone keeps `next` as its cursor and asks again while `more` is true."""
    samples = db.execute(
        "SELECT rowid, device, phone_rowid, t, soot, temp, regen, odo_m FROM sample "
        "WHERE rowid > ? AND device != ? ORDER BY rowid LIMIT ?",
        (after, device, EXPORT_PAGE),
    ).fetchall()
    regens = []
    for dev, rid, start, end, s0, s1 in db.execute(
        "SELECT device, id, start_t, end_t, soot_start, soot_end FROM regen WHERE device != ?", (device,)
    ):
        end = close_if_stale(db, (dev, start, end, s0, s1), now_ms)[2]
        regens.append([dev, rid, start, end, s0, s1])
    return {
        "samples": [list(r[1:]) for r in samples],
        "regens": regens,
        "next": samples[-1][0] if samples else after,
        "more": len(samples) == EXPORT_PAGE,
    }


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "YarisHub/1"

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/summary":
            db = connect()
            try:
                self._send(200, summary(db, int(time.time() * 1000)))
            finally:
                db.close()
        elif self.path.startswith("/api/export?"):
            q = parse_qs(urlsplit(self.path).query)
            device = q.get("device", [""])[0]
            after = q.get("after", ["0"])[0]
            if not DEVICE_RE.match(device) or not after.isdigit():
                return self._send(400, {"error": "need device=<id>&after=<rowid>"})
            db = connect()
            try:
                self._send(200, export(db, device, int(after), int(time.time() * 1000)))
            finally:
                db.close()
        elif self.path == "/icon.svg":  # app icon, e.g. for a ZimaOS / CasaOS dashboard tile
            self._send(200, (HERE / "icon.svg").read_bytes(), "image/svg+xml")
        elif self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/sync":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": "body missing or too large"})
        try:
            body = json.loads(self.rfile.read(length))
            db = connect()
            try:
                result = ingest(db, body, int(time.time() * 1000))
            finally:
                db.close()
        except (ValueError, TypeError) as e:  # JSONDecodeError is a ValueError
            return self._send(400, {"error": str(e)})
        self._send(200, result)

    def log_message(self, fmt, *args):
        if not self.path.startswith(("/api/summary", "/api/export")):  # polled; keep the log readable
            super().log_message(fmt, *args)


if __name__ == "__main__":
    connect().close()
    print(f"Yaris hub on :{PORT}, database {DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
