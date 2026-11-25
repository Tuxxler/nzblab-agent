# NASlab Agent

**English** · [Deutsch](README.de.md)

## Overview

**What it is.** A small, free and open-source service for your NAS that measures around the clock and feeds the NASlab Android app: temperatures of all sensors, CPU load (total and per core), GPU (graphics and media engine; the media engine transcodes video for Plex), memory, disk activity (when a disk spins up after a pause) and the top CPU processes. 8 days at full resolution (every 30 s), 2 years of hourly values. Optional parts: SMART, logs, backup status, service discovery ("everything connected in one minute"), file index, duplicate finder. The agent is optional; the app also works without it.

**What it reads.** Host data through read-only mounts (`/proc`, `/sys`, your shares, optionally logs and SMART). It writes only to its own `/config` folder (history and token). It never changes anything on the NAS. Reading service API keys for auto-connect is on by default and can be turned off with `DISCOVER_KEYS=0`.

**Getting the app.** The NASlab Android app is coming to the Google Play Store in the next few days. Until then it is in a closed test: see <https://tuxxler.github.io/nzblab/en/#play>.

**Install.** Image: `ghcr.io/tuxxler/nzblab-agent:latest` (amd64 and arm64). The image, container and folder names stay `nzblab-agent`.
- Unraid: Docker › "Template repositories" › `https://github.com/Tuxxler/unraid-templates` › Save, then Docker › Add Container › template "NASlab-Agent" (the template is also in `unraid/nzblab-agent.xml`). Once it is approved in Community Apps, you can also install it via Apps › search "NASlab-Agent".
- TrueNAS SCALE 24.10+: `compose/truenas.yml` (Install via YAML). The agent runs there; the NASlab app talks to the TrueNAS API from TrueNAS 25.04. Synology DSM 7.2+: `compose/synology.yml` (Container Manager project). Linux or any Docker host: `compose/linux.yml` or `docker-compose.yml`, then `docker compose up -d`.
- Then enter `https://<NAS-IP>:8099` and the token in the app (Settings › Server connections › NASlab agent). From 1.20.0 the same port speaks HTTPS (self-generated certificate) and, during a transition period, plain HTTP; the app (2.77+) checks the certificate itself with a token-bound proof; the SHA-256 fingerprint in the container log is there to compare if you like.

**Security notes.**
- Read-only: all host paths are mounted read-only; only `/config` is writable.
- Token: a random token is created on first start (`/config/token.txt`, mode 600) and printed to the container log exactly once. All endpoints except ping/health/hello need `Authorization: Bearer <token>`. Wrong tokens are rate-limited.
- HTTPS (from 1.20.0): on first start the agent creates its own certificate in `/config/tls` (EC P-256, 10 years) and logs its SHA-256 fingerprint. Own certificate: put `cert.pem` and `key.pem` into `/config/tls`. `NZBLAB_HTTP=0` switches plain HTTP off. Service API keys (`/api/discover?keys=1`) and token rotation (`POST /api/token/rotate`) are only served over HTTPS; keys only in a 30-minute window after start, until keys were handed out once (from 1.20.1; afterwards only 10 minutes after creating an empty file `keys-unlock` in `/config`); a token alone never unlocks.
- Port 8099 is for your home network or VPN only; never expose it to the internet.
- Licence: GNU AGPL-3.0-only, see [`LICENSE`](LICENSE). Free to use, also commercially; if you distribute a modified version or offer it as a network service, you must publish your changes under the same licence.
- Beta status: TrueNAS, Synology and Linux support is beta (tested with virtual machines and sample data, not on real hardware).

**What leaves your server.** Nothing from your NAS goes to the developer. The agent itself contacts only these addresses:
- `1.1.1.1` port 443: a plain TCP connection once a minute to measure internet latency; no data is sent (`NZBLAB_NET=0` turns it off).
- Disk price sites (Toppreise.ch, Geizhals.de/.at, diskprices.com): only when a price country is set; the search terms are your own disk models (`PRICE_SOURCES_OFF` turns sources off).
- `speed.cloudflare.com`: only when you start a speed test in the app.
- Your ntfy server: only if you set `NTFY_URL`.

Each of them sees the IP address of your connection. The NASlab app has its own list of addresses (in the app: Trust screen, and the privacy policy on the website).

**Uninstall.** Stop and remove the container (Unraid: Docker › container › Remove; others: `docker compose down`). Then delete the `/config` folder (for example `appdata/nzblab-agent`): it holds the history, the file index and the token. The agent changes nothing else on the NAS. In the app, remove the NASlab-Agent connection under Settings › Server connections.

---

The container, image (`ghcr.io/tuxxler/nzblab-agent`), folders and paths are still called `nzblab-agent`; the log line with the token still reads "NZBLab-Agent Token: …".

## Install (for everyone, from agent 1.10.0)

Ready-made image: **`ghcr.io/tuxxler/nzblab-agent:latest`** (amd64 and arm64, built by `.github/workflows/agent-image.yml`,
based on `python:3.12-alpine3.22`, Python standard library only). Free, source code publicly viewable:
<https://github.com/Tuxxler/nzblab-agent> (see Licence below).
The same instructions are in the app (Settings › Server connections › NASlab agent › "Install agent") and on the
website <https://tuxxler.github.io/nzblab/agent.html>.

| System | File | In short |
|---|---|---|
| Unraid | `unraid/nzblab-agent.xml` | Docker › "Template repositories" › `https://github.com/Tuxxler/unraid-templates` › Save, then Docker › Add Container › template "NASlab-Agent" |
| TrueNAS SCALE (24.10+) | `compose/truenas.yml` | Apps › Discover Apps › ⋮ › Install via YAML; `tank` = your own pool. The agent runs from 24.10; the app only talks to the TrueNAS API from 25.04 |
| Synology DSM 7.2+ | `compose/synology.yml` | Create the folder `docker/nzblab-agent`; Container Manager › Project › Create |
| Linux (Debian, Ubuntu …, from 1.14.0) | `compose/linux.yml` | Create a folder, save the file as `docker-compose.yml`, `docker compose up -d`; see below |
| Docker in general / everything in one file (from 1.15.0) | `docker-compose.yml` | Uncomment blocks A–D for your server, optional blocks S (SMART), L (logs), B2 (backups); `docker compose up -d` |

**Token:** On first start the agent creates a random token (`/config/token.txt`, mode 600) and writes it to the container
log **exactly once** (the log text itself is in German):

```
================================================================
NZBLab-Agent Token: …
In der App eintragen: Einstellungen › Server-Verbindungen › NZBLab-Agent.
================================================================
```

After that it only exists in the file. Anyone who can read the container log is an admin of the NAS anyway. There is deliberately **no**
setup page that shows the token in the browser: otherwise every device in the home network could fetch it. New token: delete
`token.txt` and restart the container. Own token: variable `NZBLAB_AGENT_TOKEN` (then nothing is logged; it takes precedence): **at least 32 random characters without
spaces, better leave it empty** (from 1.16.1; shorter values or values with fewer than 8 distinct characters, e.g.
`aaaa…`, are rejected by the agent with `503`/`token_error`).

**The `/config` path is mandatory:** the agent stores its token and history there. Without a path of its own, both live only inside the
container and are gone after every update: you get a new token each time and the app can no longer connect. Unraid:
Docker › NASlab-Agent › Edit › path `/config` → `/mnt/user/appdata/nzblab-agent` (read/write) › Apply.
Compose: `- ./nzblab-agent:/config` (or the path in the templates).

From 1.13.0 the agent guards against this:

- A new token is created **only** if `token.txt` is missing. If the file is empty, damaged, too short (under 32 characters, from 1.16.1), a symlink (never read from 1.16.1 on, thanks to `O_NOFOLLOW`) or unreadable (permissions),
  it never overwrites it: an error is logged, and all requests with a token answer `503` with the exact reason
  (`{"error": …, "token_error": …}`). Fix: write the old token into the file, delete the file (then a new token is created) or
  set `NZBLAB_AGENT_TOKEN`.
- At start it checks in `/proc/self/mountinfo` whether `/config` (or a folder above it, except `/`) is a mount of its own.
  Not persistent are: only the container's own filesystem, tmpfs, an anonymous Docker volume (`VOLUME /config` in the image
  without a path of your own, replaced when the container is recreated) and a read-only mount. In that case: a loud warning with the fix in the log
  (German and English), `config_persistent: false` in `/api/ping` and `/api/info` (plus `warnings` there) and a notice
  on the browser page.
- If `settings.json` is damaged (not valid JSON), it moves it aside before the next save as
  `settings.json.broken-<time>` and reports this in the log instead of silently overwriting it.

**Without root:** The image sets `PUID=8099`, `PGID=100` (its own, otherwise unused user number: other containers run as 99 on Unraid, 568 on TrueNAS or your own number on Synology, and a process with the same user and group number could reach their files through `/host/proc`). The agent starts as root, creates the token, reads the
container names (`/host/docker/containers`, readable by root only), gives `/config` to this user and then continues without root.
If it cannot hand over `/config` (e.g. NFS with root_squash), it stays root and says so in the log. `PUID=0` = always root:
needed only for appdata sizes of other containers with strict permissions. TrueNAS template: 8099:568;
Synology template: 8099:100 (its own number; only if DSM permissions deny access to the folders, enter the number of your DSM user, SSH `id`). **`PUID=0` (root) is discouraged**, only if DSM ACLs would otherwise block the file index.

**Hardening in all templates:** `--read-only` (root filesystem), `/tmp` as tmpfs, `no-new-privileges`, 512 MB RAM, 1 CPU,
no `privileged`, `cap_drop: ALL` and only five capabilities for the start (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETUID`, `SETGID`; after that the agent runs without root). HEALTHCHECK in the image: the agent's `--healthcheck`, which checks `/api/health`.

More variables for other systems: `FILES_EXCLUDE` (top-level folders, also patterns like `@*`), `FILES_SKIP` (at every level,
default `@eaDir,#recycle,#snapshot`), `CACHE_ROOTS` (empty = no cache).

Optional: push notification on overheating while you are away: variable `NTFY_URL` = `https://ntfy.sh/<long-secret-word>` and subscribe
to the same topic in the app's "ntfy". Threshold: `HEAT_ALERT_C` (default 95 °C) and `HEAT_ALERT_MINUTES` (default 5 minutes). From 1.18.0 ntfy sends
only critical things by default (see section 1.18.0, `NZBLAB_NTFY_ALL`).

## Linux (Debian, Ubuntu …) (from 1.14.0)

For Linux servers without a NAS interface of their own (Debian, Ubuntu, Raspberry Pi OS and similar). In the app the
server type is called **"Linux (Beta)"** (Settings › Server connections › NAS server). Overview, storage, temperatures and
notifications come from the agent; Docker containers are optionally controlled by the app through **Portainer** (a separate service in the app,
access token from Portainer › My account › Access tokens). The agent itself gets **no** `docker.sock`.

**Beta:** tested only with test data (replicated `/proc`, `/sys` and `os-release` in `test_agent.py`, demo agent in the app),
never on a real Debian or Ubuntu server.

Template `compose/linux.yml`:

1. Create a folder and give the folder for token and history to the agent's user:
   `mkdir -p ~/nzblab-agent/nzblab-agent-config && sudo chown 65534:65534 ~/nzblab-agent/nzblab-agent-config`,
   then save the file as `docker-compose.yml` in `~/nzblab-agent`.
2. Adjust the folders with data (see below), then `docker compose up -d`.
3. Token: `docker logs nzblab-agent 2>&1 | grep "NZBLab-Agent Token"`. In the app, add the "NASlab agent" service and
   choose the type "Linux (Beta)" for the NAS server.

**Security first: never mount the whole system (`/`).** The agent does not need it. It reads:

| Path in the container | From the host | Used for |
|---|---|---|
| `/host/proc` | `/proc` (read-only) | CPU, RAM, load, uptime, mount points (`/host/proc/1/mountinfo`, else `mounts`, else the container's own), network |
| `/host/sys` | `/sys` (read-only) | Disks (model, size, rotational, temperature via `drivetemp`/NVMe), network cards, sensors |
| `/host/os-release` | `/etc/os-release` (read-only, **one file**) | Name and version of the system |
| `/host/fs/<name>` | a folder of your choice (read-only) | **Usage** (used, free, total via `os.statvfs`) and file index |
| `/host/docker-root` | optional | Usage of the Docker data folder. Careful: `/var/lib/docker` holds the settings of all containers (including passwords); mount it only if necessary |

Usage is only available for folders under `/host/fs` (example `- /srv:/host/fs/data:ro`, more with their own names). All other
filesystems appear in the list without usage; the size comes from `/sys/class/block`. Pseudo filesystems (proc, tmpfs,
overlay, squashfs …), `/proc`, `/sys`, `/dev`, `/run`, `/boot/efi` and Docker overlays are hidden. With
`FILES_ROOT=/host/fs` the agent indexes exactly these folders (storage analysis, search, duplicates).

`network_mode: host`: this way the agent sees the server's real network cards and IP addresses and the devices in the home network.
Port 8099 then sits directly on the server: never expose it to the internet.

**User (important):** The template starts the agent directly as `65534:65534` (nobody, no login) with `cap_drop: ALL`
and `no-new-privileges`; so it never drops from root, it never has root in the first place. Do **not** start it as root and
**not** as your own user (e.g. 1000): with the server's `/proc`, a process can look into the filesystem of other processes of the **same** user
via `/proc/<pid>/root`, with that user's write permissions. As nobody this affects at most programs that run as nobody themselves. Even stricter: a number of its own that is otherwise unused
(e.g. `user: "8099:8099"`, folder `chown 8099:8099` accordingly). Folders under `/host/fs` must be
readable for this user (usually "world-readable"); usage (`statvfs`) also works without read permission.

Optionally stricter: mount `/proc` on the server with `hidepid=invisible` (formerly `hidepid=2`)
(`proc /proc proc defaults,hidepid=invisible 0 0` in `/etc/fstab`). The agent then no longer sees other users' processes:
the "top load" list stays empty, and mount points only come from the mounted folders.

If a network mount under `/host/fs` hangs (NFS/SMB gone), the agent waits at most `STATVFS_TIMEOUT` seconds (default 2)
and marks it with `timeout: true`. It recalculates `/api/host` and `/api/filesystems` at most every `HOST_CACHE_S`
seconds (default 15).

The templates for Unraid, TrueNAS and Synology use the own number 8099 (from 1.20.2; existing installations keep their saved number; whoever switches, the agent takes over its `/config` folder at the next start).

Unraid, TrueNAS and Synology therefore do not change: the new paths are only additional.

## Overview in the browser (from 1.4.0)

Open `https://<NAS-IP>:8099` (e.g. `https://192.168.1.10:8099`) in the browser (PC, tablet, phone on Wi-Fi or via
WireGuard). The browser warns once because the agent uses its own certificate: compare the fingerprint with the line in the container log. Setup (finding services) only works within 30 minutes after the first start; otherwise restart the container. Once keys were handed out, a restart no longer opens the window: create an empty file `keys-unlock` in `/config` instead. The first time, paste the token (see "Token" above); it stays stored in this browser only, "Sign out" deletes it.

Shown live, every 5 seconds: CPU and CPU temperature with history (12 h), memory, network, array with
disks, internet speed ("Measure now" button), all temperatures, top load, disk activity, GPU,
devices in the home network and file index. **Display only**: control (containers, downloads, array) is done in the app.

At the very top **"Today on the NAS"** (last 24 hours, filters Problems · Downloads · Unraid notifications · Containers · System):
Unraid notifications, finished and failed downloads (from SABnzbd's history in appdata, read-only), crashed
or stopped containers, errors in the system log (identical messages grouped) and the agent's alerts
(heat, array, new devices, disks that spun up, speed test). No passwords or API keys. This takes two additional
paths, both **read-only**: `/tmp/notifications` → `/host/notifications` and `/var/log` → `/host/log`.
If a source is missing, the page says at the bottom of the card which path is missing.

The page itself needs no token, all values do. It loads nothing from the internet (strict Content-Security-Policy).
Do not expose it to the internet; from elsewhere use WireGuard.

From 1.9.0: the token is only valid for the open browser tab (`sessionStorage`, gone after closing). The page
refreshes every 30 seconds, the CPU and temperature history shows 24 hours. New are the cards
**"Cache & Mover"** (is the mover running, usage of the shares on the cache) and **"Agent"** (version, measuring since,
number of crash reports from the app, last update). The HTML page itself contains no values from the NAS.

## API

All requests except `/api/ping`, `/api/health` (with host check from 1.19.0) and `/api/hello` (from 1.16.1 at most 30 per minute and sender) need `Authorization: Bearer <token>`. Wrong token: `401`;
from 1.15.0, after 10 wrong tokens within a minute from the same address: `429` with `Retry-After` for 5 minutes
(`NZBLAB_AUTH_FAILS`). Requests over 64 KiB: `413` (crash reports up to 128,000 bytes), `Transfer-Encoding`: `411`. If the
agent itself could not load a token (from 1.13.0, `token.txt` empty/broken/unreadable): `503` with `token_error`.

Exception without a token: `GET /api/prices/watch` (disk series only), only from private networks **and** only with an IP address or
a local name in the `Host` header (`nas`, `*.local`, `*.lan`, `*.home.arpa`, `*.internal`), so that foreign websites cannot read it via
DNS rebinding. `GET /api/crashes` (crash reports from the app) always needs the token from 1.16.1, even from the NAS
itself. The app sends the token anyway. The agent redacts new reports when saving them (credentials in URLs,
bearer tokens, cookie lines, keys and passwords, long random strings). The token is compared in constant time (`secrets.compare_digest`); `token.txt` and `settings.json` have mode 600.

| Path | Content |
|---|---|
| `GET /api/tls` | without token (host check like `/api/health`), from 1.20.0: `{"tls", "fingerprint", "algorithm": "sha256", "source": "auto"/"user", "http", "secure"}`; `/api/health` also carries the fingerprint |
| `GET /api/keys/status` | with token, from 1.20.0: `{"mode": "window"/"always"/"off", "open", "seconds_left", "tls", …}`; unlocking is only possible on the NAS (see 1.20.0) |
| `POST /api/token/rotate` | with token, HTTPS only, from 1.20.0: new token `{"ok": true, "token": "…"}`, the old one is invalid immediately; `409 token_fixed` if the token is fixed via `NZBLAB_AGENT_TOKEN` |
| `GET /api/ping` | is the agent alive? From 1.13.0 also `config_persistent` (`false` = `/config` is lost on update), from 1.15.0 `api_level` and `sampling_ok`, nothing else |
| `GET /api/hello?nonce=<32 hex characters>` | without token, from 1.11.0: the app's home-network check, see below |
| `GET /api/info` | version, hostname, measuring interval, measuring since; from 1.13.0 `config_persistent`, `token_source` (`env`, `file`, `new`) and `warnings` (`[{"code": "config_not_persistent", "de": …, "en": …}]`, with the fix); from 1.14.0 `host_os` (os-release mounted) and `host_fs` (folders under `/host/fs`); from 1.15.0 `api_level` (number, 15), `features` (enabled extras: `boots`, `disktemps`, `health`, `smart`, `backups`, `raid`, `net`, `logs`; from 1.16.0 `discover` and `discover_keys`, `api_level` 16), `last_boot`, `boots_7d`, `sampling_ok` |
| `GET /api/now` | current values (measured anew at most every 2 s) |
| `GET /api/history?hours=24` | up to 192 h all measurements, beyond that hourly values (`res=hour` forces hourly values) |
| `GET /api/today?hours=24` | "Today": events from all sources, newest first, with a summary and which sources are missing |
| `GET /api/prices` | Disk prices (from 1.5.0): per disk in the NAS the price of its own model (from 1.12.0, with the cheapest alternative), otherwise per size the best price with shop, Swiss shops, abroad, history, 90-day low/high, "cheap" |
| `GET /api/prices/watch` | without token, private networks only: which disk series the app additionally watches (for an external price list, see below) |
| `POST /api/config` | `ntfy_url`, `price_alerts` (`{"hdd28": 900}`), `price_watch` (`[{"series": "ironwolfpro", "tb": 16}]`), `price_country` (`"DE"`, from 1.10.0; with `price_country_if_unset: true` only if none is chosen yet). `price_alerts` and `price_watch` each replace the whole list; an empty value (`{}`, `[]`, `null`) deletes all price limits or watched series |
| `GET /api/wakes?hours=168` | Disks that spun up after `DISK_IDLE_MINUTES` (30) of rest, with the programs running at that moment |
| `GET /api/files/tree?path=` · `GET /api/files/largest?path=&limit=` | Storage analysis (from 1.7.0) |
| `GET /api/files/duplicates?min_mb=100&limit=100` | duplicate files (from 1.8.0), see below |
| `GET /api/inventory` | Media inventory: movies, series, gaps, new since the last index (from 1.8.0) |
| `GET /api/cache/shares` | Usage of each share on the cache (from 1.8.1; path `/mnt/cache` → `/host/mnt/cache` read-only, several pools with `CACHE_ROOTS`) |
| `GET /api/discover` | from 1.16.0: discovered services with port, SSL, URL base and API key (see "From 1.16.0"); `DISCOVER_KEYS=0` or query `?keys=0` without keys (`apiKey: null`, codes unchanged; the app only counts them this way), `Cache-Control: no-store`, at most 10 calls per minute (`429`) |
| `GET /api/appdata` | Size of each container folder under appdata (from 1.8.2; measures in the background at most every 6 h, needs `/mnt/user` → `/host/mnt/user` read-only) |
| `GET /api/wireguard` | WireGuard tunnels and devices, without keys (from 1.8.0) |
| `GET /api/host` | Linux server (from 1.14.0): `hostname`, `os` (from `/host/os-release`: `name`, `version`, `id`, `pretty`, `available`), `kernel`, `uptime_s`, `boot_time`, `cpu` (`model`, `cores`, `threads`), `load` (1/5/15 min), `mem` (bytes), `net` (interfaces with MAC, state, speed, IPv4) |
| `GET /api/filesystems` | Linux server (from 1.14.0): `filesystems` (`device`, `mount`, `fstype`, `size`, `used`/`free` only with `measured: true`, `label`), `disks` (`dev`, `model`, `size`, `rotational`, `kind`, `temp`), `mounts_from` (`host` or `container`), `measured` (names under `/host/fs`) |
| `GET /api/health` | without token, from 1.15.0: `200 {"ok": true, "sample_age_s", "interval"}`, `503` if the measuring loop has stalled for more than 2 intervals (+15 s) (Docker health check) |
| `GET /api/boots?days=30` | from 1.15.0: `boots: [{boot_time, previous_uptime_s (or null), planned: "unknown"}]`, newest first |
| `GET /api/disktemps?days=7&res=` | from 1.15.0: `devices: {sda: {model, latest, points}}`, `res` = `raw` (up to 2 days, points `[t, °C]` every 5 min), `hour` (up to 14 days) or `day` (points `[t, mean, max]`), `fields`, `interval_s` |
| `GET /api/net?days=1&res=` | from 1.15.0: `enabled`, `targets` (`gateway`, `internet`), `latest` (`{gateway: {t, rtt, loss}}`), `series` (`{gateway: {rtt: [...], loss: [...]}}`, ms and %), `res`/`fields` as above |
| `GET /api/smart` | from 1.15.0, only with `NZBLAB_SMART=1`: `enabled`, `checked`, `error`, `disks: [{device, device_name, model_name, serial_number, firmware, capacity, rotational_speed, device_protocol, smart_passed, power_on_hours, power_cycle_count, temp, reallocated, pending, uncorrectable, crc, media_errors, percentage_used, unsafe_shutdowns, standby, attrs}]` (`attrs` as in Scrutiny: by ID `"5"` or, for NVMe, by name `media_errors`, `percentage_used` …, each with `raw_value`) |
| `GET /api/smart/history?days=90` | from 1.15.0: one daily value per disk (last reading, temperature = maximum): `disks: [{serial, model, device, rows: [{day, realloc, pending, uncorr, crc, media, temp_max, hours, wear, passed}]}]`, 2 years |
| `GET /api/backups` | from 1.15.0: `backups: [{kind (restic, borg, kopia, rsync, log, unknown), name, path, last_success, last_error, error_text, snapshots, source, found_by (path, auto)}]`, `checked`, `paths`; unknown = `null` |
| `GET /api/raid` | from 1.15.0: `pools: [...]` (mdadm: `level`, `devices`, `total`, `active`, `degraded`, `sync {action, percent, finish_min}`; ZFS: `state`; Btrfs: `profiles`, `devices` with `missing` and `errors`; Unraid: pointer to `/api/array`), `degraded`, `sources {mdadm, zfs: kstat/zpool/none/unavailable, btrfs}` |
| `GET /api/logs?hours=24&max=200` | from 1.15.0, only with `NZBLAB_LOGS=1`: `lines: [{t, level (err, warn), src, text, n}]`, `files`, `redacted: true`; otherwise `403` |

## Home-network check (from 1.11.0)

Before the app sends keys over `http://` to an address in the home network, it checks that your own NAS really
answers there (a foreign Wi-Fi can use the same address range, e.g. 192.168.1.x). To do so it asks, without a token,
`GET /api/hello?nonce=<32 characters 0-9a-f>`. The agent answers with:

- `id`: a fixed random identifier, created on first start in `settings.json` (no names, no addresses)
- `proof`: HMAC-SHA256 with the token as key over `nzblab-hello:<nonce>`, as hex text
- `tls_proof` (from 1.20.0, HTTPS only): HMAC-SHA256 with the token over `nzblab-tls:<nonce>|<fingerprint>`, where the
  fingerprint is the SHA-256 of the certificate that was served ("AB:CD:…"). The app computes with the fingerprint it
  saw itself in the TLS connection; if the value matches, it pins the certificate without any typing.

From 1.19.0 without `version`. The app computes the same value with its stored token and compares in constant time. The token itself
never leaves the app or the agent. A nonce other than 32 characters `0-9a-f`: 400. A foreign name in the `Host` header: 403 (as with the
other endpoints without a token). So still do not expose the agent to the internet.

## Testing

```bash
python3 test_agent.py        # in the folder with nzblab_agent.py (in the app repo: agent/)
```

## File index (from 1.1.0)

With the path `/host/mnt/user` (read-only), the agent builds an index of all folders and files of the shares every 6 hours
(without `appdata`, `system`, `domains`, `isos`; changeable with `FILES_EXCLUDE`, interval with `FILES_EVERY_HOURS`).
The app then searches directly in it. Endpoints: `GET /api/files/status`, `GET /api/files/search?q=…`, `POST /api/files/refresh`.


## Fetching disk prices yourself (from 1.10.0)

From 1.10.0 the agent fetches the prices **itself** (Python standard library only), once a day from 06:00, for the
disk sizes in this NAS (from `/sys/block`, from 0.5 TB, HDD/SSD/NVMe separately) and the series watched in the app.
You choose the country in the app (Disk prices › Country); when setting up, the app sends the country of its language setting
but never changes one that was already chosen. Without the app: variable `PRICE_COUNTRY` (e.g. `DE`). Without a country the agent fetches nothing.

| Country | Source | Requests per day |
|---|---|---|
| CH | Toppreise.ch (search by price per TB) | one per size, plus one (rarely two) per disk model |
| DE, AT | Geizhals.de / .at, **only if their robots.txt allows it**; otherwise Amazon.de via diskprices.com | one per size, or one for all |
| US, GB, FR, IT, ES, CA, AU, SE, IE, IN | Amazon via diskprices.com (one table for all sizes) | one |
| others | nearest Amazon market (e.g. NL → Amazon.de), otherwise Amazon US | one |

Polite: it reads the site's `robots.txt` first (once per run; if it forbids the search, no request is made), honest
user agent `NZBLab-Agent/<version>`, 20 s timeout, 5 s pause between requests (`PRICE_PAUSE_S`), no cookies, no
retries. As of 28 Oct 2026, Geizhals forbids the search (`/?fs=`, `*?*sort=`) for all bots in its robots.txt,
so in practice the agent does not query Geizhals.

**Never invented numbers:** If a page is blocked (HTTP 403/429), is a check page ("Checking your connection securely"),
is forbidden by robots.txt or has an unknown layout, the size shows "Prices currently not readable" and
`browser_url`: the app then shows the button "Search in browser". Amounts are in the country's currency (`currency`);
for compatibility the field is still called `chf`.

**Sources and affiliate links (disk prices P1):** Each source is listed in `PRICE_SOURCES` (`toppreise`, `geizhals`, `diskprices`) with
name, site, `terms` (`pending` = written permission outstanding) and `amazon`. To switch off: setting `price_sources_off`
(list of IDs) or variable `PRICE_SOURCES_OFF=toppreise,geizhals`; then no fetching, `/api/prices` reports `search_only: true`
and only `search_url` per size. `/api/prices` additionally returns `sources` (id, label, url, allowed, terms, amazon), per size
`source`, `source_id`, `fetched_at`, `no_alert` and on the offer `source` and `t`. Amazon prices (diskprices) never trigger "cheap"
and never get an affiliate link. Affiliate links: `/config/affiliate.json`, e.g. `{"galaxus": {"id": "…", "program": "…"}}`;
without an ID there is no `ad_link`. The normal `link` always stays unchanged, `ad_link` is only added (https, public
targets, never Amazon). `ads_available` tells the app whether to offer the "Show affiliate links" switch.

Result per country in `/config/prices-<country>.json` (400 days of history). For Switzerland a **fresh** external `prices.json`
(at most 36 hours old, see "External price list") takes precedence; if it is older or missing,
the agent fetches itself. The tests (`test_agent.py`, class `PriceFetchTest`) check the readers with sample pages in
`test_fixtures/` (Toppreise, Geizhals and diskprices pages replicated from the known structure; block/check pages and
robots.txt real, shortened), without a network.

`GET /api/prices` also returns: `country`, `country_set`, `currency`, `countries` (choice for the app) and per size
`unreadable`, `browser_url`.

### Price of your own model (from 1.12.0)

Until 1.11.0 the price was "the cheapest disk of the same size", so often an Exos instead of the IronWolf Pro in the NAS. From
1.12.0 the agent reads the **model of each disk** from sysfs (read-only, never the serial number):

- SATA: full model name from the ATA information `device/vpd_pg89` (only the model bytes), otherwise `device/model`
  (at most 16 characters, e.g. `ST28000NT000-4AB`); NVMe: `device/model` or `/sys/class/nvme/nvmeX/model`.
  Works with `/host/sys` (container) and `/sys`.
- Part number and series via a table: Seagate `ST…NT/NE` IronWolf Pro, `VN` IronWolf, `NM` Exos, `DM` BarraCuda,
  `VX/VE` SkyHawk; WD `EFPX/EFZX/EFRX` Red Plus, `EFAX` Red (SMR), `FFBX/KFBX/KFGX` Red Pro, `PURZ` Purple,
  `WUH…/HUH…` Ultrastar; Toshiba `HDWG` N300, `HDWR` X300, `HDWT` S300, `MG…` Enterprise; SSDs by name
  (Samsung 990 PRO, WD Red SN700 …). Warranty from the table, only where documented (IronWolf Pro, Exos, Red Pro, Ultrastar,
  MG, 990 PRO 5 years; IronWolf, Red Plus, N300 3; BarraCuda 2). Sources are in the code at `MODEL_SERIES`.
- Several identical disks = one entry (`disks` lists all). Several models of the same size: the most common one keeps
  the key `hdd3`, the others are called e.g. `hdd3-wd30ezrx` (price limits from the app continue to apply).

Procedure per model: first search for the **exact part number** (Toppreise `?q=ST28000NT000`, usually jumps straight to the
product page); only offers that carry the part number in their name count (`match: exact`). If nothing is found: search by series and
size (`Seagate IronWolf Pro 28TB`), only offers of the same series (`match: series`, "same series"). Unknown models:
search for the model name itself. In addition, as before, the search by size: its cheapest offer of a
**different** model appears as `alternative`, only if it is cheaper than your own. Each search at most once
a day, same rules (robots.txt, pauses, "Prices currently not readable"). Without a readable model: as until 1.11.0.

New fields per entry in `GET /api/prices`: `model` ("Seagate IronWolf Pro 28 TB"), `part` ("ST28000NT000"), `series`,
`warranty_years`, `match` (`exact`/`series`), `match_label` ("exact"/"same series"), `alternative` (offer with `model`),
`alt_search_url`. `best`, history, "cheap" and price limits apply to your own model. Old `prices-<country>.json` files
keep working: the old history (cheapest disk) becomes `alt_history` on the first run, until then the
old price appears as an alternative. With an external `prices.json` the agent takes the price from `models_now` (history from
`model_history`, if present) and the `best` there as the alternative.

## External price list (optional)

If you prefer to collect prices yourself (e.g. with your own script), put a `prices.json` in the same format as
`/config/prices-CH.json`. The agent only reads it, never writes to it:

- Variable `PRICES_FILE` = path in the container (e.g. `/host/mnt/user/daten/nzblab-prices/prices.json`), or
- without the variable (from 1.20.4): `/config/prices.json`.
- Only with the setting `prices_scan_shares=1` (or `NZBLAB_PRICES_SCAN_SHARES=1`; default off) the agent also searches the first share under
  `/host/mnt/user` (alphabetically) that contains `nzblab-prices/prices.json` (at most every 6 hours).

Note: with the share search on, a share that contains `nzblab-prices/prices.json` is trusted for prices. Whoever can write to
that share decides which prices the app shows. Set `PRICES_FILE` to pin exactly one file.

Which series the app additionally watches is returned by `GET /api/prices/watch` (without token, private networks only).
"Cheap" means: below your own limit, or the lowest price in 90 days and at least 3% below the 30-day mean
(from 7 days of history). The agent reports this via ntfy, the app as a notification.


## Duplicate files (from 1.8.0)

`GET /api/files/duplicates?min_mb=100&limit=100`: candidates are files of exactly the same size (from `min_mb` MB, taken from the
file index). Whether they are really identical is checked by the agent with a partial checksum (SHA-1 of the first and last MiB
plus the size): so it reads at most 2 MiB per file, even for a 40 GB movie. This runs in a background thread with
low priority, at most `DUP_MAX_FILES` (2000) files per run, largest first. While it is running the result is
incomplete (`running: true`, `pending` = files not yet checked); the app simply asks again later. The checksums
stay stored in `files.db` and remain valid as long as size and modification time stay the same.
`wasted` = space that would be freed if only one file remained from each group (across all groups).

**Read-only:** The agent never deletes or moves anything. Cleaning up is up to you.

**Precomputed (from 1.13.0):** The agent computes the candidates (from 100 MB) and the media inventory in the background after every index run
(and once after start); a request only reads the finished result (`computed_at`, `building: false`).
The computation happens outside the index lock (first read rows, then process), so search and storage analysis stay fast.
If the result is still missing (e.g. a smaller tier than 100 MB or right after start), the request waits at most 2 seconds
and otherwise answers immediately with `building: true`, an empty list and the state of the index (`status`); the app then asks again.

## Media inventory (from 1.8.0)

`GET /api/inventory` calculates directly on the NAS from the file index:

- Movie folders are called `Filme`, `Movies` or `Film`, series folders `Serien`, `Series`, `TV` or `Shows` (level 1 or 2, e.g. `Media/Filme`).
  Videos: `mkv mp4 avi m4v ts iso`.
- Each subfolder is a movie or a series. Title, year and IMDb ID come from the folder name, e.g. `Babylon 5 (1994) [imdbid-tt0105946]`.
- Episodes from the file name: `S01E02`, `s1e2`, `S01E01E02`, `1x02`. `S01E001` (three or more digits) counts as a continuous
  number as with One Piece: gaps are then computed over the whole number sequence (`S01E492`, `S01E780-S01E782`). Season 0 (specials) without gaps.
  `mixed_numbering`: a series contains both.
- "New" compares the last two index runs (every 6 hours): at the end of each run the agent remembers the video files of the
  previous run (table `inv_prev`). After the first run nothing is "new" yet (`previous: 0`).

## WireGuard (from 1.8.0)

`GET /api/wireguard` shows the tunnels (`wg0` …) and per device: name, endpoint, allowed IPs, last handshake, data, online
(handshake less than 3 minutes ago). **The agent never hands out private or preshared keys**; of the public key it returns
only the first 8 characters as an identifier.

Honestly: **In the standard setup the endpoint returns nothing** (`available: false`). The app then shows the reason:

| `reason` | Meaning | Fix |
|---|---|---|
| `no_wg` | The image `python:3.12-alpine` does not contain the `wg` program | Install `wireguard-tools` in the container (e.g. `apk add --no-cache wireguard-tools` in `start.sh`) |
| `no_interfaces` | `wg` runs but sees no tunnel: in network type "Bridge" the container has its own network | Network type **Host** |
| `no_permission` | `wg show` is denied | additionally the capability `NET_ADMIN`: Extra Parameters `--cap-add=NET_ADMIN` (not "Privileged"). The template deliberately does not set this |

Peer names (e.g. "Peer 1", "Laptop") are optional: mount the path `/boot/config/wireguard` **read-only** at
`/host/boot/config/wireguard` (other location: variable `WG_CONFIG_DIR`). The agent reads only the names from it
(comment in the `[Peer]` block of `wg0.conf` or `NAME:n`/`PUBLIC:n` in `wg0.cfg`). Note: these files also contain the NAS's
private key. The agent never passes it on, but the container could read it. Without the path only the names are missing.

## Licence

Free and open source under the **GNU Affero General Public License 3.0** (`AGPL-3.0-only`), see
[`LICENSE`](LICENSE) and <https://www.gnu.org/licenses/agpl-3.0.html>. Copyright (c) 2026 Tuxxler.

- **Allowed:** using the agent free of charge, also commercially; viewing, changing and sharing the code.
- **Required:** anyone who modifies the agent and distributes the modified version or offers it as a service over a network
  must also publish the source code of their changes under the AGPL-3.0.

Full text: [`LICENSE`](LICENSE).


## Changelog

Features and fixes by agent version.

### From 1.20.4: hardening
- **Connections:** open connections are counted per sender as the rate limits do (IPv6 per /64, not per address); 8 slots are kept for 127.0.0.1/::1 (Docker health check), other senders are refused earlier.
- **Slow requests:** the request line and all headers must arrive within 15 seconds in total (not per read), otherwise the connection is closed.
- **Price file:** only `PRICES_FILE` or `/config/prices.json`; the share search needs the setting `prices_scan_shares=1`. Search links in an external price file are accepted only as `https://`.

### From 1.20.3: hardening
- **ntfy** follows at most one redirect, and only to the same host (same scheme, or http to https); anything else fails (other port, 303 is not re-sent) and the reason is logged without any address.
- **File index guard:** a run with 0 files never replaces a non-empty index (and does not count towards the streak); a run that finds under 10 % of the previous file count keeps the old index; if the 3rd run in a row is equally small (within 10 % of the previous small run), the new index is accepted (disks really removed).
- **SMART helper** only accepts read-only `-d` device types (the ones `smartctl --scan` reports, plus `marvell` and combinations such as `sat+megaraid,3`); bridge types that write (`jmb39x`, `jms56x`, `intelliprop`) and anything with `force` are always rejected.
- **Price file** (`prices.json` from a share): links only as `https://`, an `updated` time in the future makes the file invalid, the age check is 36 hours, malformed entries are ignored.

### From 1.20.2: fixes and hardening
- **Own user number 8099** in the image, the Unraid template and all compose files (instead of 99, 568 or your own DSM number), so the agent does not run under the same number as your other containers. Existing installations keep their saved number; the agent takes over its `/config` folder itself when you switch.
- **Key window.** For the discovery, the "keys handed out" marker is only set when at least one service was really handed out (not on an empty search); an existing installation still gets it at the first start after the update. The hint in the app, the browser page and the API names only the file `keys-unlock` once keys were handed out; `GET /api/info` shows `keys.issued`.
- **Fixed texts on errors.** An internal error answers `500` with a fixed text; the details go (redacted) to the container log only.
- **ntfy** does not follow redirects any more.
- **Empty fields** in the Unraid template (e.g. `PORT`, `INTERVAL`) no longer stop the agent: an empty or invalid number counts as not set.
- **`POST /api/config`** checks types strictly (price series, sizes, `price_country_if_unset`).
- **Browser page:** warning when the token is typed in over plain `http://`; the NAS name and the fingerprint wrap on narrow phones.
- **Disks stay asleep.** The `drivetemp` sensors are no longer read every 30 seconds; the disk temperature comes every 5 minutes and only for disks that had accesses since the last time (NVMe always).
- **`/api/history`:** raw values up to 48 hours, up to 8 days averages per 10 minutes (same fields, `bucket_s: 600`), above that hourly values.
- **Home-network search:** all real network interfaces (also `enp…`, `eno…`, `ovs_eth0`), but only private networks (10.x, 172.16–31.x, 192.168.x).
- **Disks:** ZFS volumes (`zd0` …) are not listed as disks; "disk spun up" only for rotating, built-in disks (also Synology `sata1` …); `rotational_speed` is passed on as `null` when the disk does not report it.
- **Disk prices:** disks under 1 TB are searched as `500GB`; adding a watched series only fetches the new series; SABnzbd history is also found under `apps/` (TrueNAS) and `docker/` (Synology).
- **File index:** a run that found (almost) no files keeps the old index. **Cache shares** are measured in the background (the request returns the last state and `measuring`).
- **NAS name** instead of the container ID, if `/etc/hostname` is mounted read-only to `/host/etc/hostname`.
- **NAS time zone** for log times, if `/etc/localtime` is mounted read-only to `/host/etc/localtime` (correct times in the logs, no `TZ` needed).
- Texts name the container "NASlab-Agent"; `HEAT_ALERT_MINUTES` is documented.

### From 1.20.0: HTTPS, key window, token rotation
Reason (full review 14 Nov 2026): The agent only spoke HTTP. Anyone listening on the same Wi-Fi read the token and fetched all service keys via `/api/discover?keys=1`.

- **HTTPS on the same port (8099).** The first byte of a connection decides (`0x16` = TLS handshake → HTTPS, otherwise plain text). Old apps (`http://…:8099`) keep working, new ones use `https://…:8099`. No second port, no second forwarding in Docker, Unraid, router.
- **Own certificate.** On first start the agent creates (with `openssl`, installed in the image) a self-signed certificate (EC P-256, 10 years, SAN: hostname, `localhost`, loopback, discovered LAN addresses and `NZBLAB_TLS_SAN`) in `/config/tls/agent-cert.pem` and `agent-key.pem` (mode 600). It is reused: the fingerprint stays the same even if the IP changes. A new one is only created if it expires in under 30 days. In a bridge network the container does not know the NAS's LAN address: enter it in `NZBLAB_TLS_SAN` (only needed for browsers; the app checks the fingerprint).
- **Use your own certificate:** put `cert.pem` and `key.pem` (PEM, key without password) into `/config/tls/`; the agent uses them and never touches them.
- **Fingerprint (SHA-256)** is shown at start in the log ("NZBLab-Agent Fingerabdruck: AB:CD:…"), in the browser dashboard (card "Agent"), in `GET /api/tls` (without token) and in `/api/health`, `/api/info`.
- **`NZBLAB_HTTP`** (default `1` in 1.20: plain text stays for old apps during the transition period). `0` = HTTPS only; plain-text connections get a short 403 response. Without a certificate (e.g. `openssl` missing) the agent then does not start.
- **Key output in the log.** The window opens at the first start until keys were handed out once (afterwards only via `keys-unlock`); every output of keys is logged with the sender address and number of services (never the keys).
- **Behind a TLS proxy.** If the proxy reports `X-Forwarded-Proto: http` (case does not matter), the request counts as insecure for keys and token rotation (403). The header is only used to forbid, never to allow.
- **Certificate pitfalls.** `NZBLAB_TLS_SAN` entries with `%` (zone identifier) or link-local addresses are skipped with a warning. If your own `cert.pem`/`key.pem` cannot be loaded, a loud warning appears in the log and the agent uses the self-generated certificate. If TLS is off, a warning line appears in the log (`tls: false`).
- **Recommendation:** `NZBLAB_HTTP=0` (HTTPS only) once all apps are on 2.77 or newer; the default stays `1` for now.
- **Keys never over HTTP.** `GET /api/discover` with keys (`keys=1`, also when not specified) answers over plain text with `403 {"error": "keys_need_tls"}`. `keys=0` (count only) stays open both ways.
- **Key window.** Keys are only available for 30 minutes after the agent starts, afterwards `403 {"error": "keys_locked", "hint": "…"}`. Reopen (10 minutes): before keys were ever handed out, restart the container (then 30 minutes again); afterwards (from 1.20.1, marker `/config/keys-issued`) only by creating the empty file `keys-unlock` in `/config` on the NAS (the agent deletes it on the next call). A token alone deliberately never unlocks: otherwise the lock would be useless against a stolen token. `DISCOVER_KEYS=0` stays "never", new `DISCOVER_KEYS=always` = without a window (old behaviour, for advanced users, still HTTPS only).
- **Token rotation:** `POST /api/token/rotate` (token required, HTTPS only) creates a new random token (`secrets.token_urlsafe(32)`), writes it atomically to `/config/token.txt` (mode 600) and answers with it; the old token is invalid immediately. Over HTTP `403 tls_required`. Additionally only while the key window is open (30 min after start or 10 min after the file `keys-unlock`), otherwise `403 keys_locked` with a hint: a stolen token cannot lock the owner out this way. Every rotation (also rejected ones) is logged with the sender address, never the token. If the token is fixed via `NZBLAB_AGENT_TOKEN`: `409 token_fixed` ("Token is fixed in the template"). At most 5 calls per minute and sender; wrong tokens count in the usual brake.
- New: `GET /api/keys/status` (with token), `features`: `tls`, `token_rotate`, `keys_window`; `api_level` 17; `/api/info` with `tls`, `fingerprint`, `http`, `secure`, `keys`.
- Image health check: `python3 nzblab_agent.py --healthcheck` (HTTPS, otherwise HTTP).

### From 1.15.0: reboots, disk temperatures, network, SMART, backups, RAID, logs

**Versions:** `/api/info` returns `api_level` (15 for 1.15.0) and `features`. The app compares `api_level` with its
minimum and shows "newer agent available". The agent **never updates itself**: new version = pull the image again
(Unraid "update ready", Compose `docker compose pull && docker compose up -d`).

Always on (read-only, nothing new to mount):

- **Reboots:** at start and every minute `btime` from `/proc/stat` and `/proc/uptime`. Newly stored if the
  uptime is smaller than last seen or the system only started after the agent's last sign of life.
  `previous_uptime_s` = last seen uptime before that (if the agent was stopped before: a minimum value). Whether a reboot
  was planned, the agent does not know (`planned: "unknown"`). 2 years.
- **Disk temperature per device:** every 5 minutes from drivetemp (SATA) or nvme-hwmon, with SMART also from disks without
  hwmon. Raw values 2 days, then hourly values (mean, low, high) for 400 days.
- **RAID/pools:** mdadm from `/proc/mdstat` (failed, resync/recovery with percent), ZFS from `/proc/spl/kstat/zfs/<pool>/state`
  (without `zpool`; if `zpool` exists in the container it is asked, otherwise `unavailable`), Btrfs from `/sys/fs/btrfs` (profiles,
  missing devices, error counters). Unraid array still via `/api/array`.
- **Network/latency** (`NZBLAB_NET=0` switches off): every minute (`NZBLAB_NET_INTERVAL`) 3 TCP connection attempts each to the router
  (the NAS's default gateway, port `NZBLAB_NET_GATEWAY_PORT` = 53; a refusal also counts as an answer) and to
  `NZBLAB_NET_TARGET` (default `1.1.1.1:443`). Stored: median in ms and loss in %. No ICMP, no privileges needed,
  no data is sent (only connect and close immediately).
- **Backups** (best effort): folders in `NZBLAB_BACKUP_PATHS` (comma-separated, paths in the container, mounted read-only) and
  a search under `/host/mnt/user` or `/host/fs` (`NZBLAB_BACKUP_AUTO=0` switches it off): top level of each share, below it
  only folders with backup-like names (backup, restic, borg, kopia, sicherung, rsync …), at most 3 levels, every 6 hours.
  restic (`config` + `snapshots/` + `data/` + `keys/`): newest file in `snapshots/`; borg (`README`): time of
  `index.N`/`hints.N`; Kopia (`kopia.repository.f`): newest index/metadata blobs. The tools are never started, encrypted
  content is never read. Log files (rsync, cron scripts): last success line (`sent … bytes received`, `total size is`,
  `backup completed` …) and last error line (filtered like the logs). Anything that cannot be determined is `null`.

Optional (own Compose block in `docker-compose.yml`):

| Block | Switch | Reads | Needs |
|---|---|---|---|
| S SMART | `NZBLAB_SMART=1` (`NZBLAB_SMART_MINUTES`, default 60) | `smartctl --scan` and `smartctl --json -a -n standby` per disk: model, serial number, power-on hours, reallocated, pending and uncorrectable sectors, CRC and media errors, wear, temperature. Never self-tests, never writes, sleeping disks stay asleep | `devices:` each disk, `cap_add: SYS_RAWIO` (NVMe additionally `SYS_ADMIN`), start as root with PUID (not `user:`) |
| L Logs | `NZBLAB_LOGS=1` | ("Today" also needs the switch, from 1.18.0, redacted) error/warning lines of the last hours from `syslog`, `messages`, `kern.log`, `daemon.log` (text files; journald binary files not) | `/var/log:/host/log:ro` |
| N Home network | `NZBLAB_LAN=1` (default off, from 1.18.0) | Device list of the home network: every 30 min an empty UDP packet to each address of the /24 network, ARP table, reverse DNS, push for new devices. Routers/IDS may report this as a port scan | `/proc:/host/proc:ro` (or host network) |
| B2 Backups | `NZBLAB_BACKUP_PATHS` | Repository folders (file names and times only) and log files | the paths `:ro` |

**SMART and permissions:** smartctl needs root and `CAP_SYS_RAWIO`. So that the agent itself still runs without privileges, it
starts (when started as root, `NZBLAB_SMART=1`) a small helper process before switching to PUID, which stays root and accepts only two commands over a
private socket pair (`--scan`, `-a -n standby` for device names like `/dev/sda`, `/dev/nvme0`).
The agent itself loses all capabilities when switching to PUID. Group IDs for `/dev` are therefore not needed. With
`user:` in Compose (single-file Linux) there is no root start: SMART then reports a permission error (Docker does not give
non-root processes `SYS_RAWIO`, not even with `group_add: [disk]` = GID 6). Image: `smartmontools` from Alpine,
about 3 MB; without `NZBLAB_SMART=1` it is never started. With the old setup (official `python:3.12-alpine`, code
mounted in) smartctl is missing: `/api/smart` reports "smartctl fehlt im Image".

**Log privacy:** Before answering, the agent removes IPv4/IPv6, MAC and email addresses, tokens, keys and
passwords (`key=…`, `password: …`, `Bearer …`, `user:pass@`), user names (`user …`, `for invalid user …`),
home folders (`/home/<user>`), file and folder names under the shares (`/mnt/user/<share>/…`) and long random strings.
Identical messages become one line with a count. The agent never sends logs anywhere by itself; only the app fetches
them with the token.

**More switches:** `NZBLAB_BIND` (default `0.0.0.0`; e.g. the LAN address or `127.0.0.1` behind a proxy, IPv6 `::`),
`NZBLAB_AUTH_FAILS` (token brake, default 10 per minute).

**Health check:** The `HEALTHCHECK` in the image asks `/api/health`: it also fails if the measuring loop hangs
(last measurement older than 2 intervals + 15 s). `/api/ping` stays cheap and does not change its behaviour.

**Tested architectures:** The image is built for amd64 and arm64 (GitHub Actions, smoke test on amd64). In continuous use:
Unraid 7.x (amd64). 1.15.0 is verified with the tests and a trial run on Unraid (amd64), not yet as an image. arm64 (Raspberry Pi 4/5 with a 64-bit system) is built but not verified on real
hardware; 32-bit ARM (armv7) is not available.

### From 1.16.0: finding services ("Everything connected in one minute")

`GET /api/discover` (token only) searches the appdata folders **read-only** for known services and reads their
API keys from the settings files. The app can then offer "7 services found, connect all?". The folder name
does not matter (`binhex-sonarr`, `sonarr`, `tv4k` …); detection is based on content and name.

**On by default.** `DISCOVER_KEYS=0` switches off reading the keys: the endpoint then only reports which services
exist (`apiKey: null`, `code: "keys_off"`), `features` contains only `discover`, not `discover_keys`.

| Service (`service`) | File | Read | Port if unspecified |
|---|---|---|---|
| `sonarr`, `radarr`, `prowlarr` | `config.xml` | `ApiKey`, `Port`, `SslPort`/`EnableSsl`, `UrlBase`; type from `<InstanceName>`, otherwise folder name, otherwise default port | 8989, 7878, 9696 |
| `sab` (SABnzbd) | `sabnzbd.ini` | `[misc]` `api_key`, `port`, `https_port`/`enable_https`, `url_base` | 8080 |
| `tautulli` | `config.ini` | `[General]` `api_key`, `http_port`, `http_root`, `enable_https` | 8181 |
| `seerr` (`variant` overseerr, jellyseerr, seerr) | `settings.json` | only `main.apiKey` | 5055 |
| `plex` | `Preferences.xml` in the folder "Plex Media Server" | **nothing**: the file is never opened (account token) → "enter the token yourself" | 32400 |
| `jellyfin` (`variant` jellyfin, emby) | `network.xml` (or folder name) | `InternalHttpPort`, `BaseUrl`, `EnableHttps`; an API key is in no file → "create it in the web interface" | 8096 |

Lidarr, Readarr, Whisparr and other services are not reported.

**Where it searches:** `DISCOVER_ROOTS` (container paths, comma-separated), otherwise `<FILES_ROOT>/appdata` (Unraid: `/mnt/user` →
`/host/mnt/user`, already in the template); if that does not exist `<FILES_ROOT>/apps` (TrueNAS, own datasets as in
`compose/truenas.yml`) and `<FILES_ROOT>/docker` (Synology `/volume1/docker`). TrueNAS catalog apps live under
`/mnt/.ix-apps/app_mounts`: if needed also mount it read-only and set `DISCOVER_ROOTS`. The agent has no
`docker.sock` and needs none.

**Limits and security:**

- at most 3 folder levels below each container folder, 3000 entries per container folder (across all levels), 500 folders
  under each root (identical folder names under a second root count only once); large data folders (cache, logs,
  backups, MediaCover, Metadata, database folders like `pgdata`, `mysql`, `model-cache` …) and hidden folders are not
  entered; Postgres/MariaDB containers are skipped entirely
- at most about 8 seconds of search time (app time limit 10 s), after that whatever was found so far is returned with `partial: true`; the
  answer without keys (`?keys=0`) is cached by the agent for 60 seconds
- from 1.16.1, `partial: true` means: time budget or folder count reached, or a folder that looks like a known service
  was cut off before its settings file was found. A cut-off foreign folder (e.g. Nextcloud
  `data`) no longer counts
- symlinks are never followed (neither folders nor files: `O_NOFOLLOW`), only regular files up to 1 MB
- XML with `DOCTYPE`/`ENTITY` is not evaluated; keys only if they look like a key (16–128 characters
  `A-Z a-z 0-9 _ - + / =`)
- the answer contains only these fields, no other file contents (passwords, Plex token, Usenet credentials stay where they are), no
  path except the folder name; `Cache-Control: no-store`
- at most 10 calls per minute (all together), then `429` with `Retry-After`; wrong tokens are still slowed down by the
  token brake
- keys never appear in the log, not even on error (`500 {"error": "Suche fehlgeschlagen"}` without details), not
  in "Today", the history or the web interface
- if the agent as PUID 8099 cannot read a folder or file: `code: "unreadable"` (from 1.16.1 also for unreadable
  folders if the folder name looks like Sonarr, Radarr, Prowlarr, SABnzbd, Tautulli, Seerr, Plex, Jellyfin or Emby), fix PUID=0
- Tautulli is only detected with a `[PMS]` section in `config.ini` or "tautulli" in the folder name (from 1.16.1; Headphones,
  LazyLibrarian, Mylar & co. also have `[General] http_root` but are not Tautulli); Emby and Jellyfin before 10.9 read the port from
  `system.xml` (`HttpServerPortNumber`)
- `?keys=` accepts `0`, `false`, `no`, `off`, `nein` (from 1.16.1, like the environment variables); `keys` in the answer shows whether
  keys are really included

**Response:**

```json
{"available": true, "keys": true, "partial": false, "at": 1762500000,
 "ports_note": "Ports stammen aus den Einstellungsdateien und gelten im Container; …",
 "services": [
  {"service": "sonarr", "folder": "binhex-sonarr", "port": 8989, "ssl": false, "urlBase": "", "apiKey": "<32 Zeichen>",
   "code": "ok", "note": "Schlüssel gefunden", "portSource": "config"},
  {"service": "radarr", "folder": "radarr", "port": 9899, "httpPort": 7878, "ssl": true, "urlBase": "/radarr",
   "apiKey": "<32 Zeichen>", "code": "ok", "note": "Schlüssel gefunden", "portSource": "config"},
  {"service": "plex", "folder": "plex", "port": 32400, "ssl": false, "urlBase": "", "apiKey": null,
   "code": "plex_token", "note": "Gefunden; Plex-Token selbst eintragen …", "portSource": "default"}]}
```

(The `ports_note` and `note` texts are returned in German by the agent.)

`code`: `ok`, `keys_off`, `plex_token`, `jellyfin_key`, `emby_key`, `unreadable`, `broken` (file broken or without a valid
key). With SSL, `port` is the HTTPS port and `httpPort` the unencrypted one. **Important:** The ports apply inside the
container. In a bridge network Docker may map them differently on the NAS (e.g. 8989 → 18989); the app has to resolve that through the
NAS's container list (port mapping). `partial: true` = a limit was reached (see above).

**Test status:** only tested with made-up test data (`test_fixtures/discover/`, unit tests), **not yet against real appdata**
and not on real Unraid.

### From 1.19.0: host check, terse token errors, brake per /64

Same interface (`api_level` 16). Changed:

- `/api/ping` and `/api/health` only answer if the Host header is an IP address, `localhost`, a name without a dot or a
  home-network name (`.local`, `.lan`, `.home.arpa`, `.internal`, `.localdomain`, `.ts.net` for Tailscale), otherwise `403` (against DNS rebinding).
  Further names (ZeroTier, WireGuard, own domain): `NZBLAB_HOSTS=nas.example.org,.vpn.example.net` (comma-separated, exact or with a
  leading dot as a suffix). `/api/ping` names
  `version` only with a valid token; `ok`, `api_level`, `config_persistent` and `sampling_ok` stay without a token.
- `503` for an unusable `token.txt`: outwardly only `{"error": …, "token_error": "<code>"}` (`short`, `simple`, `chars`, `empty`,
  `corrupt`, `unreadable`, `unwritable`). Length, path and error text appear only in the container log
  (not even for `127.0.0.1`, because a reverse proxy in host mode forwards internet traffic as 127.0.0.1).
  `/api/hello` no longer names a version. `NZBLAB_HOSTS` rejects entries that are too broad (`*`, `.com`) with a warning.
- **Token brake:** IPv6 senders are counted per /64 (token brake and `/api/hello`). The correct token is still never slowed down.
  A self-chosen token (`NZBLAB_AGENT_TOKEN`) is reported in the log at start as less secure than a random one
  (rule as before: at least 32 characters, at least 8 distinct).
- **ntfy:** Program and container names ("top load") only appear in messages to an ntfy server in the home network (private IP or
  local name) or with `NZBLAB_NTFY_ALL=1` / `NZBLAB_NTFY_NAMES=1` (own server under your own domain). An invalid
  `NTFY_URL` (required: `https://server/topic`, topic 8–64 characters) is ignored with a warning in the log, ntfy stays off.
- The Synology template no longer runs as root (see above).

### From 1.18.0: fixes from the security audit of 12 Nov 2026

Same interface (`api_level` 16), new fields are only additions (old apps do not read them). Changed:

- **The home-network search is off by default (`NZBLAB_LAN=1` switches it on).** When on, the agent sends an empty UDP packet (port 9) every 30 minutes
  to every address of every /24 network from the NAS's ARP table so that the device list fills up,
  asks reverse DNS for the names of new devices and reports new devices (name, IP, MAC) by push if an ntfy address
  is set. Routers or intrusion detection may report this as a port scan. Off: no packet, no device list. `/api/info` reports
  `lan_enabled`, `/api/devices` reports `enabled` and `reason: "off"`; `POST /api/devices/scan` answers `202` with `started: false`.
  The app then shows "LAN scan off" instead of an error.
- **"Today" (`/api/today`) only returns syslog lines with `NZBLAB_LOGS=1`**, then with the same redaction as `/api/logs`
  (IP, MAC, user, email, paths, secrets). Without the switch: no syslog content, `sources.syslog` is `false`.
- **Owner of `/config`:** The agent changes the owner recursively only if `/config` is empty or only its own entries lie at the top
  level (the marker file `.nzblab-agent-config` alone is not enough) (`token.txt`, `settings.json`, `history.db`, `files.db`,
  `devices.db`, `crashes/`, `prices-*.json`). If foreign files are in it (e.g. the whole `appdata` mounted), it changes nothing on
  them, only takes ownership of its own entries and warns loudly in the log. Use a subfolder of its own: `/mnt/user/appdata/nzblab-agent`.
- **Warning instead of silent root:** If the agent cannot give `/config` to `PUID` (NFS with root_squash, ACLs, foreign files), it continues
  as root and reports this in `/api/info` under `warnings` (`running_as_root`) and in the log.
- **Token brake:** A correct token is never slowed down, even if wrong ones came from the same address (proxy, Docker gateway).
  Wrong tokens stay blocked for 5 minutes after 10 failed attempts per minute.
- **Templates:** `/var/log` and `/var/lib/docker/containers` are no longer pre-filled (syslog only with `NZBLAB_LOGS=1`; the
  container folders contain the environment variables of all containers, the agent keeps only the names). New variable `NZBLAB_LAN`.

- **ntfy only for critical things (default).** The agent now sends via ntfy only: disk failed or new read/write errors,
  array stopped, CPU above the critical limit (`HEAT_ALERT_C`, default 95 °C, for `HEAT_ALERT_MINUTES`, default 5 minutes). Ntfy is the emergency route when
  the phone is asleep; the app reports the rest. **Info messages** (cheap disks, new devices in the home network, "back to normal") are sent
  via ntfy only with **`NZBLAB_NTFY_ALL=1`** (default `0`). In "Today" in the app they appear unchanged. The same thing
  at most once in 24 hours unless it gets worse (more errors, hotter); an all-clear lifts the block (the all-clear itself:
  only with `NZBLAB_NTFY_ALL=1`, without sound). The block is kept in memory and applies until restart. `/api/info` reports `ntfy_all`.
- **Log redaction:** User names in PAM/sshd lines ("authentication failure for bob from …", "Accepted … for bob from …",
  "user bob", `logname=`, `ruser=`, `rhost=`) are redacted; "login failed for user=bob" leaves "failed" in place.

**Exactly as documented, what leaves the server:** TCP connection to the router (port 53) and to `1.1.1.1:443` once per minute
(network measurement, `NZBLAB_NET=0` switches off); speed test to `speed.cloudflare.com` (25 MB download, 10 MB upload) only on button press;
price fetch once a day as soon as a country is set (`PRICE_COUNTRY` or by opening the price page in the app, which passes the phone's country
once), the search terms contain your own disk models; push to the ntfy address you set yourself.
Without a token only the page `/`, `/api/ping`, `/api/health`, `/api/hello` (signature, no token) and `/api/prices/watch`
(home network only) answer; everything else needs the token. Access runs unencrypted over HTTP: use it only in a trusted home network or via
VPN or a reverse proxy with TLS.

### From 1.17.0: limits, Synology disk names, mover state from the log

Same interface (`api_level` 16), three additions:

- **`limits` in `/api/now`:** limit per sensor according to hwmon (`temp*_crit`, otherwise `temp*_max`, only 40–150 °C). The app
  takes the CPU's limit from it (before the manufacturer table); if the value is missing, everything stays as before.
- **Synology DSM 7:** Disks there are called `sata1…`, `sas1…` or `usb1…` instead of `sda`. The agent now recognises these names for
  disk activity (`disks`, `disk_rw`, disk wake-ups), disk temperatures (`/api/disktemps`) and as a SMART device
  (`/dev/sata1`). Only tested with made-up test data, not yet on a real Synology.
- **`mover` in `/api/logs`** (only with `NZBLAB_LOGS=1` and `/var/log` mounted): last mover start and last end from
  syslog/messages (`{"state": "running"|"finished"|null, "started": t, "ended": t}`, start without end at most 12 hours
  old). The app reports "Mover stuck" only if the log still shows the run as open and nothing has been moved for 30 minutes;
  without a log it stays a labelled estimate. No file names, no paths.

**Test status:** Unit tests with test data (`test_agent.py`); not tested on real Unraid or Synology. Security audit and
code review of the diff since 1.16.2 on 11 Nov 2026: two small bugs fixed (limit: `max` if `crit` is unusable;
mover: a duplicate start line in syslog and messages does not reset the end).

### From 1.16.1: fixes from the security audit and code reviews

Same interface (`api_level` 16). Changed:

- **Token:** `NZBLAB_AGENT_TOKEN` and `token.txt` under 32 characters (or with whitespace/control characters) are rejected by the agent with a clear
  message in the log and `503`/`token_error` (never with the token in the log); `token.txt` is opened with `O_NOFOLLOW`.
- **`/api/crashes`:** only with a token, even from the NAS itself (audit run 2: a reverse proxy on the NAS without a
  forwarding header otherwise looked like loopback); all fields are redacted when saved, short fields truncated to 200 characters.
- **Toppreise:** `og:title` is capped at 500 characters and split linearly (no more quadratic regex).
- **Finding services:** stricter Tautulli detection, `partial` only for a truncated service folder, database folders
  skipped, `unreadable` also for unreadable folders, time budget 8 s, 60-second cache without keys, `?keys=0|false|no|off`
  with an honest `keys`, Emby/Jellyfin port from `system.xml`.

### From 1.16.1: hardening (audit run 2)

- **Headers:** All responses (JSON and error pages such as `501` for `HEAD`) with `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and
  `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` (the browser page keeps its own CSP and
  `no-cache`). Server header without the Python version.
- **Browser page:** CSP without `'unsafe-inline'` for scripts; only the SHA-256 of its own script is allowed (computed at start from
  `web.html`). Styles stay `'unsafe-inline'` (bars and colours via `style="…"`; all data goes through `esc()`).
  `X-Frame-Options: DENY`.
- **Token:** additionally at least 8 distinct characters (`aaaa…`, `1234…` are easy to guess despite 32 characters).
- **`/api/hello`:** at most 30 requests per minute and sender (then `429` with `Retry-After`; the app treats this as
  "not recognised", not as "foreign device"). The proof stays `HMAC-SHA256(token, "nzblab-hello:" + nonce)`: a slow
  derivation (PBKDF2/scrypt) would break all previous app versions (they compute exactly this value) and only helps if the
  old proof is dropped entirely. Protection against offline guessing is therefore the minimum length, the character variety and the random
  default token (192 bits).
- **Connections:** at most 32 simultaneously open connections per sender and 256 in total (`NZBLAB_MAX_CONN_PER_IP`,
  `NZBLAB_MAX_CONN`); half-finished requests (Slowloris) thus cannot occupy arbitrarily many threads. The token brake cleans up at most once
  per second.
- **Collectors:** `README` in backup folders only the first 300 bytes, broken UTF-8 no longer aborts the backup search;
  email rule when redacting is linear, log lines truncated to 4000 characters before redacting; device list: at most 64 new
  devices per scan (the first scan adopts all, without a notification), 5 individual notifications plus one summary notification, at most 2000 devices, names printable only (253 characters);
  syslog lines for "Today" are redacted (key=value, Bearer, Cookie …); cookie lines are redacted entirely.
- **/config:** new files are readable only by the agent (`umask 077`: file index, history, crash reports).
  Existing files keep their permissions.
- **Container:** templates (Compose, TrueNAS, Synology, Unraid) with `cap_drop: ALL` and only `CHOWN`, `DAC_OVERRIDE`, `FOWNER`,
  `SETUID`, `SETGID` (start as root for `/config`, then switch to PUID without privileges), plus as before `read_only`,
  `no-new-privileges`. SMART additionally needs `SYS_RAWIO`. No `USER` in the Dockerfile: existing installations still own
  `/config` as root, and the SMART helper needs root. Base image pinned by digest.
- **Workflow:** permissions only in the job (`contents: read`, `packages: write`), actions pinned to commit SHA, checkout without stored
  credentials, no `pull_request_target`; the image is first built locally and tested with the templates' permissions,
  only then are the version tag and `latest` uploaded. The token is never printed in the log.

### From 1.16.1: remaining audit items

- **WireGuard:** configuration read at most 256 KiB per file; comments or `NAME` fields that look like a key
  ("Private Key", "Preshared", "PSK", long Base64 sequence; "Mein privater Laptop" stays a name) never become peer names; errors while parsing report only `reason: "error"`
  without text. Private and preshared keys still never leave the agent.
- **Duplicate files:** partial checksums only for real files within the shares (`O_NOFOLLOW`, no symlink, no pipe,
  which `open()` would otherwise block on forever); such entries count as unreadable.
- **Triggers:** speed test and test push at most one at a time (otherwise `409` "already running"); device search and
  index rebuild check and set "running" in one step, a second trigger answers `202` with `started: false`.
- **Prices:** only `https` to public addresses, also on redirects (at most 5; never into the home network, loopback,
  link-local, CGNAT, not even via NAT64 or IPv4 in IPv6; DNS error = blocked); page at most 4 MB in 60 seconds; links from foreign pages only `https://`.

**Existing installations:** Nothing needs to be changed. Anyone who set a custom token with fewer than 8 distinct characters
gets `503` with a reason and has to replace it (or leave it empty). Anyone who queried `/api/crashes` by hand without a token:
`curl -H "Authorization: Bearer $(cat /config/token.txt)" http://127.0.0.1:8099/api/crashes`. The new
container permissions apply only to new templates; your own Compose files and saved Unraid templates stay as they are
(adopt voluntarily: the `cap_drop`/`cap_add` lines or the "Extra Parameters" from the new template).
