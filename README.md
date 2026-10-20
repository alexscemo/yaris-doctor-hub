# Yaris Doctor Hub

A small home-server companion for **[Yaris Doctor](https://github.com/alexscemo/yaris-doctor)**, the Android app that
logs a Toyota Yaris diesel's DPF soot load, temperature and regenerations from an ELM327 Bluetooth dongle.

Every phone running Yaris Doctor uploads its log to the hub when it joins your home Wi-Fi. The hub keeps one merged
database for the car — so if two people drive it, their trips add up — and serves a dashboard:

- current soot load, distance since the last regen, average distance between regens
- a "Regenerating · keep driving" banner while a regen is live
- soot over the last 30 days and distance between regens (hover or tap to read values)
- regen history with who was driving, and one card per phone

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

- Readings and regens are stored per phone. Re-uploads are ignored (readings) or replace the row (regens, which gain
  an end time later), so a phone can always resend safely.
- A regen seen by two phones (one hands the dongle to the other mid-burn) counts once.
- Distance between regens is the car's distance: the sum of every phone's GPS distance in that window.
- If a phone is killed mid-regen and never records the end, the hub ends that regen at its last reading.
- **Live data:** the phones also upload every raw engine reply ("frames") and their PID table, so the **Live data** view
  shows each of the ~100 engine values decoded the same way as the app, with a 30-minute chart per value. The hub keeps
  every frame it receives (the phones thin theirs after 30 days).
- **Health:** the phones run the health checks and upload what they find. The Overview's status card shows "All good"
  or the open warnings, and what was seen in the last 7 days. The hub repeats the phones' results and checks nothing itself.

Each phone keeps its full log too, so losing the hub loses nothing. Phones also download the other phones' logs
from the hub while at home, so each phone's own screen shows the whole car.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/sync` | Phone upload: `{device_id, name, samples: [[rowid, t, soot, temp, regen, odo_m]…], regens: [[id, start_t, end_t, odo_start, odo_end, soot_start, soot_end]…]}`, optionally `frames: [[rowid, t, lid, hex]…]`, `alerts: [[id, key, level, text, start_t, end_t]…]` and `defs` (the PID table: `[[lid, bit_start, bit_end, len, signed, mul, div, offset, decimals, name, unit, category, enum]…]`). The reply counts what was stored; its `frames` key tells the phone this hub stores frames |
| `GET` | `/api/export?device=<id>&after=<cursor>` | What every *other* phone logged, so each phone shows the whole car: `{samples: [[device, phone_rowid, t, soot, temp, regen, odo_m]…], regens: [[device, id, start_t, end_t, soot_start, soot_end]…], next, more}`. Samples come 5000 at a time after `cursor` (start at 0, then pass `next` while `more`); regens are always the full list, stale open ones closed |
| `GET` | `/api/summary` | Everything the Overview shows, as JSON, including `alerts` (open and last 7 days, one per check) |
| `GET` | `/api/live` | Latest decoded value of every engine PID: `{now, values: [{id, name, unit, category, enum, decimals, t, value}…]}` |
| `GET` | `/api/series?id=<lid>.<bit>&span=<ms>` | One PID over the last `span` ms it was read (max 7 days): `{points: [[t, value]…]}` |
| `GET` | `/health` | `{"ok": true}` |

Settings via environment: `DB_PATH` (default `/data/hub.db`), `PORT` (default `8765`).

## Develop

Python standard library only, no dependencies.

```sh
cd app
python -m unittest -v test_app.py   # tests (in-memory database)
DB_PATH=dev.db python app.py         # run locally on :8765
```

Pushing to `main` publishes `:latest`; a `vX.Y.Z` tag publishes `:X.Y.Z`.

## License

[MIT](LICENSE)
