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
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import groupby
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from labels import LABELS, STATES

DB_PATH = os.environ.get("DB_PATH", "hub.db")
PORT = int(os.environ.get("PORT", "8765"))
HERE = Path(__file__).parent
MAX_BODY = 8 * 1024 * 1024
MAX_ROWS = 10_000
DEVICE_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MERGE_SLACK_MS = 2 * 60_000  # regens seen by two phones (dongle hand-over) closer than this are one regen
LIVE_MS = 60_000
TRIP_GAP_MS = 2 * 60_000  # the phone's TRIP_GAP_MS: readings further apart than this are two trips
SERIES_POINTS = 1500  # more than a chart has pixels; a narrower range comes back in finer detail
STATS_ROWS = 5000
HEX_RE = re.compile(r"^(?:[0-9A-F]{2}){1,64}$")
LID_RE = re.compile(r"^[0-9A-F]{2}$")
KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
PID_ID_RE = re.compile(r"^[0-9A-F]{2}\.\d{1,3}$")
# The app's fonts (OFL, licences beside them); a fixed list, so no path from the request reaches the filesystem
FONTS = {"/fonts/hanken_grotesk.ttf": "font/ttf", "/fonts/pt_mono.ttf": "font/ttf",
         "/fonts/HankenGrotesk-OFL.txt": "text/plain; charset=utf-8", "/fonts/PTMono-OFL.txt": "text/plain; charset=utf-8"}

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
CREATE TABLE IF NOT EXISTS deleted(device TEXT NOT NULL, from_t INTEGER NOT NULL, to_t INTEGER NOT NULL, PRIMARY KEY(device, from_t, to_t));
"""

def _add_columns(table, *columns):
    """A step adding `columns` ("name TYPE") to `table`, skipping any it already has: ALTER TABLE ADD COLUMN can't be re-run."""
    def step(db):
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for col in columns:
            if col.split()[0] not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    return step


# Schema changes, oldest first; PRAGMA user_version counts the steps a database has run. SCHEMA above is the baseline
# and stays as it is: change the schema only by appending a step here (never edit or remove one), or existing hubs miss it.
# A step is a list of SQL statements or functions taking the database.
MIGRATIONS = [
    # 1: distance_m's MIN(odo_m) per phone, which otherwise reads all of that phone's samples once per regen
    ["CREATE INDEX IF NOT EXISTS sample_device_odo ON sample(device, odo_m)"],
    # 2: the car's own odometer (app 1.2.0): frames and warnings carry it, and each phone says since when its odo_m is the
    # car's (odo_real_from; null = a phone still on GPS distance)
    [
        _add_columns("frame", "odo_m REAL"),
        _add_columns("alert", "odo_start REAL", "odo_end REAL"),
        _add_columns("device", "odo_real_from INTEGER"),
    ],
]

write_lock = threading.Lock()  # SQLite takes one writer; uploads are rare, so serialise them


def connect(path=None):
    db = sqlite3.connect(path or DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    if db.execute("PRAGMA user_version").fetchone()[0] < len(MIGRATIONS):
        with write_lock:
            # Re-read under the lock: another request may have just run the steps
            for version in range(db.execute("PRAGMA user_version").fetchone()[0], len(MIGRATIONS)):
                db.execute("BEGIN IMMEDIATE")  # a step and its version number commit together, or neither does
                try:
                    for sql in MIGRATIONS[version]:
                        sql(db) if callable(sql) else db.execute(sql)
                    db.execute(f"PRAGMA user_version = {version + 1}")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
    return db


# ---------- ingest (trust boundary: everything from the phone is checked) ----------

def _num(v, kind=int, required=False):
    if v is None and not required:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"expected a number, got {v!r}")
    return kind(v)


def _rows(value, width, name):
    """`width` is the row length, or a tuple of the lengths accepted (an older phone sends the shorter row)."""
    widths = width if isinstance(width, tuple) else (width,)
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise ValueError(f"{name} must be a list of at most {MAX_ROWS} rows")
    for r in value:
        if not isinstance(r, list) or len(r) not in widths:
            raise ValueError(f"each {name} row must have {' or '.join(map(str, widths))} values")
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


def _sample_top(db):
    """Highest sample rowid ever used: the current maximum, or the one saved before deletes took the newest rows."""
    saved = db.execute("SELECT v FROM meta WHERE k = 'sample_top'").fetchone()
    return max(db.execute("SELECT COALESCE(MAX(rowid), 0) FROM sample").fetchone()[0], int(saved[0]) if saved else 0)


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
    # [rowid, t, lid, hex] and, from app 1.2.0, the car's odometer then (metres)
    frames = [
        (dev, _num(r[0], required=True), _num(r[1], required=True), _match(r[2], LID_RE, "lid"), _match(r[3], HEX_RE, "hex"),
         _num(r[4], float) if len(r) > 4 else None)
        for r in _rows(body.get("frames", []), (4, 5), "frames")
    ]
    # [id, key, level, text, start, end] and, from app 1.2.0, the odometer when it opened and closed
    alerts = [
        (dev, _num(r[0], required=True), _match(r[1], KEY_RE, "alert key"), _num(r[2], required=True), _str(r[3], 300, "alert text"),
         _num(r[4], required=True), _num(r[5]), _num(r[6], float) if len(r) > 6 else None, _num(r[7], float) if len(r) > 6 else None)
        for r in _rows(body.get("alerts", []), (6, 8), "alerts")
    ]
    # From when this phone's odo_m is the car's odometer (the cluster's); before it, its estimate. Absent: GPS distance
    odo_real_from = _num(body.get("odo_real_from"))
    # DPF condition per regen, worked out on the phone (Db.dpfRows): [regen start, restriction after it, rise per km]
    dpf = None if body.get("dpf") is None else [
        (dev, _num(r[0], required=True), _num(r[1], float), _num(r[2], float)) for r in _rows(body["dpf"], 3, "dpf")
    ]
    defs = _defs(body["defs"]) if body.get("defs") is not None else None
    rules = _rules(body["rules"]) if body.get("rules") is not None else None
    # Trips the user deleted on the phone: [from, to] time windows (ms, inclusive) of this phone's rows to drop
    deleted = [(_num(r[0], required=True), _num(r[1], required=True)) for r in _rows(body.get("deleted", []), 2, "deleted")]
    removed = 0
    with write_lock, db:
        if deleted:
            # Remember the highest sample rowid before deleting: the phones' export cursor relies on it never being handed out again
            db.execute("INSERT INTO meta VALUES ('sample_top', ?) ON CONFLICT(k) DO UPDATE SET v = MAX(CAST(v AS INTEGER), CAST(excluded.v AS INTEGER))",
                       (str(_sample_top(db)),))
        # Kept so /api/export can tell the other phones to drop their copies of these rows too
        db.executemany("INSERT OR IGNORE INTO deleted VALUES (?,?,?)", [(dev, a, b) for a, b in deleted])
        for a, b in deleted:
            # Open alerts go too: the phone keeps its open ones and resends them in this same request (inserted below)
            for table, col in (("sample", "t"), ("frame", "t"), ("alert", "start_t"), ("regen", "start_t")):
                removed += db.execute(f"DELETE FROM {table} WHERE device = ? AND {col} BETWEEN ? AND ?", (dev, a, b)).rowcount
        # A frame sent again (the phone moved its history onto the car's odometer) brings its odometer; the rest stays
        db.executemany("INSERT INTO frame(device, phone_rowid, t, lid, hex, odo_m) VALUES (?,?,?,?,?,?) "
                       "ON CONFLICT(device, phone_rowid) DO UPDATE SET odo_m = COALESCE(excluded.odo_m, frame.odo_m)", frames)
        # Alerts gain an end time and new text after upload, so the latest copy wins, like regens
        db.executemany("INSERT OR REPLACE INTO alert(device, id, key, level, text, start_t, end_t, odo_start, odo_end) VALUES (?,?,?,?,?,?,?,?,?)", alerts)
        if defs is not None:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('defs', ?)", (json.dumps(defs),))
        if rules is not None:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('rules', ?)", (json.dumps(rules),))
        # A re-sent row keeps what it had, except its odometer: a phone that moved its history onto the car's odometer
        # sends everything again. So a phone that lost our reply can simply upload again.
        # Rowids set here, above any ever used, so rows after a delete still sort after every export cursor
        top = _sample_top(db)
        db.executemany("INSERT INTO sample(rowid, device, phone_rowid, t, soot, temp, regen, odo_m) VALUES (?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(device, phone_rowid) DO UPDATE SET odo_m = excluded.odo_m",
                       [(top + 1 + i, *r) for i, r in enumerate(samples)])
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
            "INSERT INTO device(id, name, last_sync, odo_real_from) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name = excluded.name, last_sync = excluded.last_sync, odo_real_from = COALESCE(excluded.odo_real_from, device.odo_real_from)",
            (dev, name, now_ms, odo_real_from),
        )
    # "frames" in the reply tells the phone this hub stores them (an older hub would silently drop them); "deleted" that it took the deletes
    return {"ok": True, "samples": len(samples), "regens": len(regens), "frames": len(frames), "alerts": len(alerts), "deleted": removed}


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


SAME_BURN_MS = 30 * 60_000  # the phone's mergeSameBurn gap: a burn does not restart this soon after stopping


def merge_same_burn(merged):
    """Joins finished regens an ignition cycle split, as the phone's mergeSameBurn (Regens.kt) does: the second starts
    within SAME_BURN_MS of the first's end and its soot carries on (not 5 points or more lower, which is a new burn)."""
    out = []
    for r in merged:
        last = out[-1] if out else None
        if (last is not None and last["end"] is not None and r["end"] is not None and 0 <= r["start"] - last["end"] <= SAME_BURN_MS
                and last["soot_end"] is not None and r["soot_start"] is not None and r["soot_start"] >= last["soot_end"] - 5):
            last["end"], last["soot_end"] = r["end"], r["soot_end"]
            last["devices"] |= r["devices"]
        else:
            out.append(r)
    return out


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


def _car_phones(db):
    """Phones whose odo_m is the car's own odometer (app 1.2.0 and later, once it read the cluster)."""
    return [d for (d,) in db.execute("SELECT id FROM device WHERE odo_real_from IS NOT NULL")]


def car_odometer(db, t, phones=None):
    """The car's odometer at instant `t` (metres): the newest reading by then from any phone that logs the car's own
    odometer, else the earliest one after. None when no phone does."""
    phones = _car_phones(db) if phones is None else phones
    if not phones:
        return None
    marks = ",".join("?" * len(phones))
    row = db.execute(f"SELECT odo_m FROM sample WHERE device IN ({marks}) AND t <= ? AND odo_m IS NOT NULL ORDER BY t DESC LIMIT 1",
                     (*phones, t)).fetchone() or \
        db.execute(f"SELECT odo_m FROM sample WHERE device IN ({marks}) AND odo_m IS NOT NULL ORDER BY t LIMIT 1", phones).fetchone()
    return row[0] if row else None


def distance_m(db, t0, t1):
    """Metres the car drove between two instants. Phones on the car's odometer all log the same number, so it is the
    odometer at t1 minus at t0: a drive two phones logged counts once. Phones still on GPS distance (each its own scale,
    only growing) add up their progress, (last reading by t1) - (last reading by t0), as before."""
    car = _car_phones(db)
    if car:
        a, b = car_odometer(db, t0, car), car_odometer(db, t1, car)
        return max(0.0, b - a) if a is not None and b is not None else 0.0
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


def merged_regens(db, now_ms):
    rows = [close_if_stale(db, r, now_ms) for r in
            db.execute("SELECT device, start_t, end_t, soot_start, soot_end FROM regen ORDER BY start_t")]
    return merge_same_burn(merge_regens(rows))


def trips(db, now_ms):
    """Every drive of the car, newest first: a run of readings from any phone with no gap over TRIP_GAP_MS."""
    names = dict(db.execute("SELECT id, name FROM device"))
    car = set(_car_phones(db))
    out, odo = [], {}
    for t, soot, temp, dev, odo_m in db.execute("SELECT t, soot, temp, device, odo_m FROM sample ORDER BY t"):
        if not out or t - out[-1]["end"] > TRIP_GAP_MS:
            out.append({"start": t, "end": t, "soot_start": None, "soot_end": None, "peak_temp": None, "distance_m": 0.0, "drivers": set()})
            odo = {}
        trip = out[-1]
        trip["end"] = t
        trip["drivers"].add(names.get(dev, "?"))
        if soot is not None:
            trip["soot_start"] = soot if trip["soot_start"] is None else trip["soot_start"]
            trip["soot_end"] = soot
        if temp is not None:
            trip["peak_temp"] = temp if trip["peak_temp"] is None else max(trip["peak_temp"], temp)
        # As in distance_m: phones on the car's odometer share one number, so a drive two logged counts once; only
        # with none of them do phones on GPS distance add up their own progress
        if odo_m is not None and (not car or dev in car):
            key = "car" if car else dev
            trip["distance_m"] += max(0.0, odo_m - odo.get(key, odo_m))
            odo[key] = max(odo_m, odo.get(key, odo_m))
    regens = merged_regens(db, now_ms)
    warned = [r[0] for r in db.execute("SELECT start_t FROM alert")]
    for trip in out:
        trip["drivers"] = sorted(trip["drivers"])
        trip["regens"] = sum(r["start"] <= trip["end"] and (r["end"] is None or r["end"] >= trip["start"]) for r in regens)
        trip["warnings"] = sum(trip["start"] <= t <= trip["end"] for t in warned)
    return {"trips": out[::-1]}


def summary(db, now_ms):
    names = dict(db.execute("SELECT id, name FROM device"))
    regens = merged_regens(db, now_ms)
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
        # The car's odometer at the newest reading (metres); None until a phone logs it
        "odometer_m": car_odometer(db, now_ms),
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


def _rows_for_stats(db, lid, t0, t1):
    n, lo, hi = db.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM frame WHERE lid = ? AND t BETWEEN ? AND ?", (lid, t0, t1)).fetchone()
    if n <= STATS_ROWS:
        return db.execute("SELECT t, hex FROM frame WHERE lid = ? AND t BETWEEN ? AND ? ORDER BY t", (lid, t0, t1)).fetchall()
    # shortcut: long ranges read one frame per slice, so min/max can miss a brief spike (the charts read every frame); pre-aggregate if that matters
    return db.execute("SELECT MAX(t), hex FROM frame WHERE lid = ? AND t BETWEEN ? AND ? GROUP BY t / ? ORDER BY 1",
                      (lid, t0, t1, (hi - lo) // STATS_ROWS + 1)).fetchall()


def stats(db, t0, t1):
    """Every PID over a time range, with its plain-English name: the last value and min/avg/max, or how long in each state."""
    out, frames = [], {}
    for d in defs_of(db):
        lid, pid_id = d[0], f"{d[0]}.{d[1]}"
        if lid not in frames:
            frames[lid] = _rows_for_stats(db, lid, t0, t1)
        vals = [v for _, h in frames[lid] if (v := decode(d, h)) is not None]
        label, about = LABELS.get(pid_id, (d[9], ""))
        row = {"id": pid_id, "label": label, "about": about, "name": d[9], "unit": d[10], "category": d[11],
               "enum": STATES.get(pid_id, d[12]), "decimals": d[8], "count": len(vals), "last": vals[-1] if vals else None,
               "t": frames[lid][-1][0] if vals else None}
        if vals and d[12]:
            row["shares"] = {int(k): c / len(vals) for k, c in Counter(vals).items()}
        elif vals:
            row.update(min=min(vals), avg=sum(vals) / len(vals), max=max(vals))
        out.append(row)
    return {"values": out}


def series(db, pid_id, t0, t1):
    """[[t, value], ...] of one PID oldest first, within t0..t1."""
    d = next((d for d in defs_of(db) if f"{d[0]}.{d[1]}" == pid_id), None)
    if d is None:
        return []
    rows = db.execute("SELECT t, hex FROM frame WHERE lid = ? AND t BETWEEN ? AND ? ORDER BY t", (d[0], t0, t1)).fetchall()
    return [[t, v] for t, h in rows if (v := decode(d, h)) is not None]


def bucketed(points, n=SERIES_POINTS):
    """[[t, avg, min, max], ...]: at most `n` rows, with short series passed through untouched so no detail is lost."""
    if len(points) <= n:
        return [[t, v, v, v] for t, v in points]
    width = (points[-1][0] - points[0][0]) // n + 1
    out = []
    for _, group in groupby(points, key=lambda p: p[0] // width):
        group = list(group)
        vals = [p[1] for p in group]
        out.append([group[0][0], sum(vals) / len(vals), min(vals), max(vals)])
    return out


def warnings(db, limit=500):
    """Every stored warning, newest first, plus what each rule checks and the values it reads (as PID ids for /api/series)."""
    ids = {}
    for d in defs_of(db):
        pid_id = f"{d[0]}.{d[1]}"
        ids.setdefault(d[9], {"id": pid_id, "name": LABELS.get(pid_id, (d[9],))[0], "unit": d[10], "enum": STATES.get(pid_id, d[12]), "decimals": d[8]})
    row = db.execute("SELECT v FROM meta WHERE k = 'rules'").fetchone()
    rules = {
        key: {"title": title, "level": level, "about": about, "values": [ids[u] for u in uses if u in ids]}
        for key, title, level, about, uses in (json.loads(row[0]) if row else [])
    }
    history = [
        {"key": k, "level": lv, "text": tx, "start": st, "end": en, "driver": dr, "odo_start": o0, "odo_end": o1}
        for k, lv, tx, st, en, dr, o0, o1 in db.execute(
            "SELECT a.key, a.level, a.text, a.start_t, a.end_t, d.name, a.odo_start, a.odo_end FROM alert a JOIN device d ON d.id = a.device "
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
        # Every other phone's deleted windows, whole each time (a short list): the phone drops its copies in them
        "deleted": [list(r) for r in db.execute("SELECT device, from_t, to_t FROM deleted WHERE device != ?", (device,))],
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
        elif self.path == "/api/trips":
            db = connect()
            try:
                self._send(200, trips(db, int(time.time() * 1000)))
            finally:
                db.close()
        elif self.path.startswith("/api/stats?"):
            q = parse_qs(urlsplit(self.path).query)
            t0, t1 = q.get("from", [""])[0], q.get("to", [""])[0]
            if not (t0.isdigit() and t1.isdigit()):
                return self._send(400, {"error": "need from=<ms>&to=<ms>"})
            db = connect()
            try:
                self._send(200, stats(db, int(t0), int(t1)))
            finally:
                db.close()
        elif self.path.startswith("/api/series?"):
            q = parse_qs(urlsplit(self.path).query)
            pid_id = q.get("id", [""])[0]
            t0, t1 = q.get("from", [""])[0], q.get("to", [""])[0]
            if not PID_ID_RE.match(pid_id) or not (t0.isdigit() and t1.isdigit()):
                return self._send(400, {"error": "need id=<lid>.<bit>&from=<ms>&to=<ms>"})
            db = connect()
            try:
                self._send(200, {"points": bucketed(series(db, pid_id, int(t0), int(t1)))})
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
        elif self.path in FONTS:  # bundled so the dashboard needs no internet on the LAN
            self._send(200, (HERE / "fonts" / self.path[7:]).read_bytes(), FONTS[self.path])
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
        if not self.path.startswith(("/api/summary", "/api/export", "/api/trips", "/api/stats", "/api/series", "/api/warnings")):  # polled; keep the log readable
            super().log_message(fmt, *args)


if __name__ == "__main__":
    connect().close()
    print(f"Yaris hub on :{PORT}, database {DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
