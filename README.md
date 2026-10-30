# Yaris Doctor Hub

A small home-server companion for **[Yaris Doctor](https://github.com/alexscemo/yaris-doctor)**, the Android app that
logs a Toyota Yaris diesel's DPF soot load, temperature and regenerations from an ELM327 Bluetooth dongle.

Every phone running Yaris Doctor uploads its log to the hub when it joins your home Wi-Fi. The hub keeps one merged
database for the car — so if two people drive it, their trips add up — and serves a dashboard:

- current soot load, distance since the last regen, average distance between regens
- a "Regenerating · keep driving" banner while a regen is live
- soot over the last 30 days and distance between regens (hover or tap to read values)
- DPF condition: exhaust restriction right after each regen (rising = ash building up) and how fast it rises per mile or km
- regen history with who was driving, and one card per phone
- every time chart shades the periods the DPF was regenerating

The dashboard uses the app's look and fonts (Hanken Grotesk and PT Mono, OFL, bundled in `app/fonts` with their licences), so it needs no internet.

No accounts and no logins: a phone is identified by a random install id plus the driver name set in the app.
**It has no authentication, so keep it on your LAN** — don't expose it to the internet.

## Run it

```sh
docker compose up -d   # uses ghcr.io/alexscemo/yaris-doctor-hub:latest, data in ./data
```

or

```sh
docker run -d --name yaris-hub --restart unless-stopped -p 8765:8765 -v "$PWD/data:/data" ghcr.io/alexscemo/yaris-doctor-hub:latest
```

Open `http://<server-ip>:8765`. Images are built for `amd64` and `arm64` (so a Raspberry Pi works too).

### ZimaOS / CasaOS

To get a dashboard tile, install it as a custom app (App Store → Custom Install → import), with this compose file:

```yaml
name: yaris-hub
services:
  yaris-hub:
    container_name: yaris-hub
    image: ghcr.io/alexscemo/yaris-doctor-hub:latest
    restart: unless-stopped
    network_mode: bridge
    ports:
      - target: 8765
        published: "8765"
        protocol: tcp
    volumes:
      - type: bind
        source: /DATA/AppData/yaris-hub/data
        target: /data
x-casaos:
  main: yaris-hub
  icon: https://raw.githubusercontent.com/alexscemo/yaris-doctor-hub/main/app/icon.svg
  port_map: "8765"
  scheme: http
  index: /
  category: Utilities
  title:
    en_us: Yaris Doctor
```

## Connect the phones

In Yaris Doctor: **Settings → Home hub**

1. **Hub address**: the server, e.g. `192.168.1.50` (port 8765 is assumed) or `http://192.168.1.50:8765`.
2. **Home Wi-Fi name**: your network's SSID. Uploads only happen while connected to it.
3. **Driver name**: shown on the dashboard.

Both are empty by default, so nothing is ever uploaded until you set them. The app then uploads new data about once a
minute after a drive, whenever it is on that Wi-Fi; **Sync now** forces an upload.

## How the data is combined

- Readings and regens are stored per phone. Re-uploads keep the row but take its new odometer (readings, frames) or
  replace the row (regens and warnings, which gain an end time later), so a phone can always resend safely.
- A regen seen by two phones (one hands the dongle to the other mid-burn) counts once.
- Distance between regens is the car's distance. Phones on app 1.2.0 or later log the car's own odometer (read from
  the dashboard), so it is the odometer at the end minus at the start, and a drive two phones logged counts once. The
  dashboard shows the odometer too, and each warning's mileage. Phones on older versions log GPS distance, summed per
  phone as before; update every phone to get the shared odometer.
- If a phone is killed mid-regen and never records the end, the hub ends that regen at its last reading.
- **Data:** the phones also upload every raw engine reply ("frames") and their PID table, so the **Data** view can show
  any drive or period in full. Every trip is listed with its length, distance, soot, peak DPF temperature, regens and
  warnings; sort by any column and click one, or pick a period. Charts of the values you choose stack on one time axis:
  hover reads them all at the same moment, and dragging across one zooms them all down to single readings. Below them,
  all ~100 engine values by category under plain-English names (`app/labels.py`), each with its last value and range over
  that period. Clicking a regen in the Overview's history opens that burn the same way.
- **Full-detail backup of every frame:** the hub keeps every frame ever recorded and never thins them. The phones keep
  full detail for 7 days only, then one frame per value per minute up to 90 days and one per hour after that, and they
  never thin a frame the hub hasn't received yet (a phone away from home for weeks keeps everything until it syncs).
  So the hub's database is the only complete copy: back up its `data` folder.
- **Health:** the phones run the health checks and upload what they find. The Overview's status card shows "All good"
  or the open warnings, and what was seen in the last 7 days. The hub repeats the phones' results and checks nothing itself.
- **Warning history:** every warning a phone ever logged is kept. Click one (in the status card or the Warning history
  table) to open it: what the check looks for and where its threshold comes from, every time it happened, and a chart
  of each value it reads from 2 minutes before to 2 minutes after, with the warning's own time shaded.

Each phone keeps its own readings, regens and warnings too, so losing the hub loses only the frames older than the
phones' 7 days of full detail. Phones also download the other phones' logs from the hub while at home, so each phone's
own screen shows the whole car.

**Upgrades:** the database records its schema version (`PRAGMA user_version`), and a new image upgrades an existing
`hub.db` in place on first start, keeping every row. Pull the new image and restart the container; nothing else to do.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/sync` | Phone upload: `{device_id, name, samples: [[rowid, t, soot, temp, regen, odo_m]…], regens: [[id, start_t, end_t, odo_start, odo_end, soot_start, soot_end]…]}`, optionally `frames: [[rowid, t, lid, hex]…]`, `alerts: [[id, key, level, text, start_t, end_t]…]`, `rules: [[key, title, level, about, [value names]]…]` `defs` (the PID table: `[[lid, bit_start, bit_end, len, signed, mul, div, offset, decimals, name, unit, category, enum]…]`), `dpf: [[regen_start, base, per_km]…]` (DPF condition per regen, replaces the phone's previous set), `regens_all: true` (the regens are the phone's whole list: ones it no longer has are deleted) and `deleted: [[from, to]…]` (trips deleted on the phone: its samples, frames, closed alerts and regens starting in those inclusive time windows are removed). The reply counts what was stored; its `frames` key tells the phone this hub stores frames, its `deleted` key (rows removed) that it takes deletes |
| `GET` | `/api/export?device=<id>&after=<cursor>` | What every *other* phone logged, so each phone shows the whole car: `{samples: [[device, phone_rowid, t, soot, temp, regen, odo_m]…], regens: [[device, id, start_t, end_t, soot_start, soot_end]…], next, more}`. Samples come 5000 at a time after `cursor` (start at 0, then pass `next` while `more`); regens are always the full list, stale open ones closed |
| `GET` | `/api/summary` | Everything the Overview shows, as JSON, including `alerts` (open and last 7 days, one per check) |
| `GET` | `/api/trips` | Every drive of the car, newest first: `{trips: [{start, end, soot_start, soot_end, peak_temp, distance_m, drivers, regens, warnings}…]}` |
| `GET` | `/api/stats?from=<ms>&to=<ms>` | Every engine PID over that window: `{values: [{id, label, about, name, unit, category, enum, decimals, count, last, t, min, avg, max}…]}`, with `shares: {state: fraction}` in place of min/avg/max for a value with named states. Over very long windows it reads one frame per slice, so min/max can miss a brief spike |
| `GET` | `/api/series?id=<lid>.<bit>&from=<ms>&to=<ms>` | One PID over that window: `{points: [[t, avg, min, max]…]}`, every reading when there are up to 1500, otherwise averaged into about that many time buckets |
| `GET` | `/api/warnings` | Every warning, newest first (up to 500), and each check's title, level, description and the values it reads: `{rules: {key: {title, level, about, values}}, history: [{key, level, text, start, end, driver}…]}` |
| `GET` | `/health` | `{"ok": true}` |

Settings via environment: `DB_PATH` (default `/data/hub.db`), `PORT` (default `8765`).

## Develop

Python standard library only, no dependencies.

```sh
cd app
python -m unittest -v test_app.py   # tests (in-memory database)
# schema changes: append a step to MIGRATIONS in app.py; never edit SCHEMA or an existing step
DB_PATH=dev.db python app.py         # run locally on :8765
```

Pushing to `main` publishes `:latest`; a `vX.Y.Z` tag publishes `:X.Y.Z`.

## License

[MIT](LICENSE)
