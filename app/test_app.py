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


if __name__ == "__main__":
    unittest.main()
