"""Run: python -m unittest server/app/test_app.py (stdlib only, uses an in-memory database)."""
import unittest

import app

MIN = 60_000
A = "aaaaaaaa-1111"
B = "bbbbbbbb-2222"


def drive(dev, start_t, minutes, odo0, regen_from=None, regen_to=None, first_rowid=1):
    """One sample a minute at 1 km/min; regen flag set in [regen_from, regen_to)."""
    return [
        [first_rowid + i, start_t + i * MIN, 50, 300, regen_from is not None and regen_from <= i < regen_to, odo0 + i * 1000.0]
        for i in range(minutes)
    ]


class HubTest(unittest.TestCase):
    def setUp(self):
        self.db = app.connect(":memory:")

    def sync(self, dev, name, samples=(), regens=(), now=10**13):
        return app.ingest(self.db, {"device_id": dev, "name": name, "samples": list(samples), "regens": list(regens)}, now)

    def test_resending_the_same_rows_is_harmless(self):
        rows = drive(A, 0, 10, 0)
        self.sync(A, "Alex", rows)
        self.sync(A, "Alex", rows)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 10)

    def test_rejects_bad_input(self):
        for body in (
            [],
            {"device_id": "x", "samples": []},                                   # id too short
            {"device_id": A, "samples": [[1, "noon", 1, 1, 0, 1.0]]},           # t not a number
            {"device_id": A, "samples": [[1, 2, 3]]},                           # wrong width
            {"device_id": A, "regens": [[1, None, None, None, None, None, None]]},  # start missing
        ):
            with self.assertRaises(ValueError):
                app.ingest(self.db, body, 0)

    def test_combines_two_phones_into_car_distance_and_merges_shared_regen(self):
        # Alex drives 0-60 min (60 km), regen 10-20; partner drives 100-160 min (60 km), regen 140-150.
        # Partner's phone also caught the end of Alex's regen during a dongle hand-over (19-21 min).
        self.sync(A, "Alex", drive(A, 0, 60, 0), [[1, 10 * MIN, 20 * MIN, 10_000.0, 20_000.0, 80, 10]])
        self.sync(B, "Sam", drive(B, 100 * MIN, 60, 5_000.0),
                  [[1, 19 * MIN, 21 * MIN, None, None, 70, 9], [2, 140 * MIN, 150 * MIN, None, None, 75, 12]])
        s = app.summary(self.db, 200 * MIN)
        self.assertEqual(len(s["regens"]), 2, "the hand-over regen is one burn")
        newest, oldest = s["regens"]
        self.assertEqual(oldest["drivers"], ["Alex", "Sam"])
        self.assertEqual(oldest["end"], 21 * MIN)
        self.assertIsNone(oldest["gap_m"])
        # Between 21 and 140 min: Alex drove 21..59 = 38 km, Sam 100..140 = 40 km
        self.assertAlmostEqual(newest["gap_m"], 78_000.0)
        self.assertAlmostEqual(s["since_regen_m"], 9_000.0)  # Sam 150..159

    def test_stale_open_regen_is_closed_and_does_not_swallow_later_ones(self):
        # Phone killed mid-regen: end_t never written, then a later complete regen
        self.sync(A, "Alex", drive(A, 0, 30, 0, regen_from=10, regen_to=30),
                  [[1, 10 * MIN, None, None, None, 80, None], [2, 500 * MIN, 510 * MIN, None, None, 70, 10]])
        s = app.summary(self.db, 1000 * MIN)
        self.assertEqual(len(s["regens"]), 2)
        self.assertEqual(s["regens"][1]["end"], 29 * MIN)

    def test_running_regen_stays_open_while_phone_is_live(self):
        self.sync(A, "Alex", drive(A, 0, 30, 0, regen_from=10, regen_to=30), [[1, 10 * MIN, None, None, None, 80, None]])
        s = app.summary(self.db, 29 * MIN + 5_000)
        self.assertIsNone(s["regens"][0]["end"])
        self.assertTrue(s["latest"]["live"] and s["latest"]["regen"])

    def test_export_gives_other_phones_rows_in_pages(self):
        self.sync(A, "Alex", drive(A, 0, 10, 0), [[1, 2 * MIN, 4 * MIN, None, None, 80, 10]])
        self.sync(B, "Sam", drive(B, 100 * MIN, 7, 0), [[3, 101 * MIN, None, None, None, 70, None]])
        app.EXPORT_PAGE, page = 4, app.EXPORT_PAGE
        try:
            got, after = [], 0
            while True:
                e = app.export(self.db, A, after, 1000 * MIN)
                got += e["samples"]
                after = e["next"]
                if not e["more"]:
                    break
        finally:
            app.EXPORT_PAGE = page
        self.assertEqual(len(got), 7, "all of Sam's rows, none of Alex's")
        self.assertTrue(all(r[0] == B for r in got))
        self.assertEqual(got[0][:3], [B, 1, 100 * MIN])  # device, phone_rowid, t
        self.assertEqual(e["regens"], [[B, 3, 101 * MIN, 101 * MIN, 70, None]], "stale open regen closed at its start (no regen readings)")

    # Rows of the phone's table (Pid.kt pidDefs), with payloads from the Techstream capture
    DEFS = [
        ["DD", 136, 143, 18, False, 2, 1, 0, 0, "PM Accumulation Ratio", "%", "DPF / exhaust", None],
        ["F1", 32, 47, 9, True, 25, 4, 0, 1, "Exhaust Temperature B1S1", "°C", "DPF / exhaust", None],
        ["AD", 64, 79, 26, True, 1, 1, 0, 1, "Pilot 1 Injection Timing", "deg(CA)", "Fuel / injection", None],
        ["DF", 72, 79, 15, False, 1, 1, 0, 0, "DPNR/DPF Status Reju(PM)", "", "DPF / exhaust", ["Standby", "Ready", "Operate", "Compl"]],
    ]
    DD = "440029642420291432" "09C4C75512004F8D17"
    F1 = "00660060005F005636"

    def test_frames_decode_like_the_phone(self):
        r = app.ingest(self.db, {"device_id": A, "defs": self.DEFS,
                                 "frames": [[1, 1000, "DD", self.DD], [2, 1000, "F1", self.F1], [3, 2000, "DD", self.DD[:-2] + "19"]]}, 0)
        self.assertEqual(r["frames"], 3, "the reply tells the phone frames are stored")
        v = {x["name"]: x for x in app.live(self.db, 5000)["values"]}
        self.assertEqual(v["PM Accumulation Ratio"]["value"], 50.0)  # newest frame: 0x19 * 2
        self.assertEqual(v["PM Accumulation Ratio"]["t"], 2000)
        self.assertAlmostEqual(v["Exhaust Temperature B1S1"]["value"], 59.3)  # 95*25/4 = 593 (truncated) / 10
        self.assertIsNone(v["Pilot 1 Injection Timing"]["value"], "never read")
        self.assertEqual(app.series(self.db, "DD.136", 1800000), [[1000, 46.0], [2000, 50.0]])
        # Signed fields, truncated toward zero like Kotlin: 0xFFBA = -70 → -7.0; 0xFFFF = -1, -1*25/4 = -6.25 → -6 → -0.6
        self.assertEqual(app.decode(self.DEFS[2], "0" * 16 + "FFBA" + "0" * 32), -7.0)
        self.assertEqual(app.decode(self.DEFS[1], "0" * 8 + "FFFF" + "0" * 6), -0.6)
        self.assertEqual(app.decode(self.DEFS[3], "0" * 18 + "02" + "0" * 10), 2.0)

    def test_rejects_bad_frames_alerts_and_defs(self):
        for extra in (
            {"frames": [[1, 1, "DD", "6x"]]},                                            # not hex
            {"frames": [[1, 1, "21DD", "00"]]},                                          # lid is one byte
            {"alerts": [[1, "Coolant Hot", 1, "x", 0, None]]},                           # key format
            {"alerts": [[1, "dtc", 1, "x" * 301, 0, None]]},                             # text too long
            {"defs": [["DD", 136, 143, 18, False, 2, 0, 0, 0, "x", "%", "c", None]]},    # div 0
            {"defs": [["DD", 136, 200, 18, False, 2, 1, 0, 0, "x", "%", "c", None]]},    # bits past the payload
        ):
            with self.assertRaises(ValueError, msg=extra):
                app.ingest(self.db, {"device_id": A, **extra}, 0)

    def test_alerts_one_per_key_open_first(self):
        self.sync(A, "Alex")
        self.sync(B, "Sam")
        app.ingest(self.db, {"device_id": A, "alerts": [[1, "dtc", 1, "1 fault code", 100, 200], [2, "coolant_hot", 1, "106 °C", 300, 400]]}, 0)
        app.ingest(self.db, {"device_id": B, "alerts": [[1, "dtc", 1, "2 fault codes", 500, None]]}, 0)
        got = app.alerts(self.db, 0)
        self.assertEqual([(a["key"], a["text"], a["end"]) for a in got], [("dtc", "2 fault codes", None), ("coolant_hot", "106 °C", 400)])
        # The phone resends an alert when it ends: the newer copy replaces it
        app.ingest(self.db, {"device_id": B, "alerts": [[1, "dtc", 1, "2 fault codes", 500, 900]]}, 0)
        self.assertEqual(app.alerts(self.db, 0)[0]["end"], 900)
        self.assertEqual(app.alerts(self.db, 1000), [], "both ended before the window")

    RULES = [
        ["soot_high", "DPF soot high", 1, "DPF soot at 100-139 %.", ["PM Accumulation Ratio"]],
        ["regen_frequent", "Regens too frequent", 1, "Regens under 150 km apart.", []],
        ["coolant_hot", "Coolant hot", 1, "Coolant at 105-111 °C.", ["Coolant Temp"]],  # value not in DEFS: dropped
    ]

    def test_warning_history_with_rules_and_the_values_to_chart(self):
        app.ingest(self.db, {"device_id": A, "name": "Alex", "defs": self.DEFS, "rules": self.RULES,
                             "frames": [[1, 1000, "DD", self.DD], [2, 5000, "DD", self.DD[:-2] + "40"], [3, 9000, "DD", self.DD]],
                             "alerts": [[1, "soot_high", 1, "DPF soot is at 128 %.", 4000, 6000],
                                        [2, "regen_frequent", 1, "Only 90 km apart.", 7000, None]]}, 0)
        w = app.warnings(self.db)
        self.assertEqual([h["key"] for h in w["history"]], ["regen_frequent", "soot_high"], "newest first")
        soot = w["rules"]["soot_high"]
        self.assertEqual((soot["title"], soot["about"]), ("DPF soot high", "DPF soot at 100-139 %."))
        self.assertEqual([v["id"] for v in soot["values"]], ["DD.136"])
        self.assertEqual(w["rules"]["regen_frequent"]["values"], [])
        self.assertEqual(w["rules"]["coolant_hot"]["values"], [])
        # The chart around that warning: only frames inside the window
        self.assertEqual(app.series(self.db, "DD.136", 0, (3000, 7000)), [[5000, 128.0]])

    def test_rejects_bad_rules(self):
        for rules in (
            [["Soot High", "t", 1, "a", []]],          # key format
            [["soot_high", "t", 3, "a", []]],          # level
            [["soot_high", "t", 1, "x" * 601, []]],    # description too long
            [["soot_high", "t", 1, "a", "PM"]],        # uses not a list
        ):
            with self.assertRaises(ValueError, msg=rules):
                app.ingest(self.db, {"device_id": A, "rules": rules}, 0)

    def test_full_regen_list_drops_rows_the_phone_deleted(self):
        r1, blip = [1, 10 * MIN, 20 * MIN, 10_000.0, 20_000.0, 80, 10], [2, 30 * MIN, 30 * MIN + 3000, 30_000.0, 30_000.0, 70, 70]
        self.sync(A, "Alex", drive(A, 0, 40, 0), [r1, blip])
        self.sync(B, "Sam", (), [[1, 100 * MIN, 110 * MIN, None, None, 75, 12]])
        # An older phone's later batches send an empty list without the flag: nothing goes
        self.sync(A, "Alex", (), [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM regen WHERE device = ?", (A,)).fetchone()[0], 2)
        app.ingest(self.db, {"device_id": A, "name": "Alex", "regens": [r1], "regens_all": True}, 10**13)
        self.assertEqual(self.db.execute("SELECT device, id FROM regen ORDER BY device").fetchall(), [(A, 1), (B, 1)])

    def test_dpf_condition_is_stored_per_phone_and_shown(self):
        app.ingest(self.db, {"device_id": A, "name": "Alex", "dpf": [[10 * MIN, 6.7, 0.144], [50 * MIN, None, None]]}, 0)
        app.ingest(self.db, {"device_id": A, "name": "Alex", "dpf": [[10 * MIN, 6.5, 0.15]]}, 0)  # replaces the phone's set
        self.assertEqual(app.summary(self.db, 60 * MIN)["dpf"], [{"start": 10 * MIN, "base": 6.5, "per_km": 0.15, "driver": "Alex"}])
        with self.assertRaises(ValueError):
            app.ingest(self.db, {"device_id": A, "dpf": [[10 * MIN, "6.5", None]]}, 0)

    def test_deleted_trips_drop_only_that_phones_rows_in_range(self):
        self.sync(A, "Alex", drive(A, 0, 20, 0) + drive(A, 60 * MIN, 10, 20_000.0, first_rowid=21),
                  [[1, 5 * MIN, 9 * MIN, 5_000.0, 9_000.0, 80, 10], [2, 62 * MIN, 66 * MIN, None, None, 70, 9]])
        self.sync(B, "Sam", drive(B, 0, 20, 0))
        app.ingest(self.db, {"device_id": A, "name": "Alex",
                             "frames": [[1, 3 * MIN, "0C", "0BB8"], [2, 61 * MIN, "0C", "0BB8"]],
                             "alerts": [[1, "soot_floor", 1, "x", 4 * MIN, 6 * MIN], [2, "regen_frequent", 1, "y", 9 * MIN, None]]}, 10**13)
        # The phone kept its open warning and resends it with the deletes
        reply = app.ingest(self.db, {"device_id": A, "name": "Alex", "deleted": [[0, 21 * MIN]],
                                     "alerts": [[2, "regen_frequent", 1, "y", 9 * MIN, None]]}, 10**13)
        self.assertEqual(reply["deleted"], 20 + 1 + 2 + 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sample WHERE device = ?", (A,)).fetchone()[0], 10)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sample WHERE device = ?", (B,)).fetchone()[0], 20)
        self.assertEqual(self.db.execute("SELECT id FROM regen WHERE device = ?", (A,)).fetchall(), [(2,)])
        self.assertEqual(self.db.execute("SELECT phone_rowid FROM frame").fetchall(), [(2,)])
        self.assertEqual(self.db.execute("SELECT id FROM alert").fetchall(), [(2,)], "an open warning stays")
        self.assertEqual(app.export(self.db, B, 0, 10**13)["deleted"], [[A, 0, 21 * MIN]], "Sam's phone is told to drop its copies")
        self.assertEqual(app.export(self.db, A, 0, 10**13)["deleted"], [], "never a phone's own windows")

    def test_regen_split_by_the_ignition_counts_once(self):
        # 10 min burn, engine off for 5, the same burn carries on (soot 40 -> 41); then a real new burn hours later
        self.sync(A, "Alex", drive(A, 0, 30, 0), [[1, 0, 10 * MIN, None, None, 80, 40], [2, 15 * MIN, 25 * MIN, None, None, 41, 10],
                                                  [3, 300 * MIN, 315 * MIN, None, None, 85, 10]])
        regens = app.summary(self.db, 10**13)["regens"]
        self.assertEqual([(r["start"], r["end"], r["soot_start"], r["soot_end"]) for r in regens[::-1]],
                         [(0, 25 * MIN, 80, 10), (300 * MIN, 315 * MIN, 85, 10)])
        self.assertEqual(self.sync(A, "Alex")["deleted"], 0)
        for bad in ([[0]], [["0", 1]], "all"):
            with self.assertRaises(ValueError):
                app.ingest(self.db, {"device_id": A, "deleted": bad}, 0)

    def test_rows_after_a_delete_still_reach_phones_past_the_export_cursor(self):
        self.sync(A, "Alex", drive(A, 0, 10, 0))
        self.sync(A, "Alex", drive(A, 60 * MIN, 10, 20_000.0, first_rowid=11))
        cursor = app.export(self.db, B, 0, 10**13)["next"]  # Sam's phone has pulled everything
        app.ingest(self.db, {"device_id": A, "name": "Alex", "deleted": [[60 * MIN, 72 * MIN]]}, 10**13)  # Alex's newest trip
        self.sync(A, "Alex", drive(A, 100 * MIN, 5, 30_000.0, first_rowid=21))
        got = app.export(self.db, B, cursor, 10**13)["samples"]
        self.assertEqual([r[1] for r in got], [21, 22, 23, 24, 25], "new rows sort after the cursor, not in the deleted rows' place")

    def car(self, dev, name, samples, real_from=0, frames=(), alerts=()):
        """A sync from app 1.2.0: odo_m is the car's odometer from `real_from` on."""
        return app.ingest(self.db, {"device_id": dev, "name": name, "samples": list(samples), "odo_real_from": real_from,
                                    "frames": list(frames), "alerts": list(alerts)}, 10**13)

    def test_phones_on_the_car_odometer_count_a_shared_drive_once(self):
        car0 = 143_000_000.0
        # Both phones in the car for the first hour (same odometer), then only Sam's for another 30 min
        self.car(A, "Alex", drive(A, 0, 60, car0))
        self.car(B, "Sam", drive(B, 0, 90, car0))
        self.assertAlmostEqual(app.distance_m(self.db, 0, 89 * MIN), 89_000.0, msg="one car, one odometer: not 59 + 89 km")
        self.assertAlmostEqual(app.distance_m(self.db, 10 * MIN, 20 * MIN), 10_000.0)
        self.assertAlmostEqual(app.summary(self.db, 100 * MIN)["odometer_m"], car0 + 89_000.0)

    def test_a_resent_row_takes_its_new_odometer_and_keeps_the_rest(self):
        self.sync(A, "Alex", drive(A, 0, 3, 1_000.0))  # GPS distance, as before 1.2.0
        app.ingest(self.db, {"device_id": A, "frames": [[1, 0, "DD", "00"]]}, 0)  # an old phone's 4-value frame
        moved = [r[:5] + [r[5] + 143_000_000.0] for r in drive(A, 0, 3, 1_000.0)]
        self.car(A, "Alex", moved, frames=[[1, 0, "DD", "00", 143_001_000.0]])
        self.assertEqual(self.db.execute("SELECT COUNT(*), MIN(odo_m) FROM sample").fetchone(), (3, 143_001_000.0))
        self.assertEqual(self.db.execute("SELECT COUNT(*), MIN(odo_m) FROM frame").fetchone(), (1, 143_001_000.0))
        app.ingest(self.db, {"device_id": A, "frames": [[1, 0, "DD", "00"]]}, 0)  # resent without one: kept
        self.assertEqual(self.db.execute("SELECT odo_m FROM frame").fetchone()[0], 143_001_000.0)

    def test_warnings_carry_the_odometer_and_old_phones_still_sync(self):
        self.car(A, "Alex", drive(A, 0, 3, 143_000_000.0), alerts=[[1, "dtc", 1, "1 code", 0, 60_000, 143_000_000.0, 143_001_000.0]])
        h = app.warnings(self.db)["history"][0]
        self.assertEqual((h["odo_start"], h["odo_end"]), (143_000_000.0, 143_001_000.0))
        app.ingest(self.db, {"device_id": B, "alerts": [[1, "dtc", 1, "1 code", 0, None]]}, 0)  # 6 values, as before
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM alert").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            app.ingest(self.db, {"device_id": A, "alerts": [[1, "dtc", 1, "x", 0, None, 1.0]]}, 0)  # 7: neither shape

    def test_migrations_upgrade_an_old_database_once_and_keep_its_rows(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "hub.db")
        old = app.connect(path)
        app.ingest(old, {"device_id": A, "name": "Alex", "samples": drive(A, 0, 10, 0)}, 10**13)
        old.execute("DROP INDEX sample_device_odo")
        old.execute("DROP TABLE frame")  # the frame table before the odometer column
        old.execute("CREATE TABLE frame(device TEXT NOT NULL, phone_rowid INTEGER NOT NULL, t INTEGER NOT NULL, lid TEXT NOT NULL, "
                    "hex TEXT NOT NULL, PRIMARY KEY(device, phone_rowid))")
        old.execute("PRAGMA user_version = 0")  # as a hub from before migrations left it
        old.close()
        db = app.connect(path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], len(app.MIGRATIONS))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 10)
        plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN SELECT MIN(odo_m) FROM sample WHERE device = ?", (A,)))
        self.assertIn("sample_device_odo", plan, "distance_m's base odometer is an index lookup, not a scan")
        self.assertIn("odo_m", {r[1] for r in db.execute("PRAGMA table_info(frame)")})
        db.close()
        app.connect(path).close()  # a second start runs nothing again


if __name__ == "__main__":
    unittest.main()
