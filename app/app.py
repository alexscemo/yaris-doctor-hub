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
HEX_RE = re.compile(r"^(?:[0-9A-F]{2}){1,64}$")
LID_RE = re.compile(r"^[0-9A-F]{2}$")
KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
PID_ID_RE = re.compile(r"^[0-9A-F]{2}\.\d{1,3}$")

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
CREATE TABLE IF NOT EXISTS frame(
    device TEXT NOT NULL, phone_rowid INTEGER NOT NULL, t INTEGER NOT NULL, lid TEXT NOT NULL, hex TEXT NOT NULL,
    PRIMARY KEY(device, phone_rowid));
CREATE INDEX IF NOT EXISTS frame_lid_t ON frame(lid, t);
CREATE TABLE IF NOT EXISTS alert(
    device TEXT NOT NULL, id INTEGER NOT NULL, key TEXT NOT NULL, level INTEGER NOT NULL, text TEXT NOT NULL,
    start_t INTEGER NOT NULL, end_t INTEGER,
    PRIMARY KEY(device, id));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dpf(
    device TEXT NOT NULL, regen_start INTEGER NOT NULL, base REAL, per_km REAL,
    PRIMARY KEY(device, regen_start));
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


def _str(v, limit, name):
    if not isinstance(v, str) or len(v) > limit:
        raise ValueError(f"{name} must be text of at most {limit} characters")
    return v


def _match(v, rx, name):
    if not isinstance(v, str) or not rx.match(v):
        raise ValueError(f"bad {name}: {v!r}")
    return v


def _defs(value):
    """The phone's PID table, which decodes frames (see Pid.kt pidDefs). Checked field by field: the dashboard shows it."""
    out = []
    for r in _rows(value, 13, "defs"):
        lid, bs, be, ln, signed, mul, div, off, dec, name, unit, cat, enum = r
        bs, be, ln, mul, div, off, dec = (_num(x, required=True) for x in (bs, be, ln, mul, div, off, dec))
        if not (0 <= bs <= be < ln * 8 <= 512 and div > 0 and 0 <= dec <= 6 and be - bs < 64):
            raise ValueError(f"bad pid def {r!r}")
        if not isinstance(signed, bool):
            raise ValueError("signed must be true or false")
        if enum is not None and (not isinstance(enum, list) or len(enum) > 16):
            raise ValueError("enum must be a list of at most 16 labels")
        out.append([_match(lid, LID_RE, "lid"), bs, be, ln, signed, mul, div, off, dec, _str(name, 80, "name"), _str(unit, 16, "unit"),
                    _str(cat, 40, "category"), None if enum is None else [_str(e, 40, "enum label") for e in enum]])
    return out


def _rules(value):
    """The phone's health rules (Checks.kt ruleDefs): [key, title, level, about, uses]; shown on the warning detail."""
    out = []
    for r in _rows(value, 5, "rules"):
        key, title, level, about, uses = r
        if level not in (1, 2) or isinstance(level, bool):
            raise ValueError("rule level must be 1 or 2")
        if not isinstance(uses, list) or len(uses) > 12:
            raise ValueError("rule uses must be a list of at most 12 value names")
        out.append([_match(key, KEY_RE, "rule key"), _str(title, 80, "rule title"), level, _str(about, 600, "rule description"),
                    [_str(u, 80, "value name") for u in uses]])
    return out


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
    frames = [
        (dev, _num(r[0], required=True), _num(r[1], required=True), _match(r[2], LID_RE, "lid"), _match(r[3], HEX_RE, "hex"))
        for r in _rows(body.get("frames", []), 4, "frames")
    ]
    alerts = [
        (dev, _num(r[0], required=True), _match(r[1], KEY_RE, "alert key"), _num(r[2], required=True), _str(r[3], 300, "alert text"),
         _num(r[4], required=True), _num(r[5]))
        for r in _rows(body.get("alerts", []), 6, "alerts")
    ]
    # DPF condition per regen, worked out on the phone (Db.dpfRows): [regen start, restriction after it, rise per km]
    dpf = None if body.get("dpf") is None else [
        (dev, _num(r[0], required=True), _num(r[1], float), _num(r[2], float)) for r in _rows(body["dpf"], 3, "dpf")
    ]
    defs = _defs(body["defs"]) if body.get("defs") is not None else None
    rules = _rules(body["rules"]) if body.get("rules") is not None else None
    with write_lock, db:
        db.executemany("INSERT OR IGNORE INTO frame VALUES (?,?,?,?,?)", frames)
        # Alerts gain an end time and new text after upload, so the latest copy wins, like regens
        db.executemany("INSERT OR REPLACE INTO alert VALUES (?,?,?,?,?,?,?)", alerts)
        if defs is not None:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('defs', ?)", (json.dumps(defs),))
        if rules is not None:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('rules', ?)", (json.dumps(rules),))
        # Re-sent rows are ignored, so a phone that lost our reply can simply upload again
        db.executemany("INSERT OR IGNORE INTO sample VALUES (?,?,?,?,?,?,?)", samples)
        # Regens change after upload (end_t is set when they finish), so the phone resends them and the latest wins
        db.executemany("INSERT OR REPLACE INTO regen VALUES (?,?,?,?,?,?,?,?)", regens)
        # regens_all: the phone sent every regen it has, so ones it deleted (engine-stop blips, not burns) go here too
        if body.get("regens_all") is True:
            ids = [r[1] for r in regens]
            db.execute(f"DELETE FROM regen WHERE device = ? AND id NOT IN ({','.join('?' * len(ids))})", (dev, *ids))
        if dpf is not None:
            db.execute("DELETE FROM dpf WHERE device = ?", (dev,))
            db.executemany("INSERT OR REPLACE INTO dpf VALUES (?,?,?,?)", dpf)
        db.execute(
            "INSERT INTO device VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, last_sync = excluded.last_sync",
            (dev, name, now_ms),
        )
    # "frames" in the reply tells the phone this hub stores them (an older hub would silently drop them)
    return {"ok": True, "samples": len(samples), "regens": len(regens), "frames": len(frames), "alerts": len(alerts)}


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
        # Open ones only, like the phone's Overview: a recovered warning stays in the warning history below
        "alerts": alerts(db, now_ms),
        "dpf": [
            {"start": s, "base": b, "per_km": k, "driver": names.get(d, "?")}
            for d, s, b, k in db.execute("SELECT device, regen_start, base, per_km FROM dpf WHERE base IS NOT NULL ORDER BY regen_start")
        ],
    }


# ---------- live data: frames decoded with the phone's PID table (same maths as Pid.kt decode) ----------

def decode(d, hexs):
    """Value of def `d` in payload `hexs` (hex after "61 <lid>"), or None when the payload is too short."""
    _, bs, be, ln, signed, mul, div, off, dec = d[:9]
    if len(hexs) < ln * 2:
        return None
    raw = int(hexs[bs // 8 * 2:(be // 8 + 1) * 2], 16)
    bits = be - bs + 1
    raw = raw >> (7 - be % 8) & ((1 << bits) - 1)
    if signed and raw >> (bits - 1) & 1:
        raw -= 1 << bits
    q = abs(raw * mul) // div  # truncating toward zero, like Techstream and Kotlin's Long division
    return ((q if raw * mul >= 0 else -q) + off) / 10 ** dec


def defs_of(db):
    row = db.execute("SELECT v FROM meta WHERE k = 'defs'").fetchone()
    return json.loads(row[0]) if row else []


def live(db, now_ms):
    """Latest value of every PID (from any phone) and when it was read."""
    out, latest = [], {}
    for d in defs_of(db):
        lid = d[0]
        if lid not in latest:
            latest[lid] = db.execute("SELECT t, hex FROM frame WHERE lid = ? ORDER BY t DESC LIMIT 1", (lid,)).fetchone()
        row = latest[lid]
        out.append({
            "id": f"{lid}.{d[1]}", "name": d[9], "unit": d[10], "category": d[11], "enum": d[12], "decimals": d[8],
            "t": row[0] if row else None, "value": decode(d, row[1]) if row else None,
        })
    return {"now": now_ms, "values": out}


def series(db, pid_id, span_ms, window=None):
    """[[t, value], ...] of one PID oldest first: over the last `span_ms` it was read, or within `window` = (from, to)."""
    d = next((d for d in defs_of(db) if f"{d[0]}.{d[1]}" == pid_id), None)
    if d is None:
        return []
    if window:
        rows = db.execute("SELECT t, hex FROM frame WHERE lid = ? AND t BETWEEN ? AND ? ORDER BY t", (d[0], *window)).fetchall()
    else:
        rows = db.execute(
            "SELECT t, hex FROM frame WHERE lid = ?1 AND t >= (SELECT MAX(t) FROM frame WHERE lid = ?1) - ?2 ORDER BY t", (d[0], span_ms)
        ).fetchall()
    return [[t, v] for t, h in rows if (v := decode(d, h)) is not None]


def warnings(db, limit=500):
    """Every stored warning, newest first, plus what each rule checks and the values it reads (as PID ids for /api/series)."""
    ids = {}
    for d in defs_of(db):
        ids.setdefault(d[9], {"id": f"{d[0]}.{d[1]}", "name": d[9], "unit": d[10], "enum": d[12], "decimals": d[8]})
    row = db.execute("SELECT v FROM meta WHERE k = 'rules'").fetchone()
    rules = {
        key: {"title": title, "level": level, "about": about, "values": [ids[u] for u in uses if u in ids]}
        for key, title, level, about, uses in (json.loads(row[0]) if row else [])
    }
    history = [
        {"key": k, "level": lv, "text": tx, "start": st, "end": en, "driver": dr}
        for k, lv, tx, st, en, dr in db.execute(
            "SELECT a.key, a.level, a.text, a.start_t, a.end_t, d.name FROM alert a JOIN device d ON d.id = a.device "
            "ORDER BY a.start_t DESC LIMIT ?", (limit,))
    ]
    return {"rules": rules, "history": history}


def alerts(db, since_ms):
    """Open alerts and those that ended after `since_ms`, one per key across phones: open first, then the newest."""
    rows = db.execute(
        "SELECT a.key, a.level, a.text, a.start_t, a.end_t, d.name FROM alert a JOIN device d ON d.id = a.device "
        "WHERE a.end_t IS NULL OR a.end_t >= ? ORDER BY a.end_t IS NOT NULL, a.level DESC, COALESCE(a.end_t, a.start_t) DESC",
        (since_ms,),
    ).fetchall()
    seen, out = set(), []
    for key, level, text, start, end, driver in rows:
        if key not in seen:
            seen.add(key)
            out.append({"key": key, "level": level, "text": text, "start": start, "end": end, "driver": driver})
    return out


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
        elif self.path == "/api/live":
            db = connect()
            try:
                self._send(200, live(db, int(time.time() * 1000)))
            finally:
                db.close()
        elif self.path.startswith("/api/series?"):
            q = parse_qs(urlsplit(self.path).query)
            pid_id = q.get("id", [""])[0]
            span = q.get("span", ["1800000"])[0]
            t0, t1 = q.get("from", [""])[0], q.get("to", [""])[0]
            if not PID_ID_RE.match(pid_id) or not span.isdigit() or (t0 or t1) and not (t0.isdigit() and t1.isdigit()):
                return self._send(400, {"error": "need id=<lid>.<bit> and span=<ms> or from=<ms>&to=<ms>"})
            # A window of at most a day: a warning's own span plus a margin, never the whole history
            window = (int(t0), min(int(t1), int(t0) + 86_400_000)) if t0 else None
            db = connect()
            try:
                self._send(200, {"points": series(db, pid_id, min(int(span), 7 * 86_400_000), window)})
            finally:
                db.close()
        elif self.path == "/api/warnings":
            db = connect()
            try:
                self._send(200, warnings(db))
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
        if not self.path.startswith(("/api/summary", "/api/export", "/api/live", "/api/series", "/api/warnings")):  # polled; keep the log readable
            super().log_message(fmt, *args)


if __name__ == "__main__":
    connect().close()
    print(f"Yaris hub on :{PORT}, database {DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
