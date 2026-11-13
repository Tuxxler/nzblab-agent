#!/usr/bin/env python3
"""
NZBLab-Agent: kleiner Dienst auf dem NAS, mit dem die NZBLab-App spricht.

Misst rund um die Uhr (alle 30 Sekunden), auch wenn das Handy schläft:
  - CPU-Last gesamt und pro Kern, Temperaturen aller Sensoren (CPU, NVMe, Platten, Mainboard)
  - Grafikchip: Auslastung der Grafik- und der Medien-Einheit (die Medien-Einheit rechnet Videos für Plex um), Takt
  - Arbeitsspeicher, Plattenzugriffe pro Platte (erkennt, wann eine Platte nach einer Pause wieder anläuft)
  - welche Programme gerade die meiste CPU brauchen
Speichert 8 Tage in voller Auflösung und 2 Jahre als Stundenwerte (SQLite in /config).
Optional: Push-Meldung über ntfy, wenn die CPU zu lange zu heiss ist.

Nur lesen: der Agent ändert nichts am NAS. Nur Python-Standardbibliothek.
Konfiguration über Umgebungsvariablen (siehe README.md). Anfragen brauchen den Token (Header «Authorization: Bearer …»).
"""
import base64
import calendar
import fnmatch
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.19.1"

# Host-Ordner (Vorlage: /host/sys und /host/proc, nur lesen). Fehlen sie, die eigenen des Containers: Temperaturen,
# Grafikchip, CPU, RAM und Platten sind dort auch sichtbar, nur die Programme des NAS (Liste «meiste Last») nicht.
HOST_SYS = os.environ.get("HOST_SYS") or ("/host/sys" if os.path.isdir("/host/sys/class") else "/sys")
HOST_PROC = os.environ.get("HOST_PROC") or ("/host/proc" if os.path.exists("/host/proc/stat") else "/proc")
CONFIG = os.environ.get("CONFIG_DIR", "/config")
PORT = int(os.environ.get("PORT", "8099"))
INTERVAL = int(os.environ.get("INTERVAL", "30"))
WEB_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html")
PRICES_FILE = os.environ.get("PRICES_FILE", "")  # leer = erste <Freigabe>/nzblab-prices/prices.json unter FILES_ROOT (optional)
NOTIFY_DIR = os.environ.get("NOTIFY_DIR", "/host/notifications")   # Unraid: /tmp/notifications
LOG_DIR = os.environ.get("LOG_DIR", "/host/log")                    # Unraid: /var/log
HEAT_ALERT = float(os.environ.get("HEAT_ALERT_C", "95"))
HEAT_MINUTES = int(os.environ.get("HEAT_ALERT_MINUTES", "5"))
NTFY_URL = os.environ.get("NTFY_URL", "").strip()          # z. B. https://ntfy.sh/mein-geheimes-thema
IDLE_MIN = int(os.environ.get("DISK_IDLE_MINUTES", "30"))  # so lange ohne Zugriff gilt eine Platte als ruhend
# Datei-Index: Freigaben des NAS (Vorlage: /mnt/user nur lesen nach /host/mnt/user), ohne Systemordner
FILES_ROOT = os.environ.get("FILES_ROOT", "/host/mnt/user")
FILES_EXCLUDE = {x.strip() for x in os.environ.get("FILES_EXCLUDE", "appdata,system,domains,isos").split(",") if x.strip()}


def top_excluded(name, exclude):
    """Freigabe ganz oben auslassen? Genaue Namen oder Muster mit * (Synology: «@*» für @appstore, @docker …)."""
    return name in exclude or any(("*" in x or "?" in x) and fnmatch.fnmatchcase(name, x) for x in exclude)


# In jeder Ebene auslassen: Vorschaubilder und Papierkörbe von Synology (unter Unraid gibt es sie nicht)
FILES_SKIP = {x.strip() for x in os.environ.get("FILES_SKIP", "@eaDir,#recycle,#snapshot").split(",") if x.strip()}
# Was liegt auf dem Cache, pro Freigabe (ab 1.8.1): Pool nur lesend eingebunden (Vorlage: /mnt/cache → /host/mnt/cache).
# Mehrere Pools mit Komma, z. B. «/host/mnt/cache,/host/mnt/cache2».
CACHE_ROOTS = [x.strip() for x in os.environ.get("CACHE_ROOTS", "/host/mnt/cache").split(",") if x.strip()]


class CacheShares:
    """Grösse jeder Freigabe auf den Cache-Pools (Ordner der obersten Ebene = Freigabe). Für die Mover-Kachel der App:
    «wartet auf den Mover» und «bleibt auf dem Cache» pro Freigabe. Nur lesen; Ergebnis 5 Minuten zwischengespeichert,
    höchstens 90 Sekunden Rechenzeit (bei sehr vielen Dateien gilt der Teilstand mit «partial»)."""

    def __init__(self, roots=None, ttl=300, budget=90):
        self.roots, self.ttl, self.budget = roots if roots is not None else CACHE_ROOTS, ttl, budget
        self.lock = threading.Lock()
        self.cached = None

    def available(self):
        return [r for r in self.roots if os.path.isdir(r)]

    def compute(self):
        roots = self.available()
        out, partial, started = {}, False, time.time()
        for root in roots:
            pool = os.path.basename(root.rstrip("/")) or root
            try:
                tops = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for top in tops:
                size, files, stack = 0, 0, [top.path]
                while stack:
                    if time.time() - started > self.budget:
                        partial = True
                        break
                    d = stack.pop()
                    try:
                        for e in os.scandir(d):
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    stack.append(e.path)
                                elif e.is_file(follow_symlinks=False):
                                    # Belegter Platz (Blöcke), damit dünn belegte VM-Festplatten nicht übertrieben zählen
                                    st = e.stat(follow_symlinks=False)
                                    size += getattr(st, "st_blocks", 0) * 512 or st.st_size
                                    files += 1
                            except OSError:
                                continue
                    except OSError:
                        continue
                acc = out.setdefault(top.name, {"name": top.name, "bytes": 0, "files": 0, "pools": []})
                acc["bytes"] += size
                acc["files"] += files
                if pool not in acc["pools"]:
                    acc["pools"].append(pool)
        return {"available": bool(roots), "roots": roots, "partial": partial, "at": int(time.time()),
                "shares": sorted(out.values(), key=lambda x: -x["bytes"])}

    def get(self):
        with self.lock:
            if self.cached and time.time() - self.cached["at"] < self.ttl:
                return self.cached
            self.cached = self.compute()
            return self.cached

class AppdataSizes:
    """Grösse jedes Container-Ordners unter appdata (ab 1.8.2, für die App-Seite «appdata pro Container»).
    Nur lesen. Misst im Hintergrund höchstens alle [ttl] Sekunden (Standard 6 Stunden), die Abfrage wartet nie auf
    das Messen: Sie liefert den letzten Stand und «measuring», solange eine Messung läuft. Ordnergrösse = belegte Blöcke."""

    def __init__(self, root=None, ttl=6 * 3600, budget=300):
        self.root = root if root is not None else os.path.join(FILES_ROOT, "appdata")
        self.ttl, self.budget = ttl, budget
        self.lock = threading.Lock()
        self.result = None
        self.thread = None

    def measure(self):
        r = CacheShares(roots=[self.root], ttl=0, budget=self.budget).compute()
        return {"available": True, "root": self.root, "partial": r["partial"], "at": r["at"],
                "containers": [{"name": x["name"], "bytes": x["bytes"], "files": x["files"]} for x in r["shares"]]}

    def _run(self):
        try:
            res = self.measure()
        except Exception as e:  # noqa: BLE001
            res = {"available": True, "root": self.root, "partial": True, "at": int(time.time()), "containers": [], "error": str(e)}
        with self.lock:
            self.result = res

    def get(self, wait=0.0):
        """[wait]: so viele Sekunden auf eine frisch gestartete Messung warten (Tests, kleine Ordner)."""
        if not os.path.isdir(self.root):
            return {"available": False, "root": self.root, "measuring": False, "at": 0, "containers": []}
        with self.lock:
            stale = self.result is None or time.time() - self.result["at"] >= self.ttl
            if stale and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, daemon=True, name="appdata")
                self.thread.start()
            t = self.thread
        if wait and t is not None:
            t.join(wait)
        with self.lock:
            res = dict(self.result) if self.result else {"available": True, "root": self.root, "at": 0, "containers": []}
            res["measuring"] = bool(self.thread and self.thread.is_alive())
            return res


FILES_EVERY_H = float(os.environ.get("FILES_EVERY_HOURS", "6"))
# Doppelte Dateien (ab 1.8.0): höchstens so viele Dateien pro Lauf prüfen, je Datei nur erstes und letztes MiB lesen
DUP_MAX_FILES = int(os.environ.get("DUP_MAX_FILES", "2000"))
DUP_PRE_MB = 100            # ab 1.13.0: Duplikat-Kandidaten ab dieser Grösse nach jedem Index-Lauf vorrechnen (kleinste Stufe der App)
CALC_WAIT = 2.0             # so lange wartet eine Anfrage höchstens auf ein Ergebnis, das noch gerechnet wird; danach «building»
CALC_RETRY = 300.0          # Rechnen fehlgeschlagen: frühestens nach so vielen Sekunden neu versuchen (oder beim nächsten Index-Lauf)
HASH_PART = 1024 * 1024
# Medien-Bestand (ab 1.8.0): Ordnernamen der Film- und Serien-Ordner (Ebene 1 oder 2 der Freigaben), Video-Endungen
MOVIE_ROOTS = {"filme", "movies", "film"}
SERIES_ROOTS = {"serien", "series", "tv", "shows"}
VIDEO_EXT = ("mkv", "mp4", "avi", "m4v", "ts", "iso")
VIDEO_SQL = "(" + " OR ".join(f"name LIKE '%.{e}'" for e in VIDEO_EXT) + ")"
# WireGuard-Konfiguration von Unraid (nur für Peer-Namen, optional nur lesend eingebunden)
WG_CONFIG_DIR = os.environ.get("WG_CONFIG_DIR", "/host/boot/config/wireguard")
CRASH_KEEP = 200            # Absturzberichte der App (CONFIG/crashes), höchstens so viele
CRASH_MAX_BYTES = 64000


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):       # ab 1.16.1: auch kaputtes UTF-8 (UnicodeDecodeError) ergibt den Standardwert
        return default


def read_head(path, limit=4096):
    """Ab 1.16.1: nur den Anfang einer Datei aus einer Freigabe lesen (fremde Inhalte: Grösse und Kodierung unbekannt)."""
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


# ---------------------------------------------------------------- Messen

class Sampler:
    """Liest die Werte aus /sys und /proc des Hosts. Zähler (CPU, GPU, Platten) als Differenz zum letzten Aufruf."""

    def __init__(self, sys_dir=HOST_SYS, proc_dir=HOST_PROC):
        self.sys = sys_dir
        self.proc = proc_dir
        self.last_cpu = None
        self.last_gpu = {}
        self.last_disk = {}
        self.last_proc = {}
        self.last_t = None

    # --- CPU
    def cpu(self):
        lines = (read(f"{self.proc}/stat", "") or "").splitlines()
        cur = {}
        for l in lines:
            if l.startswith("cpu"):
                p = l.split()
                vals = list(map(int, p[1:9]))
                idle = vals[3] + vals[4]
                cur[p[0]] = (sum(vals), idle)
        out = {}
        if self.last_cpu:
            for k, (tot, idle) in cur.items():
                if k in self.last_cpu:
                    dt = tot - self.last_cpu[k][0]
                    di = idle - self.last_cpu[k][1]
                    out[k] = round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0
        self.last_cpu = cur
        total = out.pop("cpu", None)
        cores = [out[k] for k in sorted(out, key=lambda x: int(x[3:]) if x[3:].isdigit() else 0)]
        return total, cores

    # --- Temperaturen aller hwmon-Sensoren
    def temps(self):
        res = {}
        base = f"{self.sys}/class/hwmon"
        for h in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            d = f"{base}/{h}"
            name = read(f"{d}/name", h)
            for f in sorted(os.listdir(d)):
                m = re.match(r"temp(\d+)_input$", f)
                if not m:
                    continue
                v = read(f"{d}/{f}")
                if v is None or not v.lstrip("-").isdigit():
                    continue
                label = read(f"{d}/temp{m.group(1)}_label", "")
                key = friendly_sensor(name, label, h)
                res[key] = max(res.get(key, -1000), int(v) / 1000.0)
        return {k: round(v, 1) for k, v in res.items() if -40 < v < 150}

    # --- Grenzwerte der Sensoren (2.63): temp*_crit (Abschaltgrenze), sonst temp*_max; je Sensorname der höchste Wert.
    # Die App zeigt daraus die CPU-Grenze «laut Server» (Startseite, Wärmekarte, Warnschwelle).
    def limits(self):
        res = {}
        base = f"{self.sys}/class/hwmon"
        for h in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            d = f"{base}/{h}"
            name = read(f"{d}/name", h)
            for f in sorted(os.listdir(d)):
                m = re.match(r"temp(\d+)_input$", f)
                if not m:
                    continue
                # crit zuerst; ist er unbrauchbar (fehlt, Unsinn, ausserhalb 40–150 °C), dann max
                v = None
                for kind in ("crit", "max"):
                    raw = read(f"{d}/temp{m.group(1)}_{kind}")
                    if raw is not None and raw.lstrip("-").isdigit() and 40 < int(raw) / 1000.0 < 150:
                        v = int(raw) / 1000.0
                        break
                if v is None:
                    continue
                label = read(f"{d}/temp{m.group(1)}_label", "")
                key = friendly_sensor(name, label, h)
                res[key] = max(res.get(key, -1000), v)
        return {k: round(v, 1) for k, v in res.items()}

    def fans(self):
        res = {}
        base = f"{self.sys}/class/hwmon"
        for h in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            d = f"{base}/{h}"
            for f in sorted(os.listdir(d)):
                if re.match(r"fan\d+_input$", f):
                    v = read(f"{d}/{f}")
                    if v and v.isdigit():
                        res[f"{read(f'{d}/name', h)} {f.split('_')[0]}"] = int(v)
        return res

    # --- Grafikchip (Intel i915: rc6 = Ruhezeit; Auslastung = 1 − Anteil Ruhe; xe: gtidle)
    def gpu(self, now):
        out = {}
        for card in sorted(os.listdir(f"{self.sys}/class/drm")) if os.path.isdir(f"{self.sys}/class/drm") else []:
            if not re.match(r"card\d+$", card):
                continue
            c = f"{self.sys}/class/drm/{card}"
            gts = []
            if os.path.isdir(f"{c}/gt"):
                gts = [(g, f"{c}/gt/{g}/rc6_residency_ms", f"{c}/gt/{g}/rps_act_freq_mhz") for g in sorted(os.listdir(f"{c}/gt"))]
            elif os.path.exists(f"{c}/power/rc6_residency_ms"):
                gts = [("gt0", f"{c}/power/rc6_residency_ms", f"{c}/gt_act_freq_mhz")]
            for tile in sorted(os.listdir(f"{c}/device")) if os.path.isdir(f"{c}/device") else []:
                if tile.startswith("tile") and os.path.isdir(f"{c}/device/{tile}"):
                    for g in sorted(os.listdir(f"{c}/device/{tile}")):
                        if g.startswith("gt"):
                            gts.append((g, f"{c}/device/{tile}/{g}/gtidle/idle_residency_ms", f"{c}/device/{tile}/{g}/freq0/act_freq"))
            for g, rc6, freq in gts:
                v = read(rc6)
                if v is None or not v.isdigit():
                    continue
                key = f"{card}/{g}"
                prev = self.last_gpu.get(key)
                self.last_gpu[key] = (now, int(v))
                # gt0 = Grafik/3D, gt1 = Medien (Video umrechnen bei Plex)
                label = "Medien" if g == "gt1" else "Grafik"
                if prev and now > prev[0]:
                    idle = (int(v) - prev[1]) / ((now - prev[0]) * 1000.0)
                    out[label] = round(max(0.0, min(100.0, 100.0 * (1 - idle))), 1)
                f = read(freq)
                if f and f.isdigit():
                    out[label + " MHz"] = int(f)
        return out

    # --- Arbeitsspeicher
    def memory(self):
        info = {}
        for l in (read(f"{self.proc}/meminfo", "") or "").splitlines():
            k, _, v = l.partition(":")
            info[k] = int(v.split()[0]) * 1024 if v.split() else 0
        tot = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        return {"total": tot, "used": max(0, tot - avail), "percent": round(100.0 * (tot - avail) / tot, 1) if tot else None}

    # --- Plattenzugriffe (Sektoren gelesen/geschrieben seit Start; Differenz in KiB/s)
    # Ganze Platten, keine Partitionen: sd*, nvme*n*, und ab 1.17.0 die Namen von Synology DSM 7 (sata1, sas1, usb1; Prüfung 10.11.2026)
    DISKSTATS_DEV = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|(?:sata|sas|usb)\d{1,3})$")

    def disks(self, now):
        out = {}
        rw = {}
        for l in (read(f"{self.proc}/diskstats", "") or "").splitlines():
            p = l.split()
            if len(p) < 10 or not self.DISKSTATS_DEV.match(p[2]):
                continue
            name, rd, wr = p[2], int(p[5]), int(p[9])
            prev = self.last_disk.get(name)
            self.last_disk[name] = (now, rd + wr, rd, wr)
            if prev and now > prev[0]:
                dt = now - prev[0]
                out[name] = round((rd + wr - prev[1]) * 512 / 1024.0 / dt, 1)
                if len(prev) > 3:
                    # Lesen und Schreiben getrennt (KiB/s), für «liest … · schreibt …» in der App
                    rw[name] = [round((rd - prev[2]) * 512 / 1024.0 / dt, 1), round((wr - prev[3]) * 512 / 1024.0 / dt, 1)]
        self.disk_rw = rw
        return out

    # --- Programme mit der meisten CPU (Prozent eines Kerns), Container-Name aus der cgroup
    def top(self, now, n=5):
        ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        cur, names = {}, {}
        # Mover von Unraid läuft (Skript «mover» oder Programm «move»): wie «mover status», nur über /proc.
        # Unraid 7.x meldet ihn in der API nicht zuverlässig (shareMoverActive bleibt «no»)
        self.mover = False
        for pid in os.listdir(self.proc) if os.path.isdir(self.proc) else []:
            if not pid.isdigit():
                continue
            st = read(f"{self.proc}/{pid}/stat")
            if not st:
                continue
            try:
                comm = st[st.index("(") + 1:st.rindex(")")]
                # Auch Varianten erkennen: Plugin «Mover Tuning» (age_mover), Mover als PHP- oder Bash-Skript (ab 1.8.1)
                if comm in ("mover", "move", "age_mover") or "mover" in comm or comm in ("bash", "sh", "php", "php-cgi"):
                    cmd = (read(f"{self.proc}/{pid}/cmdline", "") or "").replace("\0", " ")
                    is_mover = ("/usr/local/sbin/mover" in cmd or "/usr/libexec/unraid/move" in cmd or cmd.startswith("move ")
                                or cmd.startswith("mover ") or "age_mover" in cmd or "/mover.php" in cmd or cmd.rstrip().endswith("/mover"))
                    if is_mover and " status" not in cmd and "grep" not in cmd:
                        self.mover = True
                f = st[st.rindex(")") + 2:].split()
                cur[pid] = int(f[11]) + int(f[12])
                names[pid] = (comm, container_of(read(f"{self.proc}/{pid}/cgroup", "") or ""))
            except (ValueError, IndexError):
                continue
        res = {}
        if self.last_t and now > self.last_t:
            dt = now - self.last_t
            for pid, t in cur.items():
                if pid in self.last_proc:
                    pct = 100.0 * (t - self.last_proc[pid]) / ticks / dt
                    if pct > 0.5:
                        comm, ctr = names[pid]
                        key = ctr or comm
                        res[key] = res.get(key, 0.0) + pct
        self.last_proc, self.last_t = cur, now
        return [[k, round(v, 1)] for k, v in sorted(res.items(), key=lambda x: -x[1])[:n]]

    def sample(self):
        now = time.time()
        total, cores = self.cpu()
        return {
            "t": int(now), "cpu": total, "cores": cores, "temps": self.temps(), "fans": self.fans(),
            "gpu": self.gpu(now), "mem": self.memory(), "disks": self.disks(now), "top": self.top(now),
            "net": self.net(now), "mover": getattr(self, "mover", False), "disk_rw": getattr(self, "disk_rw", {}),
            "limits": self.limits(),
        }

    # Netz des NAS (nicht des Containers): /proc/1/net/dev des Hosts. Nur echte Schnittstellen, Bytes pro Sekunde
    SKIP_IF = ("lo", "docker", "veth", "br-", "shim-", "tunl", "virbr", "vnet", "bonding")

    def stacked_member(self, name):
        """Gehört die Schnittstelle zu einer Brücke oder einem Bond (Unraid: eth0 → bond0 → br0)? Dann zählt nur die
        oberste; sonst wird derselbe Verkehr mehrfach gezählt (1.18.1). Erkannt am Symlink /sys/class/net/<x>/master."""
        return os.path.exists(f"{self.sys}/class/net/{name}/master")

    def net(self, now):
        txt = read(f"{self.proc}/1/net/dev", "") if self.proc != "/proc" else read("/proc/net/dev", "")
        cur = {}
        for line in (txt or "").splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            f = rest.split()
            if len(f) < 9 or name.startswith(self.SKIP_IF) or self.stacked_member(name):
                continue
            cur[name] = (int(f[0]), int(f[8]))
        out = {}
        last, lt = getattr(self, "last_net", {}), getattr(self, "last_net_t", None)
        if last and lt:
            dt = max(now - lt, 0.001)
            for k, (rx, tx) in cur.items():
                if k in last:
                    out[k] = {"rx": round(max(rx - last[k][0], 0) / dt), "tx": round(max(tx - last[k][1], 0) / dt)}
        self.last_net, self.last_net_t = cur, now
        return out


def friendly_sensor(name, label, hw):
    """Lesbarer Sensor-Name: «coretemp» + «Package id 0» → «CPU», «Core 3» → «CPU Kern 3», drivetemp → «Platte»."""
    l = (label or "").lower()
    if name == "coretemp" or name == "k10temp":
        if "package" in l or l in ("tctl", "tdie", ""):
            return "CPU"
        m = re.search(r"core\s*(\d+)", l)
        return f"CPU Kern {m.group(1)}" if m else f"CPU {label}"
    if name == "nvme":
        return f"NVMe {hw[5:]}" + ("" if l in ("composite", "") else f" {label}")
    if name == "drivetemp":
        return f"Platte {hw[5:]}"
    if name == "acpitz":
        return "Mainboard"
    return f"{name} {label}".strip()


def container_of(cgroup):
    """Docker-Container aus /proc/<pid>/cgroup («…/docker/<id>» oder «docker-<id>.scope»), sonst None."""
    m = re.search(r"docker[-/]([0-9a-f]{12})", cgroup)
    return CONTAINER_NAMES.get(m.group(1), "Container " + m.group(1)) if m else None


CONTAINER_NAMES = {}


# ---------------------------------------------------------------- Speichern

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS raw (t INTEGER PRIMARY KEY, j TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS hourly (h INTEGER PRIMARY KEY, j TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS wakes (t INTEGER, disk TEXT, j TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS events (t INTEGER, kind TEXT, level TEXT, title TEXT, text TEXT)")
            # ab 1.15.0: Neustarts, Messreihen (Plattentemperaturen, Netz) roh + Stundenwerte, SMART-Tageswerte
            self.db.execute("CREATE TABLE IF NOT EXISTS boots (id INTEGER PRIMARY KEY AUTOINCREMENT, boot INTEGER, last_seen INTEGER, "
                            "last_up INTEGER, prev_up INTEGER)")
            self.db.execute("CREATE TABLE IF NOT EXISTS series (t INTEGER, k TEXT, v REAL, PRIMARY KEY (k, t))")
            self.db.execute("CREATE INDEX IF NOT EXISTS series_t ON series (t)")
            self.db.execute("CREATE TABLE IF NOT EXISTS series_h (h INTEGER, k TEXT, mx REAL, mn REAL, sm REAL, n INTEGER, PRIMARY KEY (k, h))")
            self.db.execute("CREATE TABLE IF NOT EXISTS smart_daily (day TEXT, disk TEXT, t INTEGER, j TEXT, PRIMARY KEY (disk, day))")
            self.db.commit()

    def add_event(self, kind, level, title, text, t=None):
        t = int(t or time.time())
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (t, kind, level, title, text))
            self.db.execute("DELETE FROM events WHERE t < ?", (t - 90 * 86400,))
            self.db.commit()

    def events(self, since):
        with self.lock:
            return [{"t": t, "kind": k, "level": l, "title": ti, "text": tx}
                    for (t, k, l, ti, tx) in self.db.execute("SELECT t, kind, level, title, text FROM events WHERE t >= ? ORDER BY t DESC", (since,))]

    def add(self, s):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO raw VALUES (?, ?)", (s["t"], json.dumps(s, separators=(",", ":"))))
            self.db.execute("DELETE FROM raw WHERE t < ?", (s["t"] - 8 * 86400,))
            self.db.commit()

    def first_t(self):
        with self.lock:
            return self.db.execute("SELECT MIN(t) FROM raw").fetchone()[0]

    def raw(self, since):
        with self.lock:
            return [json.loads(j) for (j,) in self.db.execute("SELECT j FROM raw WHERE t >= ? ORDER BY t", (since,))]

    def roll_hour(self, hour):
        """Stunde zusammenfassen: Höchst- und Mittelwert jedes Sensors, CPU- und GPU-Last, meiste Programme."""
        rows = self.raw(hour)
        rows = [r for r in rows if r["t"] < hour + 3600]
        if not rows:
            return
        agg = {"h": hour, "n": len(rows), "temps": {}, "gpu": {}, "cpu": None, "cpu_max": None, "top": {}}
        for key in ("temps", "gpu"):
            vals = {}
            for r in rows:
                for k, v in (r.get(key) or {}).items():
                    vals.setdefault(k, []).append(v)
            agg[key] = {k: [round(max(v), 1), round(sum(v) / len(v), 1)] for k, v in vals.items()}
        cpus = [r["cpu"] for r in rows if r.get("cpu") is not None]
        if cpus:
            agg["cpu"], agg["cpu_max"] = round(sum(cpus) / len(cpus), 1), max(cpus)
        for r in rows:
            for name, pct in r.get("top") or []:
                agg["top"][name] = agg["top"].get(name, 0) + pct / len(rows)
        agg["top"] = [[k, round(v, 1)] for k, v in sorted(agg["top"].items(), key=lambda x: -x[1])[:5]]
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO hourly VALUES (?, ?)", (hour, json.dumps(agg, separators=(",", ":"))))
            self.db.execute("DELETE FROM hourly WHERE h < ?", (hour - 730 * 86400,))
            self.db.commit()

    def hourly(self, since):
        with self.lock:
            return [json.loads(j) for (j,) in self.db.execute("SELECT j FROM hourly WHERE h >= ? ORDER BY h", (since,))]

    def add_wake(self, t, disk, info):
        with self.lock:
            self.db.execute("INSERT INTO wakes VALUES (?, ?, ?)", (t, disk, json.dumps(info)))
            self.db.execute("DELETE FROM wakes WHERE t < ?", (t - 365 * 86400,))
            self.db.commit()

    def wakes(self, since):
        with self.lock:
            return [dict(json.loads(j), t=t, disk=d) for (t, d, j) in self.db.execute("SELECT t, disk, j FROM wakes WHERE t >= ? ORDER BY t DESC", (since,))]

    # --- ab 1.15.0: Neustarts
    def note_boot(self, boot, uptime, now=None):
        """Neustart erkennen und speichern. Ein Neustart beendet immer auch den Agenten, darum reicht der Vergleich mit dem
        letzten gespeicherten Stand: Betriebszeit kleiner als zuletzt gesehen, oder das System ist erst nach dem letzten
        Lebenszeichen gestartet (Agent war während des Neustarts gestoppt). Kleine Sprünge von boot_time (Uhr per NTP
        gestellt) gelten als derselbe Start. True = neuer Start gespeichert."""
        now = int(now if now is not None else time.time())
        boot, uptime = int(boot), int(uptime)
        with self.lock:
            row = self.db.execute("SELECT id, boot, last_seen, last_up FROM boots ORDER BY id DESC LIMIT 1").fetchone()
            if row and not (uptime < row[3] - 5 or boot > row[2] + 60):
                self.db.execute("UPDATE boots SET boot=?, last_seen=?, last_up=? WHERE id=?", (boot, now, uptime, row[0]))
                self.db.commit()
                return False
            self.db.execute("INSERT INTO boots (boot, last_seen, last_up, prev_up) VALUES (?,?,?,?)", (boot, now, uptime, row[3] if row else None))
            self.db.execute("DELETE FROM boots WHERE boot < ?", (now - 730 * 86400,))
            self.db.commit()
            return True

    def boots(self, since):
        with self.lock:
            return [{"boot_time": b, "previous_uptime_s": p, "planned": "unknown"}
                    for (b, p) in self.db.execute("SELECT boot, prev_up FROM boots WHERE boot >= ? ORDER BY boot DESC", (since,))]

    # --- ab 1.15.0: Messreihen. Rohwerte 2 Tage, danach nur Stundenwerte (Höchst-, Tiefst- und Mittelwert) für 400 Tage
    SERIES_RAW_S = 2 * 86400
    SERIES_KEEP_S = 400 * 86400

    def add_series(self, t, vals):
        t = int(t)
        h = t // 3600 * 3600
        with self.lock:
            for k, v in vals.items():
                if v is None:
                    continue
                v = float(v)
                self.db.execute("INSERT OR REPLACE INTO series VALUES (?,?,?)", (t, k, v))
                self.db.execute("INSERT INTO series_h VALUES (?,?,?,?,?,1) ON CONFLICT(k, h) DO UPDATE SET mx=max(mx, excluded.mx), "
                                "mn=min(mn, excluded.mn), sm=sm+excluded.sm, n=n+1", (h, k, v, v, v))
            self.db.execute("DELETE FROM series WHERE t < ?", (t - self.SERIES_RAW_S,))
            self.db.execute("DELETE FROM series_h WHERE h < ?", (t - self.SERIES_KEEP_S,))
            self.db.commit()

    def series(self, prefix, since, res="raw"):
        """{Schlüssel ohne Präfix: Punkte}. raw: [t, Wert]; hour/day: [t, Mittel, Höchstwert] (Tag nach Ortszeit)."""
        like = prefix.replace("%", "").replace("_", "") + "%"
        out = {}
        with self.lock:
            if res == "raw":
                rows = self.db.execute("SELECT k, t, v FROM series WHERE k LIKE ? AND t >= ? ORDER BY k, t", (like, since))
                for k, t, v in rows:
                    out.setdefault(k[len(prefix):], []).append([t, round(v, 1)])
                return out
            if res == "day":
                off = time.localtime().tm_gmtoff
                rows = self.db.execute("SELECT k, ((h + ?) / 86400) * 86400 - ? AS d, SUM(sm) / SUM(n), MAX(mx) FROM series_h "
                                       "WHERE k LIKE ? AND h >= ? GROUP BY k, d ORDER BY k, d", (off, off, like, since))
            else:
                rows = self.db.execute("SELECT k, h, sm / n, mx FROM series_h WHERE k LIKE ? AND h >= ? ORDER BY k, h", (like, since))
            for k, t, avg, mx in rows:
                out.setdefault(k[len(prefix):], []).append([t, round(avg, 1), round(mx, 1)])
        return out

    def latest_series(self, prefix):
        like = prefix.replace("%", "").replace("_", "") + "%"
        with self.lock:
            rows = self.db.execute("SELECT k, t, v FROM series WHERE k LIKE ? AND t = (SELECT MAX(t) FROM series s2 WHERE s2.k = series.k)", (like,))
            return {k[len(prefix):]: {"t": t, "v": round(v, 1)} for k, t, v in rows}

    # --- ab 1.15.0: SMART-Tageswerte (letzter Stand des Tages, Temperatur = Höchstwert des Tages), 2 Jahre
    def add_smart_day(self, disk, row, t=None):
        t = int(t or time.time())
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        with self.lock:
            old = self.db.execute("SELECT j FROM smart_daily WHERE disk=? AND day=?", (disk, day)).fetchone()
            row = dict(row, t=t)
            if old:
                o = json.loads(old[0])
                if o.get("temp_max") is not None and (row.get("temp_max") is None or o["temp_max"] > row["temp_max"]):
                    row["temp_max"] = o["temp_max"]
            self.db.execute("INSERT OR REPLACE INTO smart_daily VALUES (?,?,?,?)", (day, disk, t, json.dumps(row, separators=(",", ":"))))
            self.db.execute("DELETE FROM smart_daily WHERE t < ?", (t - 730 * 86400,))
            self.db.commit()

    def smart_days(self, since):
        out = {}
        with self.lock:
            for day, disk, j in self.db.execute("SELECT day, disk, j FROM smart_daily WHERE t >= ? ORDER BY disk, day", (since,)):
                out.setdefault(disk, []).append(dict(json.loads(j), day=day))
        return out


# ---------------------------------------------------------------- Überwachen

class Watcher:
    """Plattenwecker und Hitze-Meldung, laufend aus den Messwerten."""

    def __init__(self, store, notify, array=None):
        self.store = store
        self.notify = notify
        self.array = array
        self.quiet_since = {}
        self.hot_since = None
        self.alerted = False
        self.gate = AlertGate()

    def feed(self, s):
        now = s["t"]
        for disk, kibs in (s.get("disks") or {}).items():
            if not disk.startswith("sd"):
                continue
            if kibs < 1:
                self.quiet_since.setdefault(disk, now)
            else:
                q = self.quiet_since.pop(disk, None)
                if q is not None and now - q >= IDLE_MIN * 60:
                    self.store.add_wake(now, disk, {"quiet_min": int((now - q) / 60), "kibs": kibs, "top": s.get("top", [])[:3]})
        if self.array:
            self.array.check()
        cpu_t = (s.get("temps") or {}).get("CPU")
        if cpu_t is not None and cpu_t >= HEAT_ALERT:
            self.hot_since = self.hot_since or now
            if not self.alerted and now - self.hot_since >= HEAT_MINUTES * 60 and self.gate.allow("heat", int(cpu_t // 5), now):
                self.alerted = True
                text = f"CPU seit {HEAT_MINUTES} Minuten über {HEAT_ALERT:.0f} °C (jetzt {cpu_t:.0f} °C)."
                if ntfy_names_allowed():      # ab 1.19.0: Programm-/Container-Namen nicht an fremde ntfy-Server (z. B. ntfy.sh)
                    who = ", ".join(n for n, _ in (s.get("top") or [])[:3]) or "unbekannt"
                    text += f" Meiste Last: {who}."
                self.notify("NAS zu heiss", text)
        else:
            if cpu_t is not None and cpu_t < HEAT_ALERT - 10:
                if self.alerted and self.gate.clear("heat"):
                    self.notify("NAS wieder normal", f"Die CPU ist auf {cpu_t:.0f} °C abgekühlt.", calm=True)
                self.hot_since, self.alerted = None, False


def _settings_raw():
    """(Einstellungen, kaputt?): kaputt = settings.json ist vorhanden, aber kein gültiges JSON-Objekt."""
    raw = read(f"{CONFIG}/settings.json", "") or ""
    if not raw:
        return {}, False
    try:
        s = json.loads(raw)
    except ValueError:
        return {}, True
    return (s, False) if isinstance(s, dict) else ({}, True)


def settings():
    """Einstellungen aus der App (config/settings.json), z. B. die ntfy-Adresse; Umgebungsvariablen gelten, wenn nichts gesetzt ist."""
    return _settings_raw()[0]


SETTINGS_LOCK = threading.Lock()


def _keep_broken_settings():
    """
    Ab 1.13.0: Ist settings.json kaputt, vor dem Überschreiben als settings.json.broken-<Zeit> aufheben und im Log melden.
    So gehen Preis-Wächter, ntfy-Adresse und Kennung nicht still verloren; die Datei lässt sich von Hand retten.
    """
    path = f"{CONFIG}/settings.json"
    keep = f"{path}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, keep)
        print(f"WARNUNG: {path} war beschädigt (kein gültiges JSON). Aufgehoben als {keep}; "
              f"die Einstellungen beginnen neu.", flush=True)
        return keep
    except OSError as e:
        print(f"WARNUNG: {path} ist beschädigt und lässt sich nicht aufheben ({e.strerror or e}).", flush=True)
        return None


def save_settings(d):
    """Mit Sperre und über eine Zwischendatei: zwei gleichzeitige Änderungen oder ein Absturz löschen nichts."""
    with SETTINGS_LOCK:
        s, broken = _settings_raw()
        if broken:
            _keep_broken_settings()
        s.update(d)
        tmp = f"{CONFIG}/settings.json.tmp"
        # 600 wie token.txt: die ntfy-Adresse darin ist ein Geheimnis
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(s, f)
        os.replace(tmp, f"{CONFIG}/settings.json")


HELLO_NONCE = re.compile(r"[0-9a-f]{32}")
AGENT_ID = {"v": None}


def agent_id():
    """
    Zufällige, feste Kennung dieses Agents (ab 1.11.0, in settings.json). Kein Name, keine Adresse, nichts Persönliches:
    die App erkennt daran nur «derselbe Agent wie gestern». Lässt sich settings.json nicht schreiben, gilt sie bis zum Neustart.
    """
    if AGENT_ID["v"]:
        return AGENT_ID["v"]
    i = settings().get("agent_id")
    if not (isinstance(i, str) and HELLO_NONCE.fullmatch(i)):
        i = secrets.token_hex(16)
        try:
            save_settings({"agent_id": i})
        except OSError:
            pass
    AGENT_ID["v"] = i
    return i


def hello_proof(tok, nonce):
    """
    Heimnetz-Prüfung der App (ab 1.11.0): Beweis, dass dieser Agent den Token kennt, ohne ihn zu verraten.
    HMAC-SHA256 mit dem Token als Schlüssel über «nzblab-hello:» + Nonce der App, als Hex-Text.
    """
    return hmac.new(tok.encode("utf-8"), ("nzblab-hello:" + nonce).encode("ascii"), hashlib.sha256).hexdigest()


NTFY_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9_-]{8,64}$")    # wie die Einstellung in der App (POST /api/config)
_NTFY_WARNED = set()


def ntfy_url():
    u = (settings().get("ntfy_url") or "").strip()
    if u:
        return u
    env = (NTFY_URL or "").strip()
    if env and not NTFY_URL_RE.match(env):
        # ab 1.19.0: dieselbe Prüfung wie bei der App-Einstellung; sonst ntfy aus (die Adresse selbst nie ins Log: das Thema ist geheim)
        if "env" not in _NTFY_WARNED:
            _NTFY_WARNED.add("env")
            print("WARNUNG: NTFY_URL ungültig (nötig: https://server/thema, Thema 8–64 Zeichen aus A-Z a-z 0-9 _ -). ntfy bleibt aus. "
                  "WARNING: NTFY_URL invalid, ntfy stays off.", flush=True)
        return ""
    return env


def ntfy_names_allowed():
    """Programm- und Container-Namen nur in ntfy-Meldungen an einen Server im Heimnetz (IP aus privatem Netz, lokaler Name),
    oder wenn NZBLAB_NTFY_ALL=1 / NZBLAB_NTFY_NAMES=1 es ausdrücklich erlauben (eigener Server unter eigener Domain)."""
    if NTFY_ALL or NTFY_NAMES:
        return bool(ntfy_url())
    m = re.match(r"^https://([^/]+)/", ntfy_url() + "/")
    if not m:
        return False
    h = m.group(1).lower().rstrip(".")
    try:
        ipaddress.ip_address(h)
        return from_private_net(h)
    except ValueError:
        # Zahlen-Namen («2915221794», «0x…») sind verkappte IP-Adressen, nie «lokal»
        if re.fullmatch(r"(0x)?[0-9a-f]+", h) and not re.fullmatch(r"[a-f]+", h):
            return False
        return local_host_header(h) and ("." not in h or h.endswith(LOCAL_SUFFIXES))


# ab 1.18.0 (Paket D): ntfy ist der Notfall-Weg (die App meldet den Rest). Standard: nur Kritisches (Array/Platte, Hitze über der
# kritischen Grenze). Info-Meldungen (günstige Platten, neue Geräte, «wieder normal») nur mit NZBLAB_NTFY_ALL=1.
NTFY_ALL = os.environ.get("NZBLAB_NTFY_ALL", "").strip().lower() in ("1", "true", "yes", "on", "ja")
# ab 1.19.0: Programm-/Container-Namen («Meiste Last») in ntfy nur zum eigenen Server im Heimnetz, oder mit NZBLAB_NTFY_ALL=1 / NZBLAB_NTFY_NAMES=1
NTFY_NAMES = os.environ.get("NZBLAB_NTFY_NAMES", "").strip().lower() in ("1", "true", "yes", "on", "ja")


class AlertGate:
    """Dieselbe Sache höchstens 1× in 24 Std., ausser sie wird schlimmer (höhere Stufe); Entwarnung (clear) hebt die Sperre auf.
    Gleiche Logik wie die App ab 2.67. Der Zustand liegt nur im Speicher: nach einem Neustart darf eine Meldung wieder kommen."""
    LOCK = 24 * 3600

    def __init__(self):
        self.sent = {}

    def allow(self, key, severity=0, now=None):
        now = time.time() if now is None else now
        last = self.sent.get(key)
        if last and now - last[0] < self.LOCK and severity <= last[1]:
            return False
        self.sent[key] = (now, severity)
        return True

    def clear(self, key):
        """Entwarnung: True, wenn vorher eine Meldung gesperrt war (dann lohnt sich eine Entwarnung)."""
        return self.sent.pop(key, None) is not None


EVENT_SINK = None   # Store.add_event, gesetzt in main()


def alerter(kind, level, critical=False):
    """Ereignis für «Heute» speichern (immer) und, wenn es kritisch ist, aufs Handy (ntfy) schicken. Nicht Kritisches und
    Entwarnungen («calm», ohne Ton) gehen nur mit NZBLAB_NTFY_ALL=1 per ntfy."""
    def send(title, text, calm=False):
        if EVENT_SINK:
            EVENT_SINK(kind, "info" if calm else level, title, text)
        if calm:
            return ntfy(title, text, tags="white_check_mark", priority="low") if NTFY_ALL else False
        if not (critical or NTFY_ALL):
            return False
        return ntfy(title, text)
    return send


def ntfy(title, text, tags="warning", priority="high"):
    url = ntfy_url()
    if not url:
        return False
    try:
        req = urllib.request.Request(url, data=text.encode(), headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                                                                    "Tags": tags, "Priority": priority})
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as e:   # auch ValueError bei einer Adresse ohne https://
        # Das Thema in der Adresse ist geheim (wer es kennt, liest mit): nie ins Log schreiben
        print("ntfy:", str(e).replace(url, "<ntfy-Adresse>"), flush=True)
        return False


class ArrayWatch:
    """Unraid-Array aus /proc/mdstat: gestoppt, Platte ausgefallen, neue Lese-/Schreibfehler → sofort Push.
    Jede Meldung höchstens alle 6 Stunden (gleicher Grund)."""

    BAD = {"DISK_DSBL", "DISK_INVALID", "DISK_WRONG", "DISK_DSBL_NEW", "DISK_NP_MISSING"}

    def __init__(self, notify, proc=HOST_PROC):
        self.notify, self.proc = notify, proc
        self.prev = None
        self.gate = AlertGate()

    def read(self):
        txt = read(f"{self.proc}/mdstat") or read("/proc/mdstat")
        if not txt or "mdState=" not in txt:
            return None
        kv = dict(l.split("=", 1) for l in txt.splitlines() if "=" in l)
        disks = []
        for k, v in kv.items():
            if k.startswith("rdevName.") and v:
                n = k.split(".", 1)[1]
                disks.append({"slot": int(n), "device": v, "name": kv.get(f"diskName.{n}", ""), "status": kv.get(f"rdevStatus.{n}", ""),
                              "errors": int(kv.get(f"rdevNumErrors.{n}", "0") or 0)})
        return {"state": kv.get("mdState", ""), "disks": sorted(disks, key=lambda d: d["slot"]),
                "resync": kv.get("mdResyncAction", "") if kv.get("mdResync", "0") not in ("0", "") else ""}

    def _send(self, key, title, text, severity=0):
        if self.gate.allow(key, severity):
            self.notify(title, text)

    def _clear(self, key, title, text):
        if self.gate.clear(key):
            self.notify(title, text, calm=True)

    @staticmethod
    def label(d):
        return ("Parität" if d["slot"] in (0, 29) else f"Platte {d['slot']}") + f" ({d['device']})"

    def check(self):
        cur = self.read()
        if cur is None:
            return None
        prev = self.prev
        if cur["state"] == "STARTED" and prev is not None and prev["state"] != "STARTED":
            self._clear("array", "Array läuft wieder", "Das Unraid-Array ist gestartet.")
        if cur["state"] != "STARTED" and (prev is None or prev["state"] == "STARTED"):
            self._send("array", "Array nicht gestartet", f"Das Unraid-Array ist {cur['state'].lower()}: Freigaben, Docker und VMs laufen nicht. In der App: Übersicht › «Array starten».")
        old = {d["slot"]: d for d in (prev or {}).get("disks", [])}
        for d in cur["disks"]:
            o = old.get(d["slot"])
            if d["status"] in self.BAD and (o is None or o["status"] not in self.BAD):
                self._send(f"bad{d['slot']}", "Festplatte ausgefallen", f"{self.label(d)} meldet {d['status']}. Unraid hat sie deaktiviert; Daten sichern und Platte prüfen.", 1)
            elif o is not None and o["status"] in self.BAD and d["status"] not in self.BAD:
                self._clear(f"bad{d['slot']}", "Festplatte wieder in Ordnung", f"{self.label(d)} meldet {d['status']}.")
            if o is not None and d["errors"] > o["errors"]:
                self._send(f"err{d['slot']}", "Neue Plattenfehler", f"{self.label(d)}: {d['errors'] - o['errors']} neue Lese-/Schreibfehler (gesamt {d['errors']}). SMART-Werte ansehen.", int(d["errors"]).bit_length())   # erneut erst, wenn sich die Fehler verdoppeln (kein Dauer-Alarm)
            elif o is not None and d["errors"] < o["errors"]:
                self.gate.clear(f"err{d['slot']}")       # Zähler zurückgesetzt: stille Entwarnung
        self.prev = cur
        return cur


# ---------------------------------------------------------------- Heimnetz

# Ab 1.18.0 (Audit M3): Heimnetz-Suche nur auf Wunsch (NZBLAB_LAN=1). Sie schickt alle 30 Minuten ein leeres UDP-Paket an jede
# Adresse des /24-Netzes, fragt per Reverse-DNS nach Namen neuer Geräte und meldet neue Geräte (Name, IP, MAC) per Push.
LAN_ON = os.environ.get("NZBLAB_LAN", "").strip().lower() in ("1", "true", "yes", "on", "ja")


class LanScan:
    """Geräte im Heimnetz: kurz jede Adresse im eigenen /24-Netz anstossen (ein UDP-Paket, das NAS fragt dann per ARP nach),
    danach die ARP-Tabelle des NAS lesen (/proc/1/net/arp des Hosts). Neue Geräte (nach der ersten Suche) → Push."""

    LAN_IF = ("br0", "eth0", "eth1", "bond0", "wlan0")
    MAX_NEW = 64            # ab 1.16.1: höchstens so viele neue Geräte je Durchgang speichern
    MAX_ALERTS = 5          # … und einzeln melden (danach eine Sammelmeldung)
    MAX_DEVICES = 2000      # … und insgesamt merken (90 Tage), /api/devices liefert höchstens so viele

    def __init__(self, db_path, notify, proc=HOST_PROC):
        self.proc, self.notify = proc, notify
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS devices (mac TEXT PRIMARY KEY, ip TEXT, host TEXT, first INTEGER, last INTEGER)")
            self.db.commit()
        self.running = False
        self._scan_guard = threading.Lock()     # ab 1.16.1 (Audit Restteile): nie zwei Suchen gleichzeitig

    def arp_path(self):
        """Geräte-Tabelle des NAS. Über /host/proc/1/net verweigert Docker den Zugriff oft (fehlende Rechte trotz Pfad);
        im Netzwerktyp «Host» ist die eigene Tabelle schon die des NAS (erkennbar an docker0)."""
        cands = [f"{self.proc}/1/net/arp"] if self.proc == "/host/proc" else []
        if "docker0" in (read("/proc/net/dev", "") or ""):
            cands.append("/proc/net/arp")
        for p in cands:
            try:
                with open(p) as f:
                    f.readline()
                return p
            except OSError:
                pass
        return None

    def available(self):
        return LAN_ON and self.arp_path() is not None

    def reason(self):
        """Warum keine Geräteliste: off = ab 1.18.0 ausgeschaltet (NZBLAB_LAN, Standard aus), no_proc = Pfad /host/proc fehlt,
        no_access = Pfad da, aber Docker sperrt die Tabelle."""
        if not LAN_ON:
            return "off"
        if self.available():
            return ""
        return "no_proc" if self.proc != "/host/proc" else "no_access"

    def arp(self):
        out = {}
        path = self.arp_path()
        for line in ((read(path, "") if path else "") or "").splitlines()[1:]:
            f = line.split()
            if len(f) >= 6 and f[2] == "0x2" and f[5] in self.LAN_IF and f[3] != "00:00:00:00:00:00":
                out[f[3].lower()] = f[0]
        return out

    def scan(self):
        # Prüfen und Setzen in einem Schritt (vorher zwei Schritte: zwei schnelle POST /api/devices/scan starteten zwei Suchen)
        if not self._scan_guard.acquire(blocking=False):
            return
        try:
            if self.running or not self.available():
                return
            self.running = True
            self._scan()
        finally:
            self._scan_guard.release()

    def _scan(self):
        try:
            known = self.arp()
            nets = {ip.rsplit(".", 1)[0] for ip in known.values() if ip.count(".") == 3}
            for net in nets:
                for i in range(1, 255):
                    try:
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
                            so.sendto(b"", (f"{net}.{i}", 9))
                    except OSError:
                        pass
            time.sleep(3)
            found = self.arp()
            now = int(time.time())
            with self.lock:
                first_scan = self.db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
                total = self.db.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
                new, skipped = [], 0
                for mac, ip in found.items():
                    row = self.db.execute("SELECT mac FROM devices WHERE mac=?", (mac,)).fetchone()
                    if row:
                        self.db.execute("UPDATE devices SET ip=?, last=? WHERE mac=?", (ip, now, mac))
                    # Erster Durchgang (Review 1.16.1): alle bis MAX_DEVICES übernehmen, sonst meldete der zweite Durchgang
                    # in Netzen mit über 64 Geräten bekannte Geräte als «neu»
                    elif (first_scan or len(new) < self.MAX_NEW) and total + len(new) < self.MAX_DEVICES:
                        self.db.execute("INSERT INTO devices VALUES (?,?,?,?,?)", (mac, ip, "", now, now))
                        new.append((mac, ip))
                    else:
                        skipped += 1            # ab 1.16.1: Flut neuer MACs (gefälschte ARP-Antworten) begrenzen
                self.db.commit()
            for n, (mac, ip) in enumerate(new):
                host = self.lookup(ip)
                if host:
                    with self.lock:
                        self.db.execute("UPDATE devices SET host=? WHERE mac=?", (host, mac))
                        self.db.commit()
                if not first_scan and n < self.MAX_ALERTS:
                    self.notify("Neues Gerät im Heimnetz", f"{host or 'ohne Namen'} · {ip} · {mac}. Kennst du es nicht: im Router nachsehen.")
            extra = (len(new) - self.MAX_ALERTS if len(new) > self.MAX_ALERTS else 0) + skipped
            if not first_scan and extra:
                self.notify("Viele neue Geräte im Heimnetz", f"{extra} weitere neue Geräte in einem Durchgang. Ungewöhnlich: im Router nachsehen.")
        finally:
            self.running = False

    @staticmethod
    def lookup(ip):
        """Name zur IP (Reverse-DNS) mit höchstens 2 Sekunden Wartezeit; gethostbyaddr kennt selbst kein Zeitlimit."""
        out = [""]
        def run():
            try:
                out[0] = socket.gethostbyaddr(ip)[0]
            except OSError:
                pass
        t = threading.Thread(target=run, daemon=True)
        t.start(); t.join(2)
        # ab 1.16.1: Namen aus dem Netz nur druckbar und höchstens 253 Zeichen (gehen in Datenbank und Push-Text)
        return "".join(c for c in out[0] if c.isprintable())[:253]

    def cleanup(self, days):
        """Geräte, die länger als [days] Tage nicht mehr gesehen wurden, vergessen (wechselnde Handy-MACs)."""
        with self.lock:
            self.db.execute("DELETE FROM devices WHERE last < ?", (int(time.time()) - days * 86400,))
            self.db.commit()

    def devices(self):
        with self.lock:
            rows = self.db.execute("SELECT mac, ip, host, first, last FROM devices ORDER BY last DESC LIMIT ?", (self.MAX_DEVICES,)).fetchall()
        return [{"mac": r[0], "ip": r[1], "host": r[2], "first": r[3], "last": r[4]} for r in rows]


def run_lan(lan, stop):
    while not stop.is_set():
        try:
            lan.scan()
            lan.cleanup(90)
        except Exception as e:   # ein Fehler darf den Scan nicht für immer stoppen
            print("Heimnetz:", e, flush=True)
        stop.wait(30 * 60)


# ---------------------------------------------------------------- Speedtest

LAST_SPEED = {}


NIC_VENDORS = {"0x8086": "Intel", "0x1d6a": "Marvell AQtion", "0x10ec": "Realtek", "0x14e4": "Broadcom", "0x15b3": "Mellanox", "0x1af4": "virtio"}
NIC_MODELS = {("0x1d6a", "0x04c0"): "Marvell AQtion AQC113", ("0x1d6a", "0x94c0"): "Marvell AQtion AQC113C", ("0x1d6a", "0x07b1"): "Marvell AQtion AQC107",
              ("0x8086", "0x125c"): "Intel I226-V", ("0x8086", "0x125b"): "Intel I226-LM", ("0x8086", "0x15f3"): "Intel I225-V",
              ("0x8086", "0x1533"): "Intel I210", ("0x10ec", "0x8125"): "Realtek RTL8125", ("0x10ec", "0x8168"): "Realtek RTL8111"}


def nic_info(sys_dir=None, proc_dir=None):
    """
    Über welche Netzwerkkarte das NAS ins Internet geht: Standard-Route → Brücke (br0) → Bond (bond0) → echte Karte mit Link.
    Ergebnis z. B. {"iface": "eth0", "via": "br0", "speed_mbit": 10000, "model": "Marvell AQtion AQC113", "driver": "atlantic"}.
    """
    sys_dir = sys_dir or HOST_SYS
    proc_dir = proc_dir or HOST_PROC
    route = read(f"{proc_dir}/1/net/route") if proc_dir != "/proc" else None
    route = route or read("/proc/net/route", "") or ""
    default = None
    for line in route.splitlines()[1:]:
        f = line.split()
        # Standard-Route; «shim-br0» ist nur Dockers Hilfs-Schnittstelle
        if len(f) > 2 and f[1] == "00000000" and not f[0].startswith(("shim-", "docker", "veth", "wg", "tun")):
            default = f[0]
            break
    if not default:
        return {}
    net = f"{sys_dir}/class/net"

    def members(i):
        out = []
        if os.path.isdir(f"{net}/{i}/brif"):
            out += sorted(os.listdir(f"{net}/{i}/brif"))
        out += (read(f"{net}/{i}/bonding/slaves", "") or "").split()
        return out

    def speed(i):
        v = read(f"{net}/{i}/speed", "")
        return int(v) if v and v.lstrip("-").isdigit() and int(v) > 0 else 0

    # Bis zur echten Karte absteigen (höchstens drei Ebenen), die mit Link und höchstem Tempo nehmen
    cur, seen = default, set()
    for _ in range(3):
        m = [x for x in members(cur) if x not in seen]
        if not m:
            break
        seen.update(m)
        cur = max(m, key=lambda x: (read(f"{net}/{x}/operstate", "") == "up", speed(x)))
    vendor, device = read(f"{net}/{cur}/device/vendor", ""), read(f"{net}/{cur}/device/device", "")
    drv = os.path.basename(os.path.realpath(f"{net}/{cur}/device/driver")) if os.path.exists(f"{net}/{cur}/device/driver") else ""
    return {"iface": cur, "via": default if default != cur else "", "speed_mbit": speed(cur) or speed(default),
            "model": NIC_MODELS.get((vendor, device)) or NIC_VENDORS.get(vendor, ""), "driver": drv}


SPEED_LOCK = threading.Lock()       # ab 1.16.1 (Audit Restteile): höchstens eine Messung gleichzeitig
NOTIFY_TEST_LOCK = threading.Lock()  # … und höchstens ein Test-Push gleichzeitig


def speedtest():
    """Internet des NAS über Cloudflare: Ping (kleinste von 3), Download 25 MB, Upload 10 MB, in Mbit/s."""
    base = "https://speed.cloudflare.com"
    # Ohne Browser-Kennung antwortet Cloudflare mit 403 (Python schickt sonst «Python-urllib»)
    ua = {"User-Agent": f"Mozilla/5.0 (X11; Linux x86_64) NZBLab-Agent/{VERSION}"}

    def get(url, timeout):
        return urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=timeout).read()

    pings = []
    for _ in range(3):
        t = time.time()
        get(f"{base}/__down?bytes=0", 10)
        pings.append((time.time() - t) * 1000)
    t = time.time()
    n = len(get(f"{base}/__down?bytes=25000000", 60))
    down = n * 8 / (time.time() - t) / 1e6
    data = os.urandom(10_000_000)
    t = time.time()
    urllib.request.urlopen(urllib.request.Request(f"{base}/__up", data=data, method="POST",
                                                  headers={**ua, "Content-Type": "application/octet-stream"}), timeout=60).read()
    up = len(data) * 8 / (time.time() - t) / 1e6
    LAST_SPEED.update({"t": int(time.time()), "ping_ms": round(min(pings)), "down_mbit": round(down, 1), "up_mbit": round(up, 1),
                       "nic": nic_info()})
    if EVENT_SINK:
        EVENT_SINK("net", "info", "Internet gemessen", f"↓ {down:.0f} Mbit/s · ↑ {up:.0f} Mbit/s · Ping {min(pings):.0f} ms")
    return dict(LAST_SPEED)


def load_container_names():
    """Namen der Container aus Unraids Docker-Vorlagen sind nicht nötig: der Hostname im Container-Ordner reicht."""
    base = os.environ.get("DOCKER_DIR", "/host/docker/containers")
    if not os.path.isdir(base):
        return
    for cid in os.listdir(base):
        cfg = read(f"{base}/{cid}/config.v2.json")
        if cfg:
            try:
                CONTAINER_NAMES[cid[:12]] = json.loads(cfg).get("Name", "").lstrip("/") or cid[:12]
            except ValueError:
                pass


def run_sampler(store, sampler, watcher, stop):
    SAMPLER_STATE["last"] = time.time()
    sampler.sample()                       # erste Messung nur für die Differenzen
    last_hour = int(time.time()) // 3600 * 3600
    while not stop.wait(INTERVAL):
        try:
            if int(time.time()) % 600 < INTERVAL:
                load_container_names()
            s = sampler.sample()
            store.add(s)
            watcher.feed(s)
            hour = s["t"] // 3600 * 3600
            if hour > last_hour:
                store.roll_hour(last_hour)
                last_hour = hour
            SAMPLER_STATE["last"] = time.time()    # ab 1.15.0: Gesundheitsprüfung (Messschleife hängt nicht)
        except Exception as e:             # eine kaputte Messung darf den Dienst nicht beenden
            print("Messung fehlgeschlagen:", e, flush=True)


# ---------------------------------------------------------------- Datei-Index

class CalcError(Exception):
    """Bestand oder Duplikat-Kandidaten liessen sich nicht berechnen (die Anfrage meldet HTTP 500 mit klarem Text)."""


class FileIndex:
    """Alle Ordner und Dateien der Freigaben in SQLite, damit die App sofort über alles suchen kann.
    Läuft auf dem NAS selbst (lokal statt über SMB, darum schnell) und hört nicht auf, wenn das Handy die App schliesst.
    Neu aufgebaut wird in eine zweite Tabelle; die Suche bleibt währenddessen nutzbar."""

    def __init__(self, path, root=FILES_ROOT, exclude=FILES_EXCLUDE):
        self.root, self.exclude = root, exclude
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS files (p TEXT PRIMARY KEY, name TEXT, dir INTEGER, size INTEGER, mtime INTEGER)")
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
            # Ab 1.8.0: Teil-Prüfsummen für doppelte Dateien (gilt nur, solange Grösse und Änderungszeit gleich sind)
            self.db.execute("CREATE TABLE IF NOT EXISTS hashes (p TEXT, size INTEGER, mtime INTEGER, h TEXT, PRIMARY KEY (p, size, mtime))")
            # Ab 1.8.0: Videodateien des vorletzten Index-Laufs, für «neu seit dem letzten Mal» im Medien-Bestand
            self.db.execute("CREATE TABLE IF NOT EXISTS inv_prev (p TEXT PRIMARY KEY)")
            self.db.commit()
        # Ordnergrössen für die Speicher-Analyse der App (ab 1.7.0); fehlt die Tabelle, baut der Indexer sofort neu
        self.has_sizes = self._has_table("dirsize")
        self.state = {"running": False, "dirs": 0, "files": 0, "error": None}
        self.hasher = None                 # Hintergrund-Thread für die Prüfsummen
        self.hash_error = None
        # Ab 1.13.0: Bestand und Duplikat-Kandidaten fertig gerechnet im Speicher (nach jedem Index-Lauf im Hintergrund).
        # Eine Anfrage rechnet nie selbst unter self.lock, sondern liefert den Stand oder «building».
        self._cache = {}                   # Name -> {"key": Stand des Index, "value": Ergebnis, "computed_at": Zeit}
        self._calc = {}                    # Name -> laufender Rechen-Thread
        self._calc_guard = threading.Lock()
        self._build_guard = threading.Lock()  # ab 1.16.1 (Audit Restteile): nie zwei Index-Läufe gleichzeitig

    def available(self):
        return os.path.isdir(self.root)

    def meta(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def build(self):
        # Prüfen und Setzen in einem Schritt: zwei schnelle POST /api/files/refresh liefen sonst gleichzeitig in dieselbe
        # Tabelle files_new (einer löschte sie dem anderen unter den Händen weg) und lasen alle Platten doppelt
        if not self._build_guard.acquire(blocking=False):
            return
        try:
            if self.state["running"] or not self.available():
                return
            self.state = {"running": True, "dirs": 0, "files": 0, "error": None}
            self._build()
        finally:
            self._build_guard.release()

    def _build(self):
        started = time.time()
        try:
            with self.lock:
                self.db.execute("DROP TABLE IF EXISTS files_new")
                self.db.execute("CREATE TABLE files_new (p TEXT PRIMARY KEY, name TEXT, dir INTEGER, size INTEGER, mtime INTEGER)")
            batch = []
            sizes = {}   # Ordner (relativ, "" = alles) -> [Bytes, Dateien], aufsummiert über alle Unterordner
            stack = [""]
            while stack:
                rel = stack.pop()
                try:
                    entries = list(os.scandir(os.path.join(self.root, rel)))
                except OSError:
                    continue
                for e in entries:
                    if (not rel and top_excluded(e.name, self.exclude)) or e.name in FILES_SKIP:
                        continue
                    p = f"{rel}/{e.name}" if rel else e.name
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    batch.append((p, e.name, 1 if is_dir else 0, 0 if is_dir else st.st_size, int(st.st_mtime)))
                    if is_dir:
                        stack.append(p)
                        sizes.setdefault(p, [0, 0])
                        self.state["dirs"] += 1
                    else:
                        self.state["files"] += 1
                        # Grösse bei allen Elternordnern bis ganz oben dazuzählen
                        d = rel
                        while True:
                            acc = sizes.setdefault(d, [0, 0])
                            acc[0] += st.st_size
                            acc[1] += 1
                            if not d:
                                break
                            d = d.rsplit("/", 1)[0] if "/" in d else ""
                if len(batch) >= 2000:
                    with self.lock:
                        self.db.executemany("INSERT OR REPLACE INTO files_new VALUES (?,?,?,?,?)", batch)
                    batch = []
            with self.lock:
                self.db.executemany("INSERT OR REPLACE INTO files_new VALUES (?,?,?,?,?)", batch)
                # Medien-Bestand: Videodateien des bisherigen Index aufheben, bevor er ersetzt wird («neu» = seit dem vorletzten Lauf)
                prev = self.db.execute("SELECT v FROM meta WHERE k='last'").fetchone()
                self.db.execute("DROP TABLE IF EXISTS inv_prev")
                self.db.execute("CREATE TABLE inv_prev (p TEXT PRIMARY KEY)")
                if prev:
                    self.db.execute(f"INSERT OR IGNORE INTO inv_prev SELECT p FROM files WHERE dir=0 AND {VIDEO_SQL}")
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('previous', ?)", (prev[0] if prev else "0",))
                self.db.execute("DROP TABLE IF EXISTS files")
                self.db.execute("ALTER TABLE files_new RENAME TO files")
                self.db.execute("CREATE INDEX IF NOT EXISTS files_size ON files (dir, size)")
                self.db.execute("DROP TABLE IF EXISTS dirsize")
                self.db.execute("CREATE TABLE dirsize (p TEXT PRIMARY KEY, parent TEXT, name TEXT, size INTEGER, files INTEGER)")
                self.db.executemany("INSERT INTO dirsize VALUES (?,?,?,?,?)",
                                    [(d, (d.rsplit("/", 1)[0] if "/" in d else "") if d else None, d.rsplit("/", 1)[-1], v[0], v[1])
                                     for d, v in sizes.items()])
                self.db.execute("CREATE INDEX IF NOT EXISTS dirsize_parent ON dirsize (parent)")
                self.has_sizes = True
                for k, v in (("last", int(time.time())), ("duration", int(time.time() - started)),
                             ("dirs", self.state["dirs"]), ("files", self.state["files"])):
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
                self.db.commit()
        except Exception as e:  # noqa: BLE001 (Fehler melden statt den Agenten zu stoppen)
            self.state["error"] = str(e)
        finally:
            self.state["running"] = False
        if not self.state["error"]:
            self.precompute()

    def precompute(self, wait=None):
        """Bestand und Duplikat-Kandidaten im Hintergrund neu rechnen (nach jedem Index-Lauf). [wait]: höchstens so lange warten."""
        # Nach jedem Index-Lauf immer neu rechnen, auch wenn der letzte Versuch fehlgeschlagen ist
        threads = [self._calc_start("inventory", self._inventory_compute, self._index_key),
                   self._calc_start("dup", lambda: self._dup_compute(DUP_PRE_MB * 1024 * 1024), lambda: self._index_key()[0])]
        for t in threads:
            t.join(wait)

    # --- Fertig gerechnete Ergebnisse (ab 1.13.0)

    def _calc_start(self, name, compute, key_fn=None):
        """Startet höchstens einen Rechen-Thread pro Name; läuft schon einer, wird dieser zurückgegeben.
        [key_fn] liefert den Stand des Index, für den gerechnet wird (damit ein Fehler diesem Stand zugeordnet ist)."""
        with self._calc_guard:
            t = self._calc.get(name)
            if t is None or not t.is_alive():
                t = threading.Thread(target=self._calc_run, args=(name, compute, key_fn), daemon=True)
                self._calc[name] = t
                t.start()
            return t

    def _calc_run(self, name, compute, key_fn=None):
        try:
            key, value = compute()
            with self._calc_guard:
                self._cache[name] = {"key": key, "value": value, "computed_at": int(time.time())}
        except Exception as e:  # noqa: BLE001 (Fehler melden statt den Agenten zu stoppen)
            print(f"Datei-Index ({name}):", e, flush=True)
            # Fehler merken (mit Stand des Index): die Anfragen melden ihn, statt bei jeder Abfrage neu zu rechnen
            try:
                key = key_fn() if key_fn else None
            except Exception:  # noqa: BLE001
                key = None
            with self._calc_guard:
                self._cache[name] = {"key": key, "error": f"{type(e).__name__}: {e}"[:200], "failed_at": time.time(), "computed_at": 0}

    def _cached(self, name):
        """Fertiges Ergebnis (mit «value») oder None; ein gemerkter Fehler zählt nicht als Ergebnis."""
        with self._calc_guard:
            c = self._cache.get(name)
            return c if c and "value" in c else None

    def _calc_failed(self, name, key):
        """Gemerkter Fehler für diesen Stand des Index, solange er jünger als CALC_RETRY ist; sonst None (neu versuchen)."""
        with self._calc_guard:
            c = self._cache.get(name)
            if not c or "error" not in c or c["key"] != key or time.time() - c["failed_at"] >= CALC_RETRY:
                return None
            return c["error"]

    def calculating(self, name):
        with self._calc_guard:
            t = self._calc.get(name)
            return t is not None and t.is_alive()

    def _index_key(self):
        with self.lock:
            return self._index_key_locked()

    def _index_key_locked(self):
        """Stand des Index (last, previous); nur unter self.lock aufrufen."""
        r = dict(self.db.execute("SELECT k, v FROM meta WHERE k IN ('last', 'previous')").fetchall())
        return int(r.get("last") or 0), int(r.get("previous") or 0)

    def building_reply(self, extra):
        """Antwort, solange noch nichts fertig gerechnet ist: sofort, mit dem Stand des Index (die App fragt wieder nach)."""
        last, previous = self._index_key()
        out = {"building": True, "computed_at": 0, "last": last, "previous": previous,
               "status": {"running": self.state["running"], "dirs": int(self.meta("dirs", "0")), "files": int(self.meta("files", "0")),
                          "progress": {"dirs": self.state["dirs"], "files": self.state["files"]}}}
        out.update(extra)
        return out

    def _has_table(self, name):
        with self.lock:
            return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def tree(self, path=""):
        """Speicher-Analyse: Grösse eines Ordners und seiner direkten Unterordner und Dateien (grösste zuerst).
        Rechnet nichts neu, sondern liest die beim Indexieren gespeicherten Summen: antwortet sofort."""
        path = path.strip("/")
        base = {"ready": self.has_sizes, "running": self.state["running"], "last": int(self.meta("last", "0")), "path": path}
        if not self.has_sizes:
            return base
        with self.lock:
            me = self.db.execute("SELECT size, files FROM dirsize WHERE p=?", (path,)).fetchone()
            dirs = self.db.execute("SELECT p, name, size, files FROM dirsize WHERE parent=? ORDER BY size DESC LIMIT 500", (path,)).fetchall()
            like = (path.replace("%", "\\%").replace("_", "\\_") + "/%") if path else "%"
            files = self.db.execute("SELECT p, name, size FROM files WHERE dir=0 AND p LIKE ? ESCAPE '\\' AND instr(substr(p, ?), '/')=0 "
                                    "ORDER BY size DESC LIMIT 300", (like, len(path) + 2 if path else 1)).fetchall()
        children = [{"path": r[0], "name": r[1], "size": r[2], "files": r[3], "dir": True} for r in dirs]
        children += [{"path": r[0], "name": r[1], "size": r[2], "files": 1, "dir": False} for r in files]
        children.sort(key=lambda c: -c["size"])
        base.update({"size": me[0] if me else 0, "files": me[1] if me else 0, "found": me is not None, "children": children[:500]})
        return base

    def largest(self, path="", limit=50):
        """Die grössten Dateien (unter [path] oder überall)."""
        path = path.strip("/")
        like = (path.replace("%", "\\%").replace("_", "\\_") + "/%") if path else "%"
        with self.lock:
            rows = self.db.execute("SELECT p, name, size, mtime FROM files WHERE dir=0 AND p LIKE ? ESCAPE '\\' ORDER BY size DESC LIMIT ?",
                                   (like, max(1, min(int(limit), 500)))).fetchall()
        return [{"path": r[0], "name": r[1], "size": r[2], "mtime": r[3]} for r in rows]

    def status(self):
        return {"available": self.available(), "running": self.state["running"], "error": self.state["error"],
                "progress": {"dirs": self.state["dirs"], "files": self.state["files"]},
                "dirs": int(self.meta("dirs", "0")), "files": int(self.meta("files", "0")),
                "last": int(self.meta("last", "0")), "duration": int(self.meta("duration", "0")),
                "every_hours": FILES_EVERY_H, "excluded": sorted(self.exclude)}

    # Ordner, die sich von selbst füllen und gerne vergessen gehen (Name, auf Ebene 1 oder 2 der Freigaben)
    CLEANUP_NAMES = {"downloads", "complete", "completed", "incomplete", "_papierkorb", "papierkorb", ".recycle.bin", "trash", "eingang"}

    def cleanup(self, old_days=30):
        """Je Ordner aus CLEANUP_NAMES: Grösse, Anzahl Dateien, wie viel davon älter als [old_days] Tage, älteste Datei."""
        old_days = max(0, min(int(old_days), 36500))
        cutoff = int(time.time()) - old_days * 86400
        out = []
        with self.lock:
            dirs = [r[0] for r in self.db.execute("SELECT p FROM files WHERE dir=1").fetchall()
                    if r[0].count("/") <= 1 and r[0].rsplit("/", 1)[-1].lower() in self.CLEANUP_NAMES]
            for d in dirs:
                like = d.replace("%", "\\%").replace("_", "\\_") + "/%"
                size, files, old, oldest = self.db.execute(
                    "SELECT COALESCE(SUM(size),0), COUNT(*), COALESCE(SUM(CASE WHEN mtime < ? THEN size ELSE 0 END),0), MIN(mtime) "
                    "FROM files WHERE dir=0 AND p LIKE ? ESCAPE '\\'", (cutoff, like)).fetchone()
                out.append({"path": d, "size": size, "files": files, "old_size": old, "oldest": oldest or 0, "old_days": old_days})
        return sorted(out, key=lambda x: -x["size"])

    def search(self, q, limit=200):
        words = [w for w in q.lower().split() if w][:6]
        if not words:
            return []
        # Jedes Wort muss im Pfad vorkommen (so findet «one piece 1023» auch die Folge im Serien-Ordner)
        where = " AND ".join(["p LIKE ? ESCAPE '\\'"] * len(words))
        # «%» und «_» wörtlich suchen, nicht als LIKE-Platzhalter (sonst findet «%» alles)
        esc = [w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for w in words]
        args = [f"%{w}%" for w in esc] + [max(1, min(limit, 1000))]
        with self.lock:
            rows = self.db.execute(f"SELECT p, name, dir, size, mtime FROM files WHERE {where} "
                                   "ORDER BY dir DESC, length(p) LIMIT ?", args).fetchall()
        return [{"path": r[0], "name": r[1], "dir": bool(r[2]), "size": r[3], "mtime": r[4]} for r in rows]

    # --- Doppelte Dateien (ab 1.8.0). Nur lesen: der Agent löscht und verschiebt nie etwas.

    def hashing(self):
        return self.hasher is not None and self.hasher.is_alive()

    def duplicates(self, min_mb=100, limit=100, background=True):
        """
        Doppelte Dateien: gleiche Grösse (aus dem Index) und gleiche Teil-Prüfsumme (erstes und letztes MiB plus Grösse).
        Fehlen Prüfsummen, startet ein Hintergrund-Thread; bis er fertig ist, ist das Ergebnis unvollständig (running=true).
        """
        min_size = max(1, int(min_mb * 1024 * 1024))
        limit = max(1, min(int(limit), 1000))
        # Ab 1.13.0: Kandidaten (gleich grosse Dateien) kommen fertig aus dem Hintergrund; gerechnet wird nie in der Anfrage.
        last = self._index_key()[0]
        c = self._cached("dup")
        if not (c and c["key"] == last and c["value"]["min_size"] <= min_size):
            # Letzter Versuch für diesen Index fehlgeschlagen: Fehler melden, nicht bei jeder Abfrage neu rechnen
            failed = self._calc_failed("dup", last)
            if failed:
                raise CalcError(f"Duplikat-Suche fehlgeschlagen ({failed})")
            self._calc_start("dup", lambda: self._dup_compute(min(min_size, DUP_PRE_MB * 1024 * 1024)),
                             lambda: self._index_key()[0]).join(CALC_WAIT)
            c = self._cached("dup")
            if not (c and c["key"] == last and c["value"]["min_size"] <= min_size):
                failed = self._calc_failed("dup", last)
                if failed:
                    raise CalcError(f"Duplikat-Suche fehlgeschlagen ({failed})")
                return self.building_reply({"running": True, "groups": [], "wasted": 0, "checked_groups": 0, "candidate_groups": 0,
                                            "pending": 0, "min_mb": min_mb, "error": None})
        cands = [r for r in c["value"]["rows"] if r[1] >= min_size]
        with self.lock:
            known = {(p, size, mtime): h for p, size, mtime, h in self.db.execute("SELECT p, size, mtime, h FROM hashes").fetchall()}
        by_size, pending = {}, []
        for p, size, mtime in cands:
            h = known.get((p, size, mtime))
            by_size.setdefault(size, []).append((p, mtime, h))
            if h is None:
                pending.append((p, size, mtime))
        if pending and background:
            self.start_hashing(pending)
        groups, checked = [], 0
        for size, files in by_size.items():
            if all(h is not None for _, _, h in files):
                checked += 1
            by_hash = {}
            for p, mtime, h in files:
                if h and not h.startswith("!"):      # «!» = Datei nicht lesbar
                    by_hash.setdefault(h, []).append({"path": p, "mtime": mtime})
            groups += [{"size": size, "files": fl} for fl in by_hash.values() if len(fl) >= 2]
        groups.sort(key=lambda g: -(len(g["files"]) - 1) * g["size"])
        return {"last": last, "running": self.hashing(), "groups": groups[:limit],
                "wasted": sum((len(g["files"]) - 1) * g["size"] for g in groups), "checked_groups": checked,
                "candidate_groups": len(by_size), "pending": len(pending), "min_mb": min_mb, "error": self.hash_error,
                "building": False, "computed_at": c["computed_at"]}

    def _dup_compute(self, min_size):
        """Kandidaten für Duplikate: alle Dateien ab [min_size], deren Grösse mehrfach vorkommt. Unter der Sperre nur lesen,
        gruppiert wird danach (die Suche und der Index bleiben währenddessen schnell)."""
        with self.lock:
            key = self._index_key_locked()[0]
            rows = self.db.execute("SELECT p, size, mtime FROM files WHERE dir=0 AND size >= ?", (min_size,)).fetchall()
        count = {}
        for _, size, _ in rows:
            count[size] = count.get(size, 0) + 1
        cands = sorted((r for r in rows if count[r[1]] >= 2), key=lambda r: (-r[1], r[0]))
        return key, {"min_size": min_size, "rows": cands}

    def start_hashing(self, pending):
        """Höchstens ein Prüf-Thread gleichzeitig, höchstens DUP_MAX_FILES Dateien pro Lauf (grösste zuerst)."""
        with self.lock:
            if self.hashing():
                return False
            self.hasher = threading.Thread(target=self._hash_run, args=(pending[:DUP_MAX_FILES],), daemon=True)
            self.hasher.start()
        return True

    def _hash_run(self, items):
        try:
            os.nice(10)            # unter Linux nur dieser Thread: Plex und Co. haben Vorrang
        except (OSError, AttributeError):
            pass
        try:
            self.hash_error = None
            done = []
            for p, size, mtime in items:
                try:
                    h = partial_hash(os.path.join(self.root, p), size, self.root)
                except OSError:
                    h = "!"
                done.append((p, size, mtime, h))
                if len(done) >= 50:
                    with self.lock:
                        self.db.executemany("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)", done)
                        self.db.commit()
                    done = []
            with self.lock:
                self.db.executemany("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)", done)
                # Prüfsummen von gelöschten oder geänderten Dateien vergessen
                self.db.execute("DELETE FROM hashes WHERE NOT EXISTS "
                                "(SELECT 1 FROM files f WHERE f.p=hashes.p AND f.size=hashes.size AND f.mtime=hashes.mtime)")
                self.db.commit()
        except Exception as e:  # noqa: BLE001 (Fehler melden statt den Agenten zu stoppen)
            self.hash_error = str(e)

    # --- Medien-Bestand (ab 1.8.0), direkt aus dem Index

    def inventory(self):
        """Filme und Serien aus dem Index: Titel, Jahr, IMDb-ID, Folgen pro Staffel, Lücken, neu seit dem vorletzten Index-Lauf.
        Ab 1.13.0 fertig gerechnet nach jedem Index-Lauf; fehlt das Ergebnis noch, kurz warten, sonst «building» (die App fragt wieder)."""
        key = self._index_key()
        c = self._cached("inventory")
        if not (c and c["key"] == key):
            # Letzter Versuch für diesen Index fehlgeschlagen: Fehler melden, nicht bei jeder Abfrage neu rechnen
            failed = self._calc_failed("inventory", key)
            if failed:
                raise CalcError(f"Bestandsliste fehlgeschlagen ({failed})")
            self._calc_start("inventory", self._inventory_compute, self._index_key).join(CALC_WAIT)
            c = self._cached("inventory")
            if not (c and c["key"] == key):
                failed = self._calc_failed("inventory", key)
                if failed:
                    raise CalcError(f"Bestandsliste fehlgeschlagen ({failed})")
                return self.building_reply({"movies": [], "series": [], "new": {"movies": [], "episodes": []}})
        return dict(c["value"], building=False, computed_at=c["computed_at"])

    def _inventory_compute(self):
        """Rechnet den Bestand. Unter self.lock werden nur die Zeilen gelesen (eine Momentaufnahme), verarbeitet wird danach."""
        with self.lock:
            last, previous = self._index_key_locked()
            dirs = [r[0] for r in self.db.execute("SELECT p FROM files WHERE dir=1 AND p NOT LIKE '%/%/%'").fetchall()]
            roots = [(d, "movie" if d.rsplit("/", 1)[-1].lower() in MOVIE_ROOTS else "series") for d in sorted(dirs)
                     if d.rsplit("/", 1)[-1].lower() in MOVIE_ROOTS | SERIES_ROOTS]
            # «TV/Shows» nicht doppelt zählen, wenn «TV» schon ein Serien-Ordner ist
            roots = [(d, k) for d, k in roots if not any(d.startswith(o + "/") for o, _ in roots)]
            snap = []
            for root, kind in roots:
                like = root.replace("%", "\\%").replace("_", "\\_") + "/%"
                snap.append((root, kind, self.db.execute(
                    f"SELECT f.p, f.name, f.size, i.p IS NULL FROM files f LEFT JOIN inv_prev i ON i.p=f.p "
                    f"WHERE f.dir=0 AND f.p LIKE ? ESCAPE '\\' AND {VIDEO_SQL.replace('name', 'f.name')}", (like,)).fetchall()))
        out = {"last": last, "previous": previous, "movies": [], "series": [], "new": {"movies": [], "episodes": []}}
        for root, kind, rows in snap:
            items = {}
            for p, name, size, is_new in rows:
                parts = p[len(root) + 1:].split("/")
                if len(parts) == 1 and kind == "series":
                    continue                  # lose Datei direkt im Serien-Ordner: keiner Serie zuzuordnen
                key = f"{root}/{parts[0]}"
                items.setdefault(key, []).append((name, size, bool(is_new) and previous > 0))
            for folder, files in items.items():
                base = folder.rsplit("/", 1)[-1]
                if kind == "movie" and len(files) == 1 and folder.endswith("/" + files[0][0]):
                    base = base.rsplit(".", 1)[0]  # Film ohne eigenen Ordner: Titel aus dem Dateinamen
                title, year, imdb = parse_title(base)
                size = sum(f[1] for f in files)
                if kind == "movie":
                    new = all(f[2] for f in files)
                    out["movies"].append({"folder": folder, "title": title, "year": year, "imdb": imdb, "files": len(files),
                                          "size": size, "new": new})
                    if new:
                        out["new"]["movies"].append(f"{title} ({year})" if year else title)
                else:
                    st = series_stats(files)
                    out["series"].append(dict({"folder": folder, "title": title, "year": year, "imdb": imdb, "size": size}, **st["stats"]))
                    out["new"]["episodes"] += [f"{title} – {lbl}" for lbl in st["new"]]
        out["movies"].sort(key=lambda m: (m["title"].lower(), m["year"] or 0))
        out["series"].sort(key=lambda m: (m["title"].lower(), m["year"] or 0))
        out["new"]["movies"] = out["new"]["movies"][:500]
        out["new"]["episodes"] = out["new"]["episodes"][:1000]
        return (last, previous), out


def partial_hash(path, size, root=None):
    """sha1 aus erstem und letztem MiB plus Grösse: liest höchstens 2 MiB, egal wie gross die Datei ist.
    Ab 1.16.1 (Audit Restteile): nur echte Dateien innerhalb von [root]. Ein Symlink in einer Freigabe (z. B. auf
    /config/token.txt oder ausserhalb der Freigaben) oder eine Pipe (open() bliebe für immer hängen) gilt als nicht lesbar."""
    if root is not None:
        base = os.path.realpath(root)
        if not os.path.realpath(path).startswith(base.rstrip(os.sep) + os.sep):
            raise OSError("ausserhalb der Freigaben")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("keine normale Datei")
        h = hashlib.sha1()
        with os.fdopen(fd, "rb") as f:
            fd = None
            h.update(f.read(HASH_PART))
            if size > HASH_PART:
                f.seek(max(HASH_PART, size - HASH_PART))
                h.update(f.read(HASH_PART))
    finally:
        if fd is not None:
            os.close(fd)
    h.update(str(size).encode())
    return h.hexdigest()


def parse_title(name):
    """«Babylon 5 (1994) [imdbid-tt0105946]» → ("Babylon 5", 1994, "tt0105946"); auch «Film.Name.2010.1080p» (Jahr ohne Klammern)."""
    m = re.search(r"(?i)imdb(?:id)?[-_ =]?(tt\d{6,10})", name) or re.search(r"(?<![a-z0-9])(tt\d{7,10})(?![0-9])", name)
    imdb = m.group(1).lower() if m else None
    t = re.sub(r"\s*[\[{][^\]}]*[\]}]\s*", " ", name).strip()
    m = re.search(r"\((19\d\d|20\d\d)\)", t)
    if m:
        return t[:m.start()].strip() or t, int(m.group(1)), imdb
    if " " not in t and re.search(r"[._]", t):
        t = re.sub(r"[._]+", " ", t).strip()
    m = re.search(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", t)
    if m and m.start() > 0:
        return t[:m.start()].strip(" -"), int(m.group(1)), imdb
    return t, None, imdb


EP_SXE = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,3})[ ._]?e(\d{1,4})((?:[ ._]?-?[ ._]?e\d{1,4})*)")
EP_NXN = re.compile(r"(?i)(?<![\d.])(\d{1,2})x(\d{2,3})(?!\d)")


def parse_episodes(name):
    """Folgen aus einem Dateinamen: [(Staffel, Folge, absolut)]. S01E02, s1e2, S01E01E02, S01E01-E03, 1x02;
    SxxE### (drei und mehr Stellen, One-Piece-Art) zählt als durchlaufende Nummer."""
    m = EP_SXE.search(name)
    if m:
        s, e, absolute = int(m.group(1)), int(m.group(2)), len(m.group(2)) >= 3
        extra = [int(x) for x in re.findall(r"\d+", m.group(3) or "")]
        eps = [e]
        if extra and extra[-1] > e and extra[-1] - e <= 10:
            eps = list(range(e, extra[-1] + 1))
        return [(s, x, absolute) for x in eps]
    m = EP_NXN.search(name)
    if m:
        return [(int(m.group(1)), int(m.group(2)), False)]
    return []


def compact(nums, fmt):
    """[1, 2, 3, 7] → ["S01E01-S01E03", "S01E07"] (mit fmt für eine Nummer)."""
    out, nums = [], sorted(nums)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(fmt(nums[i]) if i == j else f"{fmt(nums[i])}-{fmt(nums[j])}")
        i = j + 1
    return out


def series_stats(files):
    """Folgen pro Staffel, Lücken und neue Folgen einer Serie aus [(Dateiname, Grösse, neu)]."""
    seasons, absolute, unparsed, new = {}, {}, 0, []
    for name, _, is_new in files:
        eps = parse_episodes(name)
        if not eps:
            unparsed += 1
        for s, e, ab in eps:
            (absolute if ab else seasons).setdefault(s, set()).add(e)
        if is_new:
            new.append("".join(f"S{s:02d}E{e:03d}" if ab else f"S{s:02d}E{e:02d}" for s, e, ab in eps[:1]) or name)
    gaps = []
    for s in sorted(seasons):
        have = seasons[s]
        if s > 0 and max(have) <= 2000:          # Staffel 0 = Specials: keine Lücken
            gaps += compact([n for n in range(1, max(have) + 1) if n not in have], lambda n, s=s: f"S{s:02d}E{n:02d}")
    if absolute:
        # Durchlaufende Nummern: Lücken über alle Staffeln zusammen (One Piece: S01E001 … S01E1023)
        have = set().union(*absolute.values())
        one = list(absolute)[0] if len(absolute) == 1 else None
        if max(have) <= 5000:
            gaps += compact([n for n in range(1, max(have) + 1) if n not in have],
                            (lambda n: f"S{one:02d}E{n:03d}") if one is not None else (lambda n: f"E{n:03d}"))
    counts = {}
    for d in (seasons, absolute):
        for s, eps in d.items():
            counts[str(s)] = counts.get(str(s), 0) + len(eps)
    return {"stats": {"episodes": sum(counts.values()) + unparsed, "seasons": dict(sorted(counts.items(), key=lambda x: int(x[0]))),
                      "gaps": gaps[:300], "mixed_numbering": bool(seasons) and bool(absolute), "new_episodes": len(new)},
            "new": new}


# ---------------------------------------------------------------- WireGuard (nur lesen, nie private Schlüssel)

def wg_dump():
    """`wg show all dump` → (Rückgabecode, Ausgabe). Fehlt wg im Container: FileNotFoundError."""
    r = subprocess.run(["wg", "show", "all", "dump"], capture_output=True, text=True, timeout=5)
    return r.returncode, r.stdout


WG_KEY = re.compile(r"^[A-Za-z0-9+/]{42,43}=$")
WG_CONF_MAX = 256 * 1024        # ab 1.16.1 (Audit Restteile): Konfigurationsdatei höchstens so weit lesen
# Kommentar sieht nach Schlüssel aus («#PresharedKey = …», auskommentierter Schlüssel): nie als Peer-Name übernehmen
# Review: nur «Private Key»/«PrivateKey»/«private_key», nicht jedes «privat…» (sonst verschwand «Mein privater Laptop»)
WG_SECRETISH = re.compile(r"(?i)private[\s_-]*key|preshared|\bpsk\b|[A-Za-z0-9+/]{40,}={0,2}")


def _wg_label(c):
    """Peer-Name aus Kommentar oder NAME-Feld, ohne alles, was nach Schlüssel aussieht."""
    c = (c or "").strip()
    return None if not c or WG_SECRETISH.search(c) else c[:60]       # erst prüfen, dann kürzen (kein halber Schlüssel)


def _wg_read(path):
    """Höchstens WG_CONF_MAX Bytes einer Konfigurationsdatei als Text (vorher fh.read() ohne Grenze)."""
    with open(path, "rb") as fh:
        return fh.read(WG_CONF_MAX).decode("utf-8", "replace")


def wg_names(conf_dir=None):
    """
    Peer-Namen aus der WireGuard-Konfiguration von Unraid (öffentlicher Schlüssel → Name), so gut es geht:
    wg0.conf mit Kommentar nach «[Peer]» («#Peer 1») oder direkt davor, und wg0.cfg mit Zeilen «NAME:1="…"» / «PUBLIC:1="…"».
    Private und Preshared Keys werden dabei nicht einmal zwischengespeichert.
    """
    conf_dir = conf_dir or WG_CONFIG_DIR
    names = {}
    for f in sorted(glob.glob(os.path.join(conf_dir, "*.conf"))):
        try:
            lines = _wg_read(f).splitlines()
        except OSError:
            continue
        # Kommentar im Peer-Block hat Vorrang, sonst der Kommentar direkt über «[Peer]»
        in_peer, inside, above, last = False, None, None, None
        for line in lines:
            t = line.strip()
            if t.lower() == "[peer]":
                in_peer, inside, above, last = True, None, last, None
            elif t.startswith("[") and t.endswith("]"):
                in_peer, inside, last = False, None, None
            elif t.startswith("#"):
                c = _wg_label(t.lstrip("#"))
                if in_peer and inside is None:
                    inside = c
                last = c
            else:
                if in_peer and t.replace(" ", "").lower().startswith("publickey="):
                    pub = t.split("=", 1)[1].strip()
                    if (inside or above) and WG_KEY.match(pub):
                        names[pub] = inside or above
                if t:
                    last = None
    for f in sorted(glob.glob(os.path.join(conf_dir, "*.cfg"))):
        slots = {}
        try:
            for line in _wg_read(f).splitlines():
                m = re.match(r'^([A-Za-z_]+):(\d+)="?(.*?)"?\s*$', line.strip())
                if not m:
                    continue
                k = m.group(1).upper()
                if "PRIVATE" in k or "PRESHARED" in k or "PSK" in k:
                    continue
                if "PUBLIC" in k and WG_KEY.match(m.group(3)):
                    slots.setdefault(m.group(2), {})["pub"] = m.group(3)
                elif ("NAME" in k or "DESC" in k) and _wg_label(m.group(3)):
                    slots.setdefault(m.group(2), {})["name"] = _wg_label(m.group(3))
        except OSError:
            continue
        for v in slots.values():
            if v.get("pub") and v.get("name"):
                names.setdefault(v["pub"], v["name"])
    return names


def wireguard_status(run=None, conf_dir=None, now=None):
    """
    WireGuard-Tunnel und Peers aus `wg show all dump` (run ersetzbar für Tests).
    Interface-Zeilen enthalten den privaten Schlüssel (Feld 2), Peer-Zeilen den Preshared Key (Feld 3): beide werden nie übernommen,
    vom öffentlichen Schlüssel nur die ersten 8 Zeichen als Kennung.
    """
    run = run or wg_dump
    try:
        code, out = run()
    except FileNotFoundError:
        return {"available": False, "reason": "no_wg", "interfaces": []}
    except (OSError, subprocess.SubprocessError) as e:
        return {"available": False, "reason": "no_permission", "error": type(e).__name__, "interfaces": []}
    if code != 0:
        return {"available": False, "reason": "no_permission", "interfaces": []}
    try:
        return _wg_parse(out, now or time.time(), wg_names(conf_dir))
    except Exception as e:  # noqa: BLE001
        # Ab 1.16.1 (Audit Restteile): nie den Fehlertext zurückgeben (die Ausgabe von wg enthält private Schlüssel)
        return {"available": False, "reason": "error", "error": type(e).__name__, "interfaces": []}


def _wg_parse(out, now, names):
    num = lambda x: int(x) if x.isdigit() else 0  # noqa: E731
    ifaces = {}
    for line in (out or "").splitlines():
        f = line.split("\t")
        if len(f) == 5:
            # Interface: Name, privater Schlüssel (nie lesen), öffentlicher Schlüssel, Port, fwmark
            ifaces.setdefault(f[0], {"name": f[0], "listen_port": None, "peers": []})["listen_port"] = num(f[3]) or None
        elif len(f) == 9:
            # Peer: Interface, öffentlicher Schlüssel, Preshared Key (nie lesen), Endpunkt, erlaubte IPs, Handshake, rx, tx, keepalive
            pub, hs = f[1], num(f[5])
            ifaces.setdefault(f[0], {"name": f[0], "listen_port": None, "peers": []})["peers"].append({
                "id": pub[:8], "name": names.get(pub), "endpoint": None if f[3] in ("(none)", "") else f[3],
                "allowed_ips": "" if f[4] == "(none)" else f[4], "handshake": hs, "rx": num(f[6]), "tx": num(f[7]),
                "online": hs > 0 and now - hs < 180})
    if not ifaces:
        return {"available": False, "reason": "no_interfaces", "interfaces": []}
    return {"available": True, "reason": "ok", "interfaces": list(ifaces.values())}


def run_indexer(index, stop):
    """Baut den Index beim Start (wenn älter als das Intervall) und danach regelmässig neu."""
    warm = False
    while not stop.is_set():
        try:
            if index.available() and (not index.has_sizes or time.time() - int(index.meta("last", "0") or 0) >= FILES_EVERY_H * 3600):
                index.build()                  # rechnet Bestand und Duplikat-Kandidaten danach selbst vor
                warm = True
            elif not warm and index.has_sizes:
                index.precompute()             # Neustart mit vorhandenem Index: einmal vorrechnen, damit die App nicht wartet
                warm = True
        except Exception as e:
            print("Datei-Index:", e, flush=True)
        stop.wait(60)


# ---------------------------------------------------------------- HTTP

TOKEN_STATE = {"error": "", "source": "", "code": ""}      # source: env, file, new; error: warum kein Token geladen ist
CONFIG_FIX_DE = ("Unraid › Docker › nzblab-agent › Bearbeiten › Pfad /config → /mnt/user/appdata/nzblab-agent "
                 "(Zugriffsmodus Lesen/Schreiben) › Übernehmen")
CONFIG_FIX_EN = ("Unraid › Docker › nzblab-agent › Edit › path /config → /mnt/user/appdata/nzblab-agent "
                 "(access mode Read/Write) › Apply")
_ANON_VOLUME = re.compile(r"/volumes/[0-9a-f]{64}/_data$")


TOKEN_MIN_LEN = 32      # ab 1.16.1: kürzere eigene Tokens lassen sich offline erraten (/api/hello) und werden abgelehnt
TOKEN_MIN_DISTINCT = 8  # ab 1.16.1 (Härtung): «aaaa…» oder «12341234…» sind trotz 32 Zeichen leicht zu erraten


def _token_weak(t):
    """None = brauchbar, sonst «short» (unter 32 Zeichen), «chars» (Leer- oder Steuerzeichen) oder «simple» (weniger als
    8 verschiedene Zeichen). Der Standard-Token (token_urlsafe(24)) hat praktisch immer über 20 verschiedene Zeichen."""
    if any(c.isspace() or not c.isprintable() for c in t):
        return "chars"
    if len(t) < TOKEN_MIN_LEN:
        return "short"
    return "simple" if len(set(t)) < TOKEN_MIN_DISTINCT else None


def _token_error(why_de, why_en, path, env=False, code="unusable"):
    """Token-Datei vorhanden, aber nicht nutzbar: nie überschreiben (sonst passt die App nicht mehr), laut melden."""
    if env:
        fix_de = ("Variable NZBLAB_AGENT_TOKEN leer lassen (dann erzeugt der Agent einen sicheren Token) oder mindestens "
                  f"{TOKEN_MIN_LEN} zufällige Zeichen ohne Leerzeichen eintragen.")
        fix_en = (f"leave the variable NZBLAB_AGENT_TOKEN empty (the agent then creates a secure token) or set at least "
                  f"{TOKEN_MIN_LEN} random characters without spaces.")
    else:
        fix_de = (f"{path} löschen (dann beim nächsten Start ein neuer Token, in der App neu eintragen) oder mindestens "
                  f"{TOKEN_MIN_LEN} zufällige Zeichen ohne Leerzeichen hineinschreiben, oder Variable NZBLAB_AGENT_TOKEN setzen.")
        fix_en = (f"delete {path} (a new token is created on the next start, enter it in the app) or write at least "
                  f"{TOKEN_MIN_LEN} random characters without spaces into it, or set the variable NZBLAB_AGENT_TOKEN.")
    TOKEN_STATE["code"] = code      # ab 1.19.0: nach aussen nur dieser Code, der Text nur im Log und für 127.0.0.1
    TOKEN_STATE["error"] = f"{why_de} Der Agent erzeugt bewusst keinen neuen Token. Lösung: {fix_de}"
    line = "!" * 72
    print(f"{line}\nFEHLER: Token nicht nutzbar. {TOKEN_STATE['error']}\n"
          f"ERROR: token unusable. {why_en} The agent deliberately does not create a new token. Fix: {fix_en}\n"
          f"Bis dahin antworten alle Abfragen mit Token mit 503 / until then all authenticated requests answer 503.\n{line}",
          flush=True)
    return ""


def token(announce=True):
    """Token für alle Anfragen. Reihenfolge: Variable NZBLAB_AGENT_TOKEN, sonst CONFIG/token.txt, sonst neu erzeugen.
    Ein neuer Token entsteht NUR, wenn token.txt gar nicht existiert. Ist die Datei da, aber leer, kaputt oder nicht
    lesbar (Rechte), bleibt sie unangetastet: Rückgabe "", Grund in TOKEN_STATE["error"], Abfragen mit Token geben 503
    (ab 1.13.0; vorher entstand bei leerer Datei still ein neuer Token und die App passte nicht mehr).
    Ein neu erzeugter Token steht genau einmal im Container-Log (Installation für andere: Unraid › Docker › Logs),
    danach nur noch in der Datei (Rechte 600). Wer das Log lesen kann, ist ohnehin Admin des NAS."""
    TOKEN_STATE.update(error="", source="", code="")
    t = os.environ.get("NZBLAB_AGENT_TOKEN", "").strip()
    if t:
        weak = _token_weak(t)
        if weak == "short":
            return _token_error(f"NZBLAB_AGENT_TOKEN ist zu kurz ({len(t)} Zeichen, nötig sind mindestens {TOKEN_MIN_LEN}).",
                                f"NZBLAB_AGENT_TOKEN is too short ({len(t)} characters, at least {TOKEN_MIN_LEN} needed).",
                                "NZBLAB_AGENT_TOKEN", env=True, code="short")
        if weak == "simple":
            return _token_error(f"NZBLAB_AGENT_TOKEN ist zu einfach (weniger als {TOKEN_MIN_DISTINCT} verschiedene Zeichen).",
                                f"NZBLAB_AGENT_TOKEN is too simple (fewer than {TOKEN_MIN_DISTINCT} different characters).",
                                "NZBLAB_AGENT_TOKEN", env=True, code="simple")
        if weak:
            return _token_error("NZBLAB_AGENT_TOKEN enthält Leer- oder Steuerzeichen.",
                                "NZBLAB_AGENT_TOKEN contains whitespace or control characters.", "NZBLAB_AGENT_TOKEN", env=True, code="chars")
        TOKEN_STATE["source"] = "env"
        print("HINWEIS: Der Token kommt aus NZBLAB_AGENT_TOKEN (selbst gewählt). Ein zufälliger Token ist sicherer: Variable leer lassen, "
              "dann erzeugt der Agent einen. NOTE: the token is set by you via NZBLAB_AGENT_TOKEN; a random one is safer (leave it empty).", flush=True)
        return t
    path = f"{CONFIG}/token.txt"
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        return _token_error(f"{path} lässt sich nicht prüfen ({e.strerror or e}).", f"{path} cannot be checked ({e.strerror or e}).", path, code="unreadable")
    else:
        try:
            # O_NOFOLLOW (ab 1.16.1): ein Symlink als token.txt wird nie gelesen
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                raw = f.read(4097)
            t = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return _token_error(f"{path} ist beschädigt (kein Text).", f"{path} is corrupt (not text).", path, code="corrupt")
        except OSError as e:
            return _token_error(f"{path} ist nicht lesbar ({e.strerror or e}; Rechte prüfen, PUID/PGID).",
                                f"{path} is not readable ({e.strerror or e}; check permissions, PUID/PGID).", path, code="unreadable")
        if not t:
            return _token_error(f"{path} ist leer.", f"{path} is empty.", path, code="empty")
        if len(raw) > 4096 or any(c.isspace() or not c.isprintable() for c in t):
            return _token_error(f"{path} ist beschädigt (mehrere Zeilen, Steuerzeichen oder zu lang).",
                                f"{path} is corrupt (several lines, control characters or too long).", path, code="corrupt")
        if len(t) < TOKEN_MIN_LEN:
            return _token_error(f"{path} enthält einen zu kurzen Token ({len(t)} Zeichen, nötig sind mindestens {TOKEN_MIN_LEN}).",
                                f"{path} holds a token that is too short ({len(t)} characters, at least {TOKEN_MIN_LEN} needed).", path, code="short")
        if _token_weak(t) == "simple":
            return _token_error(f"{path} enthält einen zu einfachen Token (weniger als {TOKEN_MIN_DISTINCT} verschiedene Zeichen).",
                                f"{path} holds a token that is too simple (fewer than {TOKEN_MIN_DISTINCT} different characters).", path, code="simple")
        TOKEN_STATE["source"] = "file"
        return t
    t = secrets.token_urlsafe(24)
    try:
        os.makedirs(CONFIG, exist_ok=True)
        # O_EXCL: nie eine Datei überschreiben, die inzwischen (zweiter Prozess) entstanden ist
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(t)
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        return token(announce)
    except OSError as e:
        return _token_error(f"{path} lässt sich nicht anlegen ({e.strerror or e}; ist /config schreibbar eingebunden?).",
                            f"{path} cannot be created ({e.strerror or e}; is /config mounted read/write?).", path, code="unwritable")
    TOKEN_STATE["source"] = "new"
    if announce:
        line = "=" * 64
        print(f"{line}\nNZBLab-Agent Token: {t}\n"
              f"In der App eintragen: Einstellungen › Server-Verbindungen › NZBLab-Agent.\n"
              f"Steht nur dieses eine Mal im Log; später: {path} (im Container) bzw. appdata/nzblab-agent/token.txt.\n{line}",
              flush=True)
    return t


def _unescape_mount(s):
    """mountinfo schreibt Leerzeichen, Tab, Zeilenumbruch und Backslash als \\040, \\011, \\012, \\134."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def config_mount(path=None, mountinfo="/proc/self/mountinfo"):
    """
    Liegt CONFIG auf einem eigenen, bleibenden Mount (ab 1.13.0)? Sonst sind Token, Verlauf und Einstellungen nach dem
    nächsten Neuerstellen des Containers (Unraid: jedes Update, jede Änderung an der Vorlage) weg.
    Bleibend: Bind-Mount oder benannter Volume auf CONFIG oder einem Elternordner ausser «/».
    Nicht bleibend: nur das Dateisystem des Containers («/»), tmpfs/ramfs, ein anonymer Docker-Volume (VOLUME /config im
    Dockerfile ohne eigenen Pfad: wird beim Neuerstellen durch einen neuen, leeren ersetzt) oder nur lesend eingebunden.
    Rückgabe {"persistent": True/False/None, "mount": Einhängepunkt, "fstype", "reason": none|root|tmpfs|anonymous|ro|unknown}.
    None = mountinfo nicht lesbar (kein Linux): dann keine Warnung.
    """
    p = os.path.realpath(path or CONFIG)
    try:
        with open(mountinfo, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return {"persistent": None, "mount": "", "fstype": "", "reason": "unknown"}
    best = None
    for ln in lines:
        left, sep, right = ln.partition(" - ")
        a = left.split()
        if not sep or len(a) < 6:
            continue
        mp = _unescape_mount(a[4])
        if not (p == mp or mp == "/" or p.startswith(mp.rstrip("/") + "/")):
            continue
        # Der längste passende Einhängepunkt gilt; bei gleichem Punkt der spätere (überdeckt den früheren)
        if best is None or len(mp) >= len(best[1]):
            best = (_unescape_mount(a[3]), mp, a[5].split(","), (right.split() or [""])[0])
    if best is None:
        return {"persistent": None, "mount": "", "fstype": "", "reason": "unknown"}
    root, mp, opts, fstype = best
    res = {"persistent": False, "mount": mp, "fstype": fstype, "reason": "none"}
    if mp == "/":
        res["reason"] = "root"
    elif fstype in ("tmpfs", "ramfs"):
        res["reason"] = "tmpfs"
    elif _ANON_VOLUME.search(root):
        res["reason"] = "anonymous"
    elif "ro" in opts:
        res["reason"] = "ro"
    else:
        res["persistent"] = True
    return res


CONFIG_STATE = {"persistent": None, "mount": "", "fstype": "", "reason": "unknown"}


def config_warnings(state=None, token_source=None):
    """Warnungen für /api/info und die Weboberfläche: [{"code", "de", "en"}]. Leer, wenn alles bleibt."""
    st = CONFIG_STATE if state is None else state
    src = TOKEN_STATE["source"] if token_source is None else token_source
    if st.get("persistent") is not False:
        return []
    why = {"root": ("/config ist kein eigener Ordner auf dem NAS, die Daten liegen nur im Container",
                    "/config is not mapped to a folder on the NAS, the data lives only inside the container"),
           "tmpfs": ("/config liegt im Arbeitsspeicher (tmpfs)", "/config is in RAM (tmpfs)"),
           "anonymous": ("/config ist ein anonymer Docker-Volume, beim Neuerstellen kommt ein neuer, leerer",
                         "/config is an anonymous Docker volume, re-creating the container replaces it with a new, empty one"),
           "ro": ("/config ist nur lesend eingebunden, nichts lässt sich speichern",
                  "/config is mounted read-only, nothing can be saved")}.get(st.get("reason"), ("/config bleibt nicht erhalten", "/config is not persistent"))
    if src == "env":
        lose_de, lose_en = "Der Token bleibt (Variable NZBLAB_AGENT_TOKEN), aber Verlauf, Datei-Index und Einstellungen gehen beim nächsten Update verloren.", \
            "The token stays (variable NZBLAB_AGENT_TOKEN), but history, file index and settings are lost on the next update."
    else:
        lose_de, lose_en = "Nach jedem Update oder Neuerstellen gibt es einen neuen Token (die App verbindet sich dann nicht mehr), und der Verlauf ist weg.", \
            "After every update or re-create there is a new token (the app can no longer connect) and the history is gone."
    return [{"code": "config_not_persistent",
             "de": f"{why[0]}. {lose_de} Lösung: {CONFIG_FIX_DE}.",
             "en": f"{why[1]}. {lose_en} Fix: {CONFIG_FIX_EN}."}]


def announce_config(state=None):
    """Beim Start: laute, mehrzeilige Warnung im Log, wenn /config nicht bleibt (Deutsch und Englisch, mit Lösung)."""
    warns = config_warnings(state)
    if not warns:
        return False
    line = "!" * 72
    w = warns[0]
    print(f"{line}\n"
          f"ACHTUNG: /config bleibt nicht erhalten!\n  {w['de']}\n"
          f"  Danach den Container neu starten; der Token steht dann einmal im Log und in appdata/nzblab-agent/token.txt.\n"
          f"WARNING: /config is not persistent!\n  {w['en']}\n"
          f"  Then restart the container; the token is shown once in the log and stored in appdata/nzblab-agent/token.txt.\n"
          f"{line}", flush=True)
    return True


CONFIG_MARKER = ".nzblab-agent-config"
# Ab 1.18.0: was der Agent in /config selbst anlegt; nur solche Einträge gelten als «eigener Ordner» (Markierung allein nicht)
OWN_CONFIG_ENTRY = re.compile(r"token\.txt|settings\.json(\.tmp)?|affiliate\.json|prices-[A-Za-z0-9_-]+\.json|"
                              r"(history|files|devices)\.db(-wal|-shm|-journal)?|crashes|" + re.escape(CONFIG_MARKER))
PRIV_STATE = {"root_reason": ""}      # ab 1.18.0: warum der Agent als root weiterläuft (leer = nicht root oder gewollt)


def privilege_warnings():
    """Ab 1.18.0 (Audit N2): läuft der Agent nur als Notlösung als root, steht das in /api/info (warnings) statt nur im Log."""
    r = PRIV_STATE["root_reason"]
    if not r:
        return []
    return [{"code": "running_as_root",
             "de": f"Der Agent läuft als root weiter, weil {r[0]}. Lösung: /config auf einen eigenen Ordner legen, der Benutzer PUID gehört "
                   "(z. B. appdata/nzblab-agent).",
             "en": f"The agent keeps running as root because {r[1]}. Fix: map /config to its own folder that user PUID can write "
                   "(e.g. appdata/nzblab-agent)."}]


def config_is_own(path):
    """Ab 1.18.0 (Audit M1): ist /config ein eigener Ordner des Agents? Ja, wenn auf der obersten Ebene
    nur Einträge liegen, die der Agent selbst anlegt (leer zählt auch). Sonst: (False, fremde Einträge)."""
    try:
        names = os.listdir(path)
    except OSError:
        return False, []
    # Nach-Audit 1.18.0: die Markierung allein reicht nicht mehr. Sonst bliebe ein Ordner, der beim ersten Start leer war
    # (z. B. /config versehentlich auf ein leeres appdata), für immer «eigen», und spätere fremde Dateien würden umbesitzt.
    foreign = sorted(n for n in names if not OWN_CONFIG_ENTRY.fullmatch(n))
    return not foreign, foreign


def _chown_tree(top, uid, gid):
    if not os.path.isdir(top) or os.path.islink(top):      # einzelne Datei (os.walk liefert für sie nichts)
        try:
            os.lchown(top, uid, gid)
        except OSError:
            pass
        return
    for root, dirs, files in os.walk(top):
        for name in [root] + [os.path.join(root, x) for x in dirs + files]:
            try:
                if os.lstat(name).st_uid != uid:
                    os.lchown(name, uid, gid)
            except OSError:
                pass


def drop_privileges():
    """Im eigenen Image (Dockerfile setzt PUID=99, PGID=100 = Unraids nobody:users) als normaler Benutzer weiterlaufen.
    Vorher /config diesem Benutzer geben, damit Verlauf und Token schreibbar bleiben. PUID=0 oder ohne PUID (altes Setup im
    offiziellen Python-Image): bleibt root. Host-Pfade sind nur lesend eingebunden; root braucht es nur für Container-Namen
    (/var/lib/docker/containers ist 600) und appdata-Ordner anderer Container mit engen Rechten.
    Ab 1.18.0 (Audit M1): rekursiv umbesitzt wird nur ein eigener oder leerer /config-Ordner; liegen fremde Dateien darin
    (falsch gesetzter Pfad, z. B. ganzes appdata), bekommen nur die eigenen Einträge den Besitzer, dazu eine laute Warnung."""
    PRIV_STATE["root_reason"] = ""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    try:
        uid, gid = int(os.environ.get("PUID", "")), int(os.environ.get("PGID", os.environ.get("PUID", "")))
    except ValueError:
        return None
    if uid == 0:
        return None
    own, foreign = config_is_own(CONFIG)
    if own:
        try:
            mk = os.path.join(CONFIG, CONFIG_MARKER)
            if not os.path.lexists(mk):
                with open(mk, "w") as f:
                    f.write("NZBLab-Agent: dieser Ordner gehört dem Agenten (Besitzer-Wechsel erlaubt).\n")
        except OSError:
            pass
        _chown_tree(CONFIG, uid, gid)
    else:
        print("!" * 72 + "\nWARNUNG: /config enthält fremde Dateien (z. B. " + ", ".join(foreign[:3]) + "). Das sieht nach einem zu grossen Ordner aus "
              "(z. B. ganzes appdata). Der Agent ändert NICHTS an fremden Dateien und besitzt nur seine eigenen Einträge um.\n"
              "Lösung: /config auf einen eigenen Ordner legen, z. B. /mnt/user/appdata/nzblab-agent.\n"
              "WARNING: /config contains files that are not the agent's. The agent will not touch them, only its own entries.\n"
              "Fix: map /config to its own folder, e.g. /mnt/user/appdata/nzblab-agent.\n" + "!" * 72, flush=True)
        try:
            entries = os.listdir(CONFIG)
        except OSError:
            entries = []                       # Nach-Audit 1.18.0: nicht lesbar → kein Absturz beim Start, unten root-Hinweis
        for n in entries:
            if OWN_CONFIG_ENTRY.fullmatch(n):
                _chown_tree(os.path.join(CONFIG, n), uid, gid)
    # Nur wechseln, wenn der Benutzer /config danach sicher erreicht und beschreiben kann (z. B. NFS mit root_squash
    # oder ACLs verhindern chown). Sonst lieber als root weiter als in einer Absturzschleife.
    try:
        st = os.stat(CONFIG)
        ok = (st.st_uid == uid and st.st_mode & 0o700 == 0o700) or bool(st.st_mode & 0o002) or (st.st_gid == gid and st.st_mode & 0o070 == 0o070)
        d = os.path.dirname(os.path.abspath(CONFIG))
        while ok and d not in ("", "/"):
            m = os.stat(d)
            ok = bool(m.st_mode & 0o001 or (m.st_uid == uid and m.st_mode & 0o100) or (m.st_gid == gid and m.st_mode & 0o010))
            d = os.path.dirname(d)
    except OSError:
        ok = False
    if not ok:
        de = f"{CONFIG} gehört nicht Benutzer {uid} oder ist für ihn nicht erreichbar" + ("" if own else " (fremde Dateien darin)")
        en = f"{CONFIG} is not owned by user {uid} or not reachable for it" + ("" if own else " (foreign files inside)")
        PRIV_STATE["root_reason"] = (de, en)
        print(f"WARNUNG: {de}; der Agent läuft als root weiter (Host-Pfade bleiben nur lesend). / WARNING: {en}; the agent keeps running as root "
              "(host paths stay read-only).", flush=True)
        return None
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)
    return uid, gid


# ---------------------------------------------------------------- Plattenpreise

_PRICES_FOUND = {"path": "", "checked": None}


def prices_path(now=None):
    """
    Optionale externe prices.json (gleiches Format wie /config/prices-CH.json, z. B. von einem eigenen Preis-Sammler).
    PRICES_FILE gesetzt: genau diese Datei. Sonst die erste Freigabe unter FILES_ROOT (alphabetisch), die
    «nzblab-prices/prices.json» enthält. Gesucht wird höchstens alle 6 Stunden (nur Verzeichnis-Einträge, nur lesen).
    """
    if PRICES_FILE:
        return PRICES_FILE
    now = time.time() if now is None else now
    if _PRICES_FOUND["path"] and os.path.isfile(_PRICES_FOUND["path"]):
        return _PRICES_FOUND["path"]
    if _PRICES_FOUND["checked"] is None or now - _PRICES_FOUND["checked"] >= 6 * 3600 or _PRICES_FOUND["path"]:
        _PRICES_FOUND["checked"], found = now, ""
        try:
            for share in sorted(os.listdir(FILES_ROOT)):
                p = os.path.join(FILES_ROOT, share, "nzblab-prices", "prices.json")
                if not share.startswith(".") and os.path.isfile(p):
                    found = p
                    break
        except OSError:
            pass
        _PRICES_FOUND["path"] = found
    return _PRICES_FOUND["path"] or os.path.join(FILES_ROOT, "nzblab-prices", "prices.json")


# Plattenserien für NAS, die man in der App zusätzlich beobachten kann (Grösse per Regler). must/not: Wörter im Produktnamen.
PRICE_SERIES = [
    {"id": "ironwolf", "label": "Seagate IronWolf", "q": "Seagate IronWolf", "must": ["ironwolf"], "not": ["pro"], "sizes": [4, 6, 8, 10, 12], "kind": "hdd"},
    {"id": "ironwolfpro", "label": "Seagate IronWolf Pro", "q": "Seagate IronWolf Pro", "must": ["ironwolf", "pro"], "not": [], "sizes": [4, 8, 12, 16, 18, 20, 22, 24, 26, 28, 30], "kind": "hdd"},
    {"id": "exos", "label": "Seagate Exos", "q": "Seagate Exos", "must": ["exos"], "not": [], "sizes": [4, 8, 10, 12, 14, 16, 18, 20, 24, 26, 28, 30, 32], "kind": "hdd"},
    {"id": "redplus", "label": "WD Red Plus", "q": "WD Red Plus", "must": ["red plus"], "not": [], "sizes": [4, 6, 8, 10, 12, 14], "kind": "hdd"},
    {"id": "redpro", "label": "WD Red Pro", "q": "WD Red Pro", "must": ["red pro"], "not": [], "sizes": [4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28], "kind": "hdd"},
    {"id": "ultrastar", "label": "WD Ultrastar", "q": "WD Ultrastar", "must": ["ultrastar"], "not": [], "sizes": [8, 12, 14, 16, 18, 20, 22, 24, 26, 28, 32], "kind": "hdd"},
    {"id": "n300", "label": "Toshiba N300", "q": "Toshiba N300", "must": ["n300"], "not": [], "sizes": [4, 6, 8, 10, 12, 14, 16, 18], "kind": "hdd"},
    {"id": "toshibamg", "label": "Toshiba MG (Enterprise)", "q": "Toshiba MG", "must": ["toshiba", "enterprise"], "not": ["surveillance"], "sizes": [4, 8, 12, 14, 16, 18, 20, 22, 24], "kind": "hdd"},
    {"id": "sn700", "label": "WD Red SN700 (NVMe für NAS)", "q": "WD Red SN700", "must": ["sn700"], "not": [], "sizes": [1, 2, 4], "kind": "nvme"},
    {"id": "990pro", "label": "Samsung 990 PRO (NVMe)", "q": "Samsung 990 PRO", "must": ["990 pro"], "not": ["heatsink"], "sizes": [1, 2, 4], "kind": "nvme"},
]


# «Alle NAS-Platten» einer Grösse: statt jede Serie einzeln mit 28 TB zu wählen, eine
# Beobachtung über alle NAS-/Enterprise-Festplatten der Liste; das günstigste Angebot gewinnt, egal welche Serie.
_HDD = [x for x in PRICE_SERIES if x["kind"] == "hdd"]
PRICE_SERIES.insert(0, {"id": "allnas", "label": "Alle NAS-Platten", "q": "HDD intern", "must": [], "not": [],
                        "any": [{"must": x["must"], "not": x["not"], "rx": x.get("rx", "")} for x in _HDD],
                        "sizes": sorted({t for x in _HDD for t in x["sizes"]}), "kind": "hdd"})


def price_watchlist():
    """Was in der App zusätzlich beobachtet wird, fertig aufgelöst für einen externen Preis-Sammler (Suche, Wörter, Grösse)."""
    series = {x["id"]: x for x in PRICE_SERIES}
    out = []
    for w in settings().get("price_watch") or []:
        sr = series.get(w.get("series"))
        tb = w.get("tb")
        if not sr or tb not in sr["sizes"]:
            continue
        q = f"{tb}TB {sr['q']}" if sr.get("any") else f"{sr['q']} {tb}TB"
        entry = {"key": f"w_{sr['id']}_{tb}", "label": f"{sr['label']} {tb} TB", "q": q, "tb": tb,
                 "must": sr["must"], "not": sr["not"], "rx": sr.get("rx", ""), "series": sr["id"], "kind": sr["kind"], "watch": True}
        if sr.get("any"):
            entry["any"] = sr["any"]
        out.append(entry)
    return out


# ---------------------------------------------------------------- Eigene Preisabfrage (ab 1.10.0)
#
# Der Agent holt die Preise selbst, einmal am Tag, für die Plattengrössen in diesem NAS (aus /sys/block) und die in der
# App beobachteten Serien. Quelle nach Land (Einstellung «price_country» aus der App, sonst Variable PRICE_COUNTRY):
#   CH → Toppreise.ch · DE → Geizhals.de · AT → Geizhals.at · alle anderen → Amazon-Preise über diskprices.com
# Höflich: eine Anfrage pro Grösse und Tag (diskprices: eine pro Tag für alle Grössen), ehrlicher User-Agent, Timeouts,
# Pausen zwischen den Anfragen. Lässt sich eine Seite nicht lesen (gesperrt, Prüfseite, neues Layout), gibt es keine
# Zahl, sondern «Preise gerade nicht lesbar» und den Link, mit dem man selbst im Browser nachschaut. Nie erfundene Werte.

PRICE_UA = f"NZBLab-Agent/{VERSION} (home NAS disk price check; each search at most once per day)"
PRICE_TIMEOUT = 20
PRICE_PAUSE = float(os.environ.get("PRICE_PAUSE_S", "5"))
PRICE_KICK = threading.Event()          # Land neu gesetzt: sofort holen statt erst beim nächsten Rundgang
UNREADABLE = "Preise gerade nicht lesbar"

# diskprices.com: Land → (Locale, Währung). Bekannte Locales von diskprices.com.
DISKPRICES = {"US": ("us", "USD"), "GB": ("uk", "GBP"), "UK": ("uk", "GBP"), "FR": ("fr", "EUR"), "IT": ("it", "EUR"),
              "ES": ("es", "EUR"), "CA": ("ca", "CAD"), "AU": ("au", "AUD"), "SE": ("se", "SEK"), "IE": ("ie", "EUR"),
              "IN": ("in", "INR")}
# Länder ohne eigenen Amazon-Markt bei diskprices: nächster passender (Währung = die des Marktes, nicht des Landes)
DISKPRICES_NEAR = {"NL": "DE", "BE": "FR", "LU": "FR", "PL": "DE", "CZ": "DE", "DK": "SE", "NO": "SE", "FI": "SE",
                   "PT": "ES", "NZ": "AU", "LI": "CH", "MX": "US"}
PRICE_COUNTRIES = [("CH", "Schweiz", "Toppreise.ch"), ("DE", "Deutschland", "Geizhals.de"), ("AT", "Österreich", "Geizhals.at"),
                   ("US", "USA", "Amazon (diskprices.com)"), ("GB", "Grossbritannien", "Amazon (diskprices.com)"),
                   ("FR", "Frankreich", "Amazon (diskprices.com)"), ("IT", "Italien", "Amazon (diskprices.com)"),
                   ("ES", "Spanien", "Amazon (diskprices.com)"), ("CA", "Kanada", "Amazon (diskprices.com)"),
                   ("AU", "Australien", "Amazon (diskprices.com)"), ("SE", "Schweden", "Amazon (diskprices.com)"),
                   ("IE", "Irland", "Amazon (diskprices.com)"), ("IN", "Indien", "Amazon (diskprices.com)")]
INTERN_NOT = r"(?i)extern|My Book|Expansion|Elements|2big|Dock|Gehäuse|enclosure|NAS-Server|Bundle|\bDS\d{3,4}|\bRS\d{3,4}|\bTS-\d{3}|DXP\d|DiskStation|refurb|recertified|generalüberholt|gebraucht|used"


def price_country():
    """Land für die Preise: aus der App (settings.json), sonst PRICE_COUNTRY, sonst leer (= noch nicht gewählt)."""
    c = (settings().get("price_country") or os.environ.get("PRICE_COUNTRY", "")).strip().upper()
    return c if re.fullmatch(r"[A-Z]{2}", c) else ""


def price_source(country):
    """Quelle für ein Land: {id, label, currency, locale}."""
    if country == "CH" or country == "LI":
        return {"id": "toppreise", "label": "Toppreise.ch", "currency": "CHF"}
    if country in ("DE", "AT"):
        return {"id": "geizhals", "label": f"Geizhals.{country.lower()}", "currency": "EUR", "base": f"https://geizhals.{country.lower()}"}
    near = DISKPRICES_NEAR.get(country, country)
    if near in ("DE",):
        return {"id": "geizhals", "label": "Geizhals.de", "currency": "EUR", "base": "https://geizhals.de", "fallback": True}
    loc, cur = DISKPRICES.get(near, DISKPRICES["US"])
    return {"id": "diskprices", "label": f"Amazon.{_amazon_tld(loc)} (diskprices.com)", "currency": cur, "locale": loc,
            "fallback": near not in DISKPRICES or near != country}


def _amazon_tld(loc):
    return {"us": "com", "uk": "co.uk", "au": "com.au", "in": "in"}.get(loc, loc)


def price_search_url(src, tb, kind, q=None):
    """Suche, die man auch selbst im Browser öffnen kann (Knopf «Im Browser suchen»)."""
    from urllib.parse import quote_plus
    t = f"{tb:g}"
    if src["id"] == "toppreise":
        q = q or (f"{t}TB NVMe SSD M.2" if kind == "nvme" else f"{t}TB SSD intern" if kind == "ssd" else f"{t}TB HDD intern")
        return f"https://www.toppreise.ch/produktsuche?q={quote_plus(q)}&o=preis_pro_einheit"
    if src["id"] == "geizhals":
        q = q or f"{t}TB"
        cat = "hde7s" if kind == "hdd" else "hdssd"
        return f"{src['base']}/?fs={quote_plus(q)}&cat={cat}&sort=p"
    return f"https://diskprices.com/?locale={src['locale']}&condition=new"


# ---------------------------------------------------------------- Quellen und Werbelinks (Plattenpreise P1)
#
# Jede Preisquelle steht hier mit Name, Seite und Stand der Erlaubnis. «allowed» False (oder Eintrag in der Einstellung
# «price_sources_off» bzw. Variable PRICE_SOURCES_OFF, kommagetrennte Quellen-IDs) = die Quelle wird nicht mehr abgefragt;
# die App zeigt dann nur noch den Suchlink fürs Browser-Suchen, keine Zahl. «terms»: pending = schriftliche Erlaubnis
# angefragt oder noch offen (Plan Plattenpreise weltweit, Paket 0). «amazon»: Amazon-Preise bekommen nie einen Werbelink
# und keine «günstig»-Meldung (Regeln des Partnerprogramms: keine Preisverfolgung/-alarme).
PRICE_SOURCES = {
    "toppreise": {"label": "Toppreise.ch", "url": "https://www.toppreise.ch", "allowed": True, "terms": "pending", "amazon": False},
    "geizhals": {"label": "Geizhals", "url": "https://geizhals.de", "allowed": True, "terms": "pending", "amazon": False},
    "diskprices": {"label": "diskprices.com (Amazon-Preise)", "url": "https://diskprices.com", "allowed": True, "terms": "pending",
                   "amazon": True},
}


def source_allowed(sid):
    """Darf die Quelle abgefragt werden? Standard aus PRICE_SOURCES, abschaltbar in den Einstellungen und per Variable."""
    entry = PRICE_SOURCES.get(sid)
    if not entry or not entry.get("allowed", True):
        return False
    off = settings().get("price_sources_off")
    off = {str(x) for x in off} if isinstance(off, list) else set()
    off |= {x.strip() for x in os.environ.get("PRICE_SOURCES_OFF", "").split(",") if x.strip()}
    return sid not in off


def price_source_info(src):
    """Eine Quelle für /api/prices: {id, label, url, allowed, terms, amazon}."""
    e = PRICE_SOURCES.get(src["id"], {})
    url = src.get("base") or e.get("url", "")
    label = src["label"]
    return {"id": src["id"], "label": label, "url": url, "allowed": source_allowed(src["id"]),
            "terms": e.get("terms", "pending"), "amazon": bool(e.get("amazon"))}


# Partnerprogramme (Werbelinks). Leer = kein Werbelink, nur der normale Link. Die Kennungen sind öffentlich (stehen in jedem
# Werbelink) und kommen aus CONFIG/affiliate.json: {"galaxus": {"id": "…", "program": "…"}, "awin": {"id": "…", "mid": "…"}}.
# Amazon steht bewusst nicht hier und wird auch mit Eintrag nie verlinkt (AMAZON_HOST).
AFFILIATE = {
    "galaxus": {"label": "Galaxus / Digitec", "hosts": ("www.galaxus.ch", "www.digitec.ch", "www.galaxus.de", "www.galaxus.at",
                                                       "www.galaxus.fr", "www.galaxus.it"),
                "template": "https://clk.tradedoubler.com/click?p={program}&a={id}&url={url}", "need": ("id", "program")},
    "awin": {"label": "Awin-Shops", "hosts": ("www.notebooksbilliger.de", "www.reichelt.de"),
             "template": "https://www.awin1.com/cread.php?awinmid={mid}&awinaffid={id}&ued={url}", "need": ("id", "mid")},
}
AMAZON_HOST = re.compile(r"(?i)(^|\.)(amazon\.[a-z.]+|amzn\.[a-z]+|a\.co)$")


def affiliate_ids():
    try:
        with open(os.path.join(CONFIG, "affiliate.json")) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def affiliate_url(link, resolve=None):
    """Werbelink zu [link] oder "": nur für https-Links auf Shops mit eingetragener Kennung, nie für Amazon, nur öffentliche Ziele."""
    from urllib.parse import quote, urlparse
    try:
        host = (urlparse(link or "").hostname or "").lower()
    except ValueError:
        return ""
    if not host or AMAZON_HOST.search(host):
        return ""
    ids = affiliate_ids()
    if not ids:
        return ""                      # ohne Partnerprogramm nichts tun (auch keine DNS-Abfrage)
    for key, prog in AFFILIATE.items():
        cfg = ids.get(key)
        if host not in prog["hosts"] or not isinstance(cfg, dict):
            continue
        vals = {k: str(cfg.get(k) or "").strip() for k in prog["need"]}
        if not all(vals.values()) or not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", v) for v in vals.values()):
            continue
        if not public_https_url(link, resolve):
            return ""
        return prog["template"].format(url=quote(link, safe=""), **{k: quote(v, safe="") for k, v in vals.items()})
    return ""


def with_ad(offer, resolve=None):
    """Angebot um «ad_link» ergänzen (nur wenn es einen Werbelink gibt); der normale «link» bleibt unverändert."""
    if isinstance(offer, dict):
        ad = affiliate_url(offer.get("link", ""), resolve)
        if ad:
            return dict(offer, ad_link=ad)
    return offer


def ads_available():
    """Gibt es überhaupt ein Partnerprogramm mit Kennung? Dann zeigt die App den Schalter «Werbelinks zeigen»."""
    ids = affiliate_ids()
    return any(isinstance(ids.get(k), dict) and all(str(ids[k].get(n) or "").strip() for n in p["need"]) for k, p in AFFILIATE.items())


# ---------------------------------------------------------------- Plattenmodelle (ab 1.12.0)
#
# Preise gelten für die Platten, die wirklich im NAS stecken: Modell aus sysfs, daraus Serie, Teilenummer und Garantie.
# Quellen für die Zuordnung (geprüft 30.10.2026):
#   Seagate ST<Kapazität GB><Familie>…: DM BarraCuda, LM BarraCuda 2.5", DX FireCuda, VN IronWolf, NE/NT IronWolf Pro,
#     VX SkyHawk, VE SkyHawk AI, NM Exos  (https://datacenterdisk.com/guides/seagate-model-numbers)
#   WD …EFAX Red (SMR), EFRX/EFZX/EFPX Red Plus, FFBX/KFBX/KFGX Red Pro, PURZ/PURX Purple, PURP Purple Pro,
#     EZEX/EZBX/EZAZ/EZRZ/EZAX Blue, FZEX/FZBX Black  (https://datacenterdisk.com/guides/wd-toshiba-model-numbers)
#   Toshiba HDWG N300, HDWR X300, HDWD P300, MG… Enterprise (gleiche Quelle); HDWT S300 Surveillance
#     (https://toshiba.semicon-storage.com/ap-en/storage/product/internal-specialty/surveillance/articles/s300.html)
#   Garantie: IronWolf Pro 5 J. (Seagate-Support, ST28000NT000), IronWolf 3 / Exos 5 / BarraCuda 2 (Seagate-Angaben laut
#     storagereview.com und Händlern), Red Plus 3 (westerndigital.com, WD40EFPX), Red Pro 5 (datacenterdisk), Ultrastar 5
#     (Händler, WUH722020BLE6L4), N300 3, MG 5 (Toshiba-Datenblatt bzw. Händler), Samsung 990 PRO 5.
# Nicht sicher Belegtes bekommt keine Garantie-Angabe. Unbekannte Modelle: Suche nach dem Modellnamen selbst.

MODEL_SERIES = {
    "ironwolf":    {"label": "Seagate IronWolf", "q": "Seagate IronWolf", "must": ["ironwolf"], "not": ["pro"], "warranty": 3},
    "ironwolfpro": {"label": "Seagate IronWolf Pro", "q": "Seagate IronWolf Pro", "must": ["ironwolf", "pro"], "not": [], "warranty": 5},
    "exos":        {"label": "Seagate Exos", "q": "Seagate Exos", "must": ["exos"], "not": [], "warranty": 5},
    "barracuda":   {"label": "Seagate BarraCuda", "q": "Seagate BarraCuda", "must": ["barracuda"], "not": ["pro"], "warranty": 2},
    "firecuda":    {"label": "Seagate FireCuda", "q": "Seagate FireCuda", "must": ["firecuda"], "not": []},
    "skyhawk":     {"label": "Seagate SkyHawk", "q": "Seagate SkyHawk", "must": ["skyhawk"], "not": ["skyhawk ai"]},
    "skyhawkai":   {"label": "Seagate SkyHawk AI", "q": "Seagate SkyHawk AI", "must": ["skyhawk ai"], "not": []},
    "red":         {"label": "WD Red", "q": "WD Red", "must": ["red"], "not": ["plus", "pro", "sn700"], "rx": r"(?i)\b(WD|Western Digital)\b"},
    "redplus":     {"label": "WD Red Plus", "q": "WD Red Plus", "must": ["red plus"], "not": [], "warranty": 3},
    "redpro":      {"label": "WD Red Pro", "q": "WD Red Pro", "must": ["red pro"], "not": [], "warranty": 5},
    "purple":      {"label": "WD Purple", "q": "WD Purple", "must": ["purple"], "not": ["purple pro"]},
    "purplepro":   {"label": "WD Purple Pro", "q": "WD Purple Pro", "must": ["purple pro"], "not": []},
    "gold":        {"label": "WD Gold", "q": "WD Gold", "must": ["gold"], "not": [], "rx": r"(?i)\b(WD|Western Digital)\b"},
    "blue":        {"label": "WD Blue", "q": "WD Blue", "must": ["blue"], "not": ["sn5", "sa510"], "rx": r"(?i)\b(WD|Western Digital)\b"},
    "green":       {"label": "WD Green", "q": "WD Green", "must": ["green"], "not": [], "rx": r"(?i)\b(WD|Western Digital)\b"},
    "black":       {"label": "WD Black", "q": "WD Black", "must": ["black"], "not": ["sn"], "rx": r"(?i)\b(WD|Western Digital)\b"},
    "ultrastar":   {"label": "WD Ultrastar", "q": "WD Ultrastar", "must": ["ultrastar"], "not": [], "warranty": 5},
    "n300":        {"label": "Toshiba N300", "q": "Toshiba N300", "must": ["n300"], "not": [], "warranty": 3},
    "x300":        {"label": "Toshiba X300", "q": "Toshiba X300", "must": ["x300"], "not": []},
    "s300":        {"label": "Toshiba S300", "q": "Toshiba S300", "must": ["s300"], "not": []},
    "p300":        {"label": "Toshiba P300", "q": "Toshiba P300", "must": ["p300"], "not": []},
    "toshibamg":   {"label": "Toshiba MG", "q": "Toshiba MG", "must": ["toshiba"], "not": [], "rx": r"(?i)\bMG\d{2}", "warranty": 5},
    # SSDs: Name und Grösse bestimmen das Produkt eindeutig («exact_name»), eine Teilenummer steht nicht in sysfs
    "990pro":      {"label": "Samsung 990 PRO", "q": "Samsung 990 PRO", "must": ["990 pro"], "not": ["heatsink"], "exact_name": True,
                    "warranty": 5},   # 5 Jahre oder TBW (Samsung-Datenblatt, techpowerup.com/review/samsung-990-pro-2-tb)
    "990evo":      {"label": "Samsung 990 EVO", "q": "Samsung 990 EVO", "must": ["990 evo"], "not": [], "exact_name": True},
    "980pro":      {"label": "Samsung 980 PRO", "q": "Samsung 980 PRO", "must": ["980 pro"], "not": ["heatsink"], "exact_name": True},
    "970evoplus":  {"label": "Samsung 970 EVO Plus", "q": "Samsung 970 EVO Plus", "must": ["970 evo plus"], "not": [], "exact_name": True},
    "870evo":      {"label": "Samsung 870 EVO", "q": "Samsung 870 EVO", "must": ["870 evo"], "not": [], "exact_name": True},
    "870qvo":      {"label": "Samsung 870 QVO", "q": "Samsung 870 QVO", "must": ["870 qvo"], "not": [], "exact_name": True},
    "sn700":       {"label": "WD Red SN700", "q": "WD Red SN700", "must": ["sn700"], "not": [], "exact_name": True},
    "sn850x":      {"label": "WD Black SN850X", "q": "WD Black SN850X", "must": ["sn850x"], "not": ["heatsink"], "exact_name": True},
    "sn770":       {"label": "WD Black SN770", "q": "WD Black SN770", "must": ["sn770"], "not": [], "exact_name": True},
    "crucialp3":   {"label": "Crucial P3", "q": "Crucial P3", "must": ["crucial", "p3"], "not": ["p3 plus"]},
    "crucialp3plus": {"label": "Crucial P3 Plus", "q": "Crucial P3 Plus", "must": ["crucial", "p3 plus"], "not": []},
    "mx500":       {"label": "Crucial MX500", "q": "Crucial MX500", "must": ["mx500"], "not": []},
    "kc3000":      {"label": "Kingston KC3000", "q": "Kingston KC3000", "must": ["kc3000"], "not": []},
    "nv2":         {"label": "Kingston NV2", "q": "Kingston NV2", "must": ["nv2"], "not": []},
}

_SEAGATE_FAMILY = {"NT": "ironwolfpro", "NE": "ironwolfpro", "VN": "ironwolf", "NM": "exos", "DM": "barracuda",
                   "LM": "barracuda", "DX": "firecuda", "VX": "skyhawk", "VE": "skyhawkai"}
_WD_SUFFIX = {"EFAX": "red", "EFRX": "redplus", "EFZX": "redplus", "EFPX": "redplus", "FFBX": "redpro", "KFBX": "redpro",
              "KFGX": "redpro", "PURZ": "purple", "PURX": "purple", "PURP": "purplepro", "EZEX": "blue", "EZBX": "blue",
              "EZAZ": "blue", "EZRZ": "blue", "EZAX": "blue", "FZEX": "black", "FZBX": "black"}
# Name enthält … → Serie (Reihenfolge zählt: «990 PRO» vor «990»)
_NAME_SERIES = [(r"990\s*PRO", "990pro"), (r"990\s*EVO", "990evo"), (r"980\s*PRO", "980pro"), (r"970\s*EVO\s*Plus", "970evoplus"),
                (r"870\s*EVO", "870evo"), (r"870\s*QVO", "870qvo"), (r"SN700", "sn700"), (r"SN850X", "sn850x"), (r"SN770", "sn770"),
                (r"^CT\d+P3PSSD", "crucialp3plus"), (r"^CT\d+P3SSD", "crucialp3"), (r"^CT\d+MX500", "mx500"),
                (r"SKC3000", "kc3000"), (r"SNV2S", "nv2")]


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def model_info(raw, tb, kind="hdd"):
    """
    Modellname aus sysfs → {raw, part, series, label, warranty_years, exact_name}. «ST28000NT000-4AB103» →
    Seagate IronWolf Pro, Teilenummer ST28000NT000. Unbekannt: series leer, part = erstes Wort des Modellnamens.
    """
    raw = re.sub(r"\s+", " ", (raw or "")).strip()
    if not raw:
        return None
    t = f"{tb:g}"
    words = [w for w in re.sub(r"^(WDC|TOSHIBA|HGST|SEAGATE|ATA)\s+", "", raw, flags=re.I).split(" ") if w]
    first = words[0].split("-")[0].upper() if words else ""
    series, part = "", ""
    m = re.match(r"^ST\d{3,5}([A-Z]{2})[A-Z0-9]{3,4}$", first)
    if m:
        series, part = _SEAGATE_FAMILY.get(m.group(1), ""), first
    elif re.match(r"^WD\d{2,4}[A-Z]{4}$", first):
        series, part = _WD_SUFFIX.get(first[-4:], ""), first
        if first[-4:] in ("FRYZ", "KRYZ"):   # WD Gold (WD4003FRYZ, WD181KRYZ)
            series = "gold"
        elif first.endswith("EZRX"):           # WD Green (Caviar Green, z. B. WD30EZRX; nicht mehr im Handel)
            series = "green"
        elif re.match(r"^WD\d+(EFBX|EFGX|EFZZ)$", first):   # grössere Red Plus (WD101EFBX, WD140EFGX, WD80EFZZ)
            series = "redplus"
    elif re.match(r"^(WUH|HUH|HUS|WUS)\d{6}", first):
        series, part = "ultrastar", first
    elif re.match(r"^HDW[A-Z]\d", first):
        series, part = {"G": "n300", "N": "n300", "Q": "n300", "R": "x300", "E": "x300", "F": "x300",
                        "T": "s300", "D": "p300"}.get(first[3], ""), first
    elif re.match(r"^MG\d{2}[A-Z]{3}", first):
        series, part = "toshibamg", first
    if not series:
        for rx, sid in _NAME_SERIES:
            if re.search(rx, raw, re.I) or re.search(rx, first):
                series = sid
                break
        if series and re.match(r"^(CT|SKC|SNV)\w+$", first):
            part = first                              # Crucial/Kingston: der Name ist die Teilenummer
    sr = MODEL_SERIES.get(series)
    if not part and not sr:
        part = first if re.search(r"\d", first) and len(first) >= 5 else raw   # unbekannt: so suchen, wie es im Gerät steht
    return {"raw": raw, "part": part, "series": series if sr else "",
            "label": f"{sr['label']} {t} TB" if sr else f"{raw} ({t} TB)",
            "warranty_years": (sr or {}).get("warranty"), "exact_name": bool(sr and sr.get("exact_name") and not part)}


def own_match(name, m, tb=None):
    """Passt ein Angebot zum eigenen Modell? «exact» (Teilenummer im Namen, bei SSDs Serie + Grösse), «series» (gleiche
    Serie und Grösse, andere oder fehlende Teilenummer) oder None (anderes Modell, z. B. Exos statt IronWolf Pro)."""
    if not m or not name:
        return None
    if tb is not None and not _size_ok(name, tb):
        return None
    if m.get("part") and len(_norm(m["part"])) >= 5 and _norm(m["part"]) in _norm(name):
        return "exact"
    sr = MODEL_SERIES.get(m.get("series") or "")
    if sr and series_match(name, sr):
        return "exact" if m.get("exact_name") else "series"
    return None


def offer_model(name, tb):
    """Kurzer Modellname eines fremden Angebots («SEAGATE Exos M, 28 TB (ST28000NM003K)» → «Seagate Exos M 28 TB»)."""
    for sid, sr in MODEL_SERIES.items():
        if sr.get("exact_name"):
            continue
        if series_match(name, sr) and not (sid == "ironwolf" and "pro" in name.lower()):
            label = sr["label"]
            sub = re.search(r"(?i)\bExos\s+(M|X\d+|E|\d+E\d+)\b", name) if sid == "exos" else None
            if sub:
                label = f"Seagate Exos {sub.group(1).upper()}"
            return f"{label} {tb:g} TB"
    for sid, sr in MODEL_SERIES.items():
        if sr.get("exact_name") and series_match(name, sr):
            return f"{sr['label']} {tb:g} TB"
    return re.sub(r"\s+", " ", name).strip()[:80]


def disk_model(sys_root, dev):
    """
    Modellname einer Platte, nur lesen und nur das Modell (nie die Seriennummer).
    SATA hinter dem SCSI-Treiber: device/model hat höchstens 16 Zeichen («ST28000NT000-4AB»); der volle Name steht in
    der ATA-Information (VPD-Seite 89h: IDENTIFY ab Byte 60, Modell = Wörter 27–46, je zwei Bytes vertauscht).
    NVMe: device/model bzw. /sys/class/nvme/<nvmeX>/model.
    """
    base = os.path.join(sys_root, "block", dev)
    try:
        with open(os.path.join(base, "device", "vpd_pg89"), "rb") as f:
            b = f.read(160)
        if len(b) >= 154 and b[1] == 0x89:
            w = b[114:154]
            full = bytes(x for i in range(0, 40, 2) for x in (w[i + 1], w[i])).decode("ascii", "replace").strip()
            if re.fullmatch(r"[\x20-\x7e]{3,40}", full):
                return full
    except OSError:
        pass
    model = (read(os.path.join(base, "device", "model"), "") or "").strip()
    if not model and dev.startswith("nvme"):
        ctrl = re.match(r"nvme\d+", dev).group(0)
        model = (read(os.path.join(sys_root, "class", "nvme", ctrl, "model"), "") or "").strip()
    vendor = (read(os.path.join(base, "device", "vendor"), "") or "").strip()
    if model and vendor and vendor.upper() not in ("ATA", "NVME") and not model.lower().startswith(vendor.lower()):
        model = f"{vendor} {model}"
    return model


def local_disks(sys_root=None):
    """Platten in diesem NAS aus /sys/block (ohne Scrutiny), ab 0.5 TB: [{dev, tb, kind, model}]."""
    sys_root = sys_root or HOST_SYS
    root = os.path.join(sys_root, "block")
    try:
        devs = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for dev in devs:
        if re.match(r"(loop|ram|zram|dm-|md|sr|nbd|fd|mmcblk)", dev):
            continue
        try:
            size = int(read(os.path.join(root, dev, "size"), "0") or 0) * 512
        except ValueError:
            continue
        if size < 0.5e12:
            continue
        cap = size / 1e12
        tb = int(round(cap)) if cap >= 1 else round(cap, 1)
        rot = read(os.path.join(root, dev, "queue", "rotational"), "1") == "1"
        kind = "nvme" if dev.startswith("nvme") else ("hdd" if rot else "ssd")
        out.append({"dev": dev, "tb": tb, "kind": kind, "model": disk_model(sys_root, dev)})
    return out


def local_disk_groups(sys_root=None):
    """
    Preis-Ziele aus den Platten im NAS: eine Gruppe pro Bauart, Grösse und Modell (mehrere gleiche Platten → ein Ziel).
    Schlüssel wie bisher «hdd28»; gibt es in einer Grösse mehrere Modelle, behält das häufigste (bei Gleichstand die
    kleinere Teilenummer) den alten Schlüssel, die anderen bekommen «hdd3-wd30ezrx». So gelten Preisgrenzen aus der App weiter.
    """
    groups = {}
    for d in local_disks(sys_root):
        mi = model_info(d["model"], d["tb"], d["kind"])
        ident = (mi["part"] or mi["series"] or mi["raw"]) if mi else ""
        base = f"{d['kind']}{d['tb']}"
        g = groups.setdefault((base, _norm(ident)), {
            "base": base, "tb": d["tb"], "kind": d["kind"], "disks": [], "model": mi, "ident": ident,
            "label": mi["label"] if mi else f"{d['tb']} TB " + {"nvme": "NVMe-SSD", "ssd": "SSD", "hdd": "Festplatte"}[d["kind"]]})
        g["disks"].append(d["dev"])
    by_base = {}
    for g in groups.values():
        by_base.setdefault(g["base"], []).append(g)
    out = []
    for base, gs in by_base.items():
        gs.sort(key=lambda g: (-len(g["disks"]), _norm(g["ident"])))
        for i, g in enumerate(gs):
            g["key"] = base if i == 0 else f"{base}-{_norm(g['ident']).lower()[:12]}"
            g.pop("base"); g.pop("ident")
            out.append(g)
    return sorted(out, key=lambda g: (-g["tb"], g["key"]))


# ---------------------------------------------------------------- Linux-Server (ab 1.14.0)
# Debian, Ubuntu und andere Linux-Server ohne eigene NAS-Schnittstelle: Die App liest Übersicht und Speicher hier.
# Sicherheit zuerst: Das Wurzelverzeichnis des Hosts wird NIE eingebunden. Gelesen werden nur /host/proc und /host/sys
# (beide nur lesen), die Datei /etc/os-release (einzeln, nur lesen, nach /host/os-release) und die Ordner, die der Benutzer
# ausdrücklich nur lesend unter /host/fs/<Name> einbindet. Nur dort misst der Agent die Belegung (os.statvfs); alle anderen
# Einhängepunkte stehen ohne Belegung in der Liste, die Grösse kommt dann aus /sys/class/block.
HOST_FS = os.environ.get("HOST_FS", "/host/fs")
HOST_OS_RELEASE = os.environ.get("HOST_OS_RELEASE", "/host/os-release")
DOCKER_ROOT = os.environ.get("DOCKER_ROOT", "/host/docker-root")
# Dateisysteme ohne Platte dahinter (Kernel, Container, Arbeitsspeicher): erscheinen nicht in der Liste
PSEUDO_FS = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "securityfs", "cgroup", "cgroup2", "pstore", "bpf", "debugfs",
             "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "rpc_pipefs", "nsfs", "overlay",
             "squashfs", "ramfs", "efivarfs", "selinuxfs", "fuse.lxcfs", "fuse.gvfsd-fuse", "fuse.portal", "nfsd", "shm", "none",
             "aufs", "zram", "iso9660"}
SKIP_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker", "/var/lib/containerd", "/var/snap", "/boot/efi")


def parse_os_release(text):
    """/etc/os-release: KEY=Wert (mit oder ohne Anführungszeichen) → dict. Kommentare und Fehlzeilen werden übersprungen."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if not re.fullmatch(r"[A-Z0-9_]{1,40}", k):
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        out[k] = v[:200]
    return out


def _cpuinfo(proc_dir):
    """Modell, Kerne (physisch) und Threads aus cpuinfo."""
    model, threads, cores = "", 0, set()
    phys = core = None
    for line in (read(f"{proc_dir}/cpuinfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "processor":
            threads += 1
            phys = core = None
        elif k in ("model name", "Model", "Hardware") and not model:
            model = v
        elif k == "physical id":
            phys = v
        elif k == "core id":
            core = v
            cores.add((phys, core))
    return {"model": re.sub(r"\s+", " ", model)[:120], "threads": threads, "cores": len(cores) or threads}


def _route_networks(proc_dir):
    """Netze je Schnittstelle aus der Routing-Tabelle des Hosts: [(iface, Netz, Maske)] (Zahlen, Hex in Little Endian)."""
    txt = read(f"{proc_dir}/1/net/route") if proc_dir != "/proc" else None
    txt = txt or read(f"{proc_dir}/net/route", "") or ""
    out = []
    for line in txt.splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            dest, mask = int.from_bytes(bytes.fromhex(f[1]), "little"), int.from_bytes(bytes.fromhex(f[7]), "little")
        except ValueError:
            continue
        if mask:
            out.append((f[0], dest, mask))
    return out


def _local_ips(proc_dir):
    """Eigene IPv4-Adressen des Hosts aus fib_trie (Einträge «/32 host LOCAL»), ohne 127.x."""
    txt = read(f"{proc_dir}/1/net/fib_trie") if proc_dir != "/proc" else None
    txt = txt or read(f"{proc_dir}/net/fib_trie", "") or ""
    ips, last = [], None
    for line in txt.splitlines():
        m = re.search(r"\|-- (\d+\.\d+\.\d+\.\d+)$", line)
        if m:
            last = m.group(1)
        elif last and "/32 host LOCAL" in line:
            if not last.startswith("127.") and last not in ips:
                ips.append(last)
            last = None
    return ips


def host_net(proc_dir=None, sys_dir=None):
    """Netzwerk-Schnittstellen des Hosts (ohne Docker-intern): Name, MAC, Zustand, Tempo, IPv4-Adressen."""
    proc_dir, sys_dir = proc_dir or HOST_PROC, sys_dir or HOST_SYS
    base = f"{sys_dir}/class/net"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    nets = _route_networks(proc_dir)
    ips = _local_ips(proc_dir)
    out = []
    for n in names:
        if n.startswith(Sampler.SKIP_IF):
            continue
        sp = read(f"{base}/{n}/speed", "")
        own = []
        for ip in ips:
            v = int(ipaddress.IPv4Address(ip))
            if any(i == n and (v & m) == d for i, d, m in nets):
                own.append(ip)
        out.append({"name": n, "mac": (read(f"{base}/{n}/address", "") or "")[:17], "state": read(f"{base}/{n}/operstate", "") or "",
                    "speed_mbit": int(sp) if sp and sp.lstrip("-").isdigit() and int(sp) > 0 else None, "ips": own})
    return out


def host_info(proc_dir=None, sys_dir=None, os_release=None, now=None):
    """Übersicht für einen Linux-Server: Name, System, Kernel, Betriebszeit, CPU, Last, RAM, Netz. Nur lesen."""
    proc_dir, sys_dir = proc_dir or HOST_PROC, sys_dir or HOST_SYS
    rel = parse_os_release(read(os_release or HOST_OS_RELEASE, "") or "")
    up = (read(f"{proc_dir}/uptime", "") or "").split()
    try:
        uptime = int(float(up[0])) if up else None
    except ValueError:
        uptime = None
    load = (read(f"{proc_dir}/loadavg", "") or "").split()
    try:
        load = [float(x) for x in load[:3]] if len(load) >= 3 else []
    except ValueError:
        load = []
    mem = {}
    for line in (read(f"{proc_dir}/meminfo", "") or "").splitlines():
        k, _, v = line.partition(":")
        if v.split() and v.split()[0].isdigit():
            mem[k] = int(v.split()[0]) * 1024
    tot, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", mem.get("MemFree", 0))
    t = int(now if now is not None else time.time())
    return {
        "hostname": read(f"{proc_dir}/sys/kernel/hostname", "") or socket.gethostname(),
        "os": {"name": rel.get("NAME", ""), "version": rel.get("VERSION", rel.get("VERSION_ID", "")),
               "id": rel.get("ID", ""), "pretty": rel.get("PRETTY_NAME", ""), "available": bool(rel)},
        "kernel": read(f"{proc_dir}/sys/kernel/osrelease", "") or "",
        "uptime_s": uptime, "boot_time": t - uptime if uptime is not None else None,
        "cpu": _cpuinfo(proc_dir), "load": load,
        "mem": {"total": tot, "available": avail, "used": max(0, tot - avail), "cache": mem.get("Cached", 0),
                "swap_total": mem.get("SwapTotal", 0), "swap_used": max(0, mem.get("SwapTotal", 0) - mem.get("SwapFree", 0))},
        "net": host_net(proc_dir, sys_dir),
        "host_proc": proc_dir != "/proc", "host_sys": sys_dir != "/sys",
    }


def _unoctal(s):
    """Leerzeichen und Sonderzeichen in Einhängepunkten (\\040 usw.) zurückwandeln."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def host_mounts(proc_dir=None):
    """
    Einhängepunkte des Hosts: [{device, mount, fstype, dev ("8:17" oder None), root}]. Zuerst mountinfo von PID 1 des
    Hosts (mit Gerätenummer), sonst dessen mounts, sonst die eigenen des Containers (dann «container»: True).
    """
    proc_dir = proc_dir or HOST_PROC
    out = []
    txt = read(f"{proc_dir}/1/mountinfo") if proc_dir != "/proc" else None
    if txt:
        for line in txt.splitlines():
            left, sep, right = line.partition(" - ")
            a, b = left.split(), right.split()
            if not sep or len(a) < 5 or len(b) < 2:
                continue
            out.append({"device": _unoctal(b[1]), "mount": _unoctal(a[4]), "fstype": b[0], "dev": a[2], "root": _unoctal(a[3])})
        return out, False
    txt = read(f"{proc_dir}/1/mounts") if proc_dir != "/proc" else None
    own = not txt
    txt = txt or read("/proc/self/mounts", "") or ""
    for line in txt.splitlines():
        f = line.split()
        if len(f) >= 3:
            out.append({"device": _unoctal(f[0]), "mount": _unoctal(f[1]), "fstype": f[2], "dev": None, "root": "/"})
    return out, own


def block_size(sys_root, device):
    """Grösse eines Blockgeräts in Bytes aus /sys/class/block (/dev/sdb1, /dev/mapper/vg-data, /dev/nvme0n1p2) oder None."""
    if not device.startswith("/dev/"):
        return None
    name = os.path.basename(device)
    base = f"{sys_root}/class/block"
    if device.startswith("/dev/mapper/"):
        try:
            for dm in os.listdir(base):
                if dm.startswith("dm-") and (read(f"{base}/{dm}/dm/name", "") or "") == name:
                    name = dm
                    break
        except OSError:
            return None
    v = read(f"{base}/{name}/size", "")
    return int(v) * 512 if v and v.isdigit() else None


STATVFS_TIMEOUT = float(os.environ.get("STATVFS_TIMEOUT", "2"))
_HUNG = {}                  # Pfad → Faden, der dort noch hängt (z. B. NFS weg): nicht nochmals einen starten
_HUNG_LOCK = threading.Lock()


def _measure(path):
    """Belegung und Gerätenummer eines Ordners: {size, used, free, dev} oder None (gleiche Arbeit wie früher in einem Zug)."""
    try:
        st = os.statvfs(path)
        d = os.stat(path).st_dev
    except OSError:
        return None
    if st.f_blocks <= 0:
        return None
    return {"size": st.f_blocks * st.f_frsize, "used": (st.f_blocks - st.f_bfree) * st.f_frsize, "free": st.f_bavail * st.f_frsize,
            "dev": f"{os.major(d)}:{os.minor(d)}"}


def _statvfs(path, timeout=None):
    """
    Wie _measure, aber höchstens [timeout] Sekunden (Standard STATVFS_TIMEOUT): Ein hängender Netzwerk-Mount unter /host/fs
    blockiert sonst den Arbeits-Faden der Anfrage für immer. Zeitüberschreitung → {"timeout": True}; solange der alte Faden
    dort noch hängt, wird kein neuer gestartet.
    """
    timeout = STATVFS_TIMEOUT if timeout is None else timeout
    with _HUNG_LOCK:
        t = _HUNG.get(path)
        if t is not None and t.is_alive():
            return {"timeout": True}
        _HUNG.pop(path, None)
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", _measure(path)), daemon=True, name="statvfs")
    t.start()
    t.join(timeout)
    if t.is_alive():
        with _HUNG_LOCK:
            _HUNG[path] = t
        return {"timeout": True}
    return box.get("r")


def _dev_of(path):
    try:
        d = os.stat(path).st_dev
        return f"{os.major(d)}:{os.minor(d)}"
    except OSError:
        return None


def disk_temp(sys_root, dev):
    """Temperatur einer Platte in °C, falls der Kernel sie meldet (drivetemp für SATA, nvme-hwmon), sonst None."""
    pats = [f"{sys_root}/block/{dev}/device/hwmon/hwmon*/temp1_input", f"{sys_root}/block/{dev}/device/hwmon*/temp1_input"]
    for p in pats:
        for f in sorted(glob.glob(p)):
            v = read(f, "")
            if v and v.lstrip("-").isdigit() and -40 < int(v) / 1000 < 150:
                return round(int(v) / 1000)
    return None


def host_disks(sys_root=None):
    """Physische Platten aus /sys/block (ohne loop, ram, zram, dm, md, sr): Gerät, Modell, Grösse, drehend, Temperatur."""
    sys_root = sys_root or HOST_SYS
    root = os.path.join(sys_root, "block")
    try:
        devs = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for dev in devs:
        if re.match(r"(loop|ram|zram|dm-|md|sr|nbd|fd)", dev):
            continue
        v = read(os.path.join(root, dev, "size"), "0") or "0"
        size = int(v) * 512 if v.isdigit() else 0
        if size <= 0:
            continue
        rot = read(os.path.join(root, dev, "queue", "rotational"), "") == "1"
        out.append({"dev": dev, "model": disk_model(sys_root, dev), "size": size, "rotational": rot,
                    "kind": "nvme" if dev.startswith("nvme") else ("hdd" if rot else "ssd"),
                    "removable": read(os.path.join(root, dev, "removable"), "0") == "1",
                    "temp": disk_temp(sys_root, dev)})
    return out


def filesystems(proc_dir=None, sys_dir=None, fs_root=None, docker_root=None):
    """
    Dateisysteme des Hosts für die App: Gerät, Einhängepunkt, Typ, Grösse und, nur für ausdrücklich eingebundene Ordner
    (/host/fs/<Name>, Docker-Datenordner), Belegung. Dazu die physischen Platten.
    """
    proc_dir, sys_dir = proc_dir or HOST_PROC, sys_dir or HOST_SYS
    fs_root = fs_root or HOST_FS
    docker_root = docker_root or DOCKER_ROOT
    mounts, own = host_mounts(proc_dir)
    measured = []           # (Name, Pfad im Container)
    try:
        for n in sorted(os.listdir(fs_root)):
            if os.path.isdir(os.path.join(fs_root, n)):
                measured.append((n, os.path.join(fs_root, n)))
    except OSError:
        pass
    if os.path.isdir(docker_root):
        measured.append(("docker", docker_root))
    out, seen = [], set()
    if not own:
        for m in mounts:
            if m["fstype"] in PSEUDO_FS or any(m["mount"] == p or m["mount"].startswith(p + "/") for p in SKIP_MOUNTS) \
                    or (m["mount"], m["device"]) in seen:
                continue
            # Bind-Mounts (root ≠ /) desselben Geräts nur einmal zeigen
            if m["root"] not in ("/", "") and any(o["dev"] == m["dev"] and m["dev"] for o in out):
                continue
            seen.add((m["mount"], m["device"]))
            out.append({"device": m["device"], "mount": m["mount"], "fstype": m["fstype"], "dev": m["dev"],
                        "size": block_size(sys_dir, m["device"]), "used": None, "free": None, "measured": False, "label": ""})
    for name, path in measured:
        u = _statvfs(path)
        if not u:
            continue
        if u.get("timeout"):
            # Antwortet nicht (z. B. Netzwerk-Mount weg): ohne Belegung, aber sichtbar markiert
            out.append({"device": "", "mount": f"/host/fs/{name}" if path.startswith(fs_root) else path, "fstype": "", "dev": None,
                        "size": None, "used": None, "free": None, "measured": False, "label": name, "timeout": True})
            continue
        dev = u.pop("dev", None)
        hit = next((o for o in out if dev and o["dev"] == dev and not o["measured"]), None) or \
            next((o for o in out if dev and o["dev"] == dev), None)
        if hit and not hit["measured"]:
            hit.update(u, measured=True, label=name)
        elif not hit:
            fstype = next((m["fstype"] for m in mounts if dev and m["dev"] == dev), "")
            out.append({"device": "", "mount": f"/host/fs/{name}" if path.startswith(fs_root) else path, "fstype": fstype,
                        "dev": dev, "measured": True, "label": name, **u})
    for o in out:
        o.pop("dev", None)
    return {"filesystems": out, "disks": host_disks(sys_dir), "mounts_from": "container" if own else "host",
            "measured": [n for n, _ in measured], "os_release": os.path.isfile(HOST_OS_RELEASE)}


HOST_CACHE_S = float(os.environ.get("HOST_CACHE_S", "15"))
_HOST_CACHE = {}
_HOST_CACHE_LOCK = threading.Lock()


def cached(name, fn, ttl=None):
    """
    Ergebnis von /api/host bzw. /api/filesystems höchstens alle [ttl] Sekunden neu rechnen (die App fragt alle paar
    Sekunden). Schlüssel enthält die Host-Pfade, damit Tests mit anderen Bäumen nicht das alte Ergebnis bekommen.
    Gleichzeitige Anfragen rechnen nur einmal (Sperre je Schlüssel).
    """
    ttl = HOST_CACHE_S if ttl is None else ttl
    key = (name, HOST_PROC, HOST_SYS, HOST_OS_RELEASE, HOST_FS, DOCKER_ROOT)
    with _HOST_CACHE_LOCK:
        entry = _HOST_CACHE.setdefault(key, {"t": 0.0, "v": None, "lock": threading.Lock()})
    with entry["lock"]:
        if entry["v"] is not None and time.time() - entry["t"] < ttl:
            return entry["v"]
        v = fn()
        entry["v"], entry["t"] = v, time.time()
        return v


PRICE_MAX_BYTES = 4_000_000
PRICE_DEADLINE_S = 60           # ab 1.16.1: ganze Seite höchstens so lange (Timeout gilt sonst nur je Lesevorgang)
PRICE_MAX_REDIRECTS = 5
NAT64_NET = ipaddress.ip_network("64:ff9b::/96")


def public_host(host, resolve=None):
    """True, wenn [host] nur auf öffentliche Adressen zeigt (nicht Heimnetz, Loopback, Link-local, CGNAT, Multicast …)."""
    if not host:
        return False
    resolve = resolve or (lambda h: [i[4][0] for i in socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)])
    try:
        addrs = resolve(host.strip("[]"))
    except (OSError, UnicodeError, ValueError):
        return False
    if not addrs:
        return False
    for x in addrs:
        try:
            ip = ipaddress.ip_address(str(x).split("%", 1)[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        elif ip.version == 6 and ip in NAT64_NET:      # Review: 64:ff9b::/96 trägt eine IPv4 (z. B. 64:ff9b::c0a8:101)
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def public_https_url(url, resolve=None):
    """Nur https:// auf öffentliche Hosts (Preis-Abfragen und ihre Weiterleitungen)."""
    try:
        u = urlparse(url)
        host = u.hostname
    except ValueError:
        return False
    return u.scheme == "https" and not u.username and not u.password and public_host(host, resolve)


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    """Ab 1.16.1 (Audit Restteile): Preis-Seiten dürfen nur auf öffentliche https-Adressen weiterleiten. Vorher folgte urllib
    jeder Weiterleitung, auch auf http://192.168.1.1/… oder http://127.0.0.1:… (Anfrage vom NAS ins Heimnetz, SSRF)."""
    max_redirections = PRICE_MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_https_url(newurl):
            raise urllib.error.URLError("Weiterleitung auf eine nicht öffentliche Adresse blockiert")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_PRICE_OPENER = urllib.request.build_opener(_PublicRedirect)


def _read_capped(r, limit, deadline):
    """Höchstens [limit] Bytes, und nur bis [deadline] (time.monotonic); tröpfelnde Server halten den Abruf nicht ewig fest."""
    buf = bytearray()
    while len(buf) < limit:
        if time.monotonic() > deadline:
            raise TimeoutError("Seite zu langsam")
        chunk = r.read(min(65536, limit - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def http_get(url, lang="en", opener=None):
    """Eine Seite lesen: (Status, HTML). Höchstens 4 MB in 60 Sekunden, ehrlicher User-Agent, kein Cookie, keine Wiederholung.
    Ab 1.16.1 nur https auf öffentliche Hosts, auch bei Weiterleitungen (nie ins Heimnetz)."""
    if not public_https_url(url):
        raise urllib.error.URLError("nur https auf öffentliche Adressen")
    req = urllib.request.Request(url, headers={"User-Agent": PRICE_UA, "Accept": "text/html", "Accept-Language": lang})
    deadline = time.monotonic() + PRICE_DEADLINE_S
    try:
        with (opener or _PRICE_OPENER).open(req, timeout=PRICE_TIMEOUT) as r:
            return r.status, _read_capped(r, PRICE_MAX_BYTES, deadline).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = _read_capped(e, 200_000, deadline).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body


def safe_link(link, fallback):
    """Link aus einer fremden Seite für die App: nur https://, sonst [fallback] (kein javascript:, intent:, http: …)."""
    link = (link or "").strip()
    return link if link.lower().startswith("https://") and len(link) <= 2000 else fallback


CHALLENGE = re.compile(r"(?i)Sichere Verbindung wird überprüft|Aufgrund massiver|cf-challenge|challenge-platform|"
                       r"captcha|Just a moment\.\.\.|Access denied|403 Forbidden|Attention Required")


def page_blocked(status, html):
    """Gesperrt oder Prüfseite statt Inhalt?"""
    return status in (401, 403, 429, 503) or (status == 200 and len(html) < 20000 and bool(CHALLENGE.search(html)))


class _Blocks:
    """Kleiner HTML-Leser (nur Standardbibliothek): sammelt alle Elemente <tag class="… cls …"> mit ihren Texten
    (je mit den Klassen und Tags der umgebenden Elemente und der Nummer der Tabellenzelle) und inneren Elementen."""
    VOID = {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "col", "area", "base", "embed", "param", "track"}

    def __init__(self, tag, cls, html):
        from html.parser import HTMLParser
        outer = self
        self.tag, self.cls, self.blocks, self.cur, self.stack, self.td = tag, cls, [], None, [], -1

        class P(HTMLParser):
            def handle_starttag(self, t, attrs):
                outer._start(t, dict((k, v or "") for k, v in attrs))

            def handle_startendtag(self, t, attrs):
                outer._start(t, dict((k, v or "") for k, v in attrs), void=True)

            def handle_endtag(self, t):
                outer._end(t)

            def handle_data(self, d):
                outer._data(d)
        p = P(convert_charrefs=True)
        p.feed(html)
        p.close()

    def _start(self, t, a, void=False):
        classes = set(a.get("class", "").split())
        if self.cur is None:
            if t == self.tag and self.cls in classes:
                self.cur = {"attrs": a, "texts": [], "els": []}
                self.stack, self.td = [(t, classes)], -1
            return
        if t == "td":
            self.td += 1
        self.cur["els"].append((t, classes, a, self.td))
        if not void and t not in self.VOID:
            self.stack.append((t, classes))

    def _end(self, t):
        if self.cur is None or t not in [x for x, _ in self.stack]:
            return
        while self.stack:
            if self.stack.pop()[0] == t:
                break
        if not self.stack:
            self.blocks.append(self.cur)
            self.cur = None

    def _data(self, d):
        if self.cur is not None and d.strip():
            cls = set().union(*(c for _, c in self.stack))
            self.cur["texts"].append((cls, [x for x, _ in self.stack], d.strip(), self.td))


def _texts(block, cls=None, td=None):
    return [s for c, _, s, i in block["texts"] if (cls is None or cls in c) and (td is None or i == td)]


def _el(block, tag, cls=None):
    return next((a for t, c, a, _ in block["els"] if t == tag and (cls is None or cls in c)), None)


def _size_ok(name, tb):
    return bool(re.search(rf"(?<![\d.]){re.escape(f'{tb:g}')}\s?TB\b", name, re.I))


def parse_toppreise(html, tb):
    """Toppreise-Suche → Angebote {name, price, per_tb, link} (CHF), nur genau diese Grösse.
    Kartenaufbau: div.Plugin_Product, Text «Angebote ab|CHF|1'019.00», «…|36.39|/|TB»."""
    out = []
    for b in _Blocks("div", "Plugin_Product", html).blocks:
        parts = [s for _, _, s, _ in b["texts"]]
        if not parts:
            continue
        txt = "|".join(parts)
        name = " ".join(parts[:parts.index("Produkt bewerten")]) if "Produkt bewerten" in parts else parts[0]
        name = re.sub(r"\s+", " ", name).strip()
        m_price = re.search(r"Angebote? ab\|CHF\|([0-9'’.,]+)", txt) or re.search(r"CHF\|([0-9'’.,]+)", txt)
        if not m_price:
            continue
        price = money(m_price.group(1))
        if not price:
            continue
        m_tb = re.search(r"\|([0-9'’.,]+)\|/\|TB", txt)
        per_tb = money(m_tb.group(1)) if m_tb else price / tb
        if not _size_ok(name, tb) and not _size_ok(txt[:300], tb):
            continue
        a = _el(b, "a")
        link = (a or {}).get("href", "")
        if link.startswith("/"):
            link = "https://www.toppreise.ch" + link
        link = safe_link(link, "https://www.toppreise.ch")
        out.append({"name": name[:120], "price": round(price, 2), "per_tb": round(per_tb, 2), "link": link})
    return out


def parse_toppreise_product(html, tb):
    """
    Toppreise-Produktseite (eine eindeutige Teilenummer wie «ST28000NT000» springt direkt dorthin, live geprüft 30.10.2026):
    og:title «SEAGATE IronWolf Pro NAS HDD + Rescue, 28 TB (ST28000NT000) ab CHF 955.00 bei Toppreise.ch …»,
    og:description «22 Angebote ab CHF 955.00 (Stand: …)», og:url = Link. Kein Produkt dieser Grösse → None.
    """
    import html as htmllib

    def meta(prop):
        m = re.search(rf'<meta\s+property="og:{prop}"\s+content="([^"]*)"', html)
        return htmllib.unescape(m.group(1)) if m else ""
    url = meta("url")
    if "/preisvergleich/" not in url or not url.startswith("https://"):
        return None
    title = meta("title")[:500]       # ab 1.16.1: gekappt und linear geteilt (vorher quadratische Regex bei langen Leerraum-Ketten)
    name = re.sub(r"\s+", " ", re.split(r"\sab CHF\s", title, maxsplit=1)[0]).strip()
    m = re.search(r"Angebote? ab CHF\s*([0-9'’.,]+)", meta("description")) or re.search(r"\sab CHF\s*([0-9'’.,]+)", title)
    price = money(m.group(1)) if m else None
    if not name or not price or not _size_ok(name, tb):
        return None
    return {"name": name[:120], "price": round(price, 2), "per_tb": round(price / tb, 2), "link": url}


def parse_geizhals(html, tb, base="https://geizhals.de"):
    """Geizhals-Liste → Angebote (EUR, günstigster Händler je Produkt). Aufbau: article.galleryview__item mit
    a.galleryview__name-link (title, href), .galleryview__price-link («€ 289,90»), .galleryview__offercount."""
    from urllib.parse import urljoin
    out = []
    for b in _Blocks("article", "galleryview__item", html).blocks:
        a = _el(b, "a", "galleryview__name-link") or {}
        name = re.sub(r"\s+", " ", a.get("title") or " ".join(_texts(b, "galleryview__name-link"))).strip()
        price = money(" ".join(_texts(b, "galleryview__price-link")))
        if not name or not price or not _size_ok(name, tb):
            continue
        offers = " ".join(_texts(b, "galleryview__offercount"))
        out.append({"name": name[:120], "price": round(price, 2), "per_tb": round(price / tb, 2),
                    "link": safe_link(urljoin(base + "/", a.get("href", "")), base), "shop": f"Geizhals ({offers})" if offers else "Geizhals"})
    return out


def parse_diskprices(html):
    """diskprices.com-Tabelle → alle Zeilen {type, gb, price, name, link, condition}. Aufbau: tr.disk mit
    data-product-type, data-capacity (GB), data-condition; Preis in der 3. Zelle, Name/Link in td.name a."""
    rows = []
    for b in _Blocks("tr", "disk", html).blocks:
        a = b["attrs"]
        link_el = _el(b, "a")
        name = " ".join(_texts(b, "name")) or " ".join(t for c, tags, t, i in b["texts"] if "a" in tags)
        price = money(" ".join(_texts(b, td=2)))
        try:
            gb = float(a.get("data-capacity") or 0)
        except ValueError:
            gb = 0
        if not price or not gb or not name:
            continue
        href = (link_el or {}).get("href", "")
        m = re.match(r"(https://www\.amazon\.[a-z.]+/dp/[A-Z0-9]+)", href)
        rows.append({"type": a.get("data-product-type", ""), "gb": gb, "price": price, "name": re.sub(r"\s+", " ", name)[:120],
                     "link": m.group(1) if m else safe_link(href.split("?")[0], "https://diskprices.com"), "condition": a.get("data-condition", "new")})
    return rows


def diskprices_pick(rows, tb, kind, q=None):
    """Passende Zeilen für eine Grösse: neu, richtige Bauart, Kapazität ±1 GB, nicht extern/refurbished."""
    types = {"hdd": ("internal_hdd",), "nvme": ("m2_nvme",), "ssd": ("internal_ssd", "m2_ssd", "sata_ssd")}[kind]
    out = []
    for r in rows:
        if r["condition"] != "new" or r["type"] not in types or abs(r["gb"] - tb * 1000) >= 1 or re.search(INTERN_NOT, r["name"]):
            continue
        if q and q.get("watch") and not series_match(r["name"], q):
            continue
        out.append({"name": r["name"], "price": round(r["price"], 2), "per_tb": round(r["price"] / tb, 2), "link": r["link"]})
    return out


def series_match(name, q):
    """Gehört ein Angebot zur beobachteten Serie? «Alle NAS-Platten»: eine Serie genügt."""
    if q.get("any"):
        return any(series_match(name, sub) for sub in q["any"])
    n = name.lower()
    return (all(m in n for m in q.get("must", [])) and not any(x in n for x in q.get("not", []))
            and (not q.get("rx") or re.search(q["rx"], name)))


def money(s):
    """«€ 1.234,56» / «$1,234.56» / «CHF 1'003.00» / «1 234,50 kr» → 1234.56."""
    t = re.sub(r"[^0-9.,]", "", (s or "").replace("'", "").replace("’", ""))
    if not t:
        return None
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        t = t.replace("." if dec == "," else ",", "").replace(",", ".")
    elif t.count(",") > 1 or t.count(".") > 1:
        t = t.replace(".", "").replace(",", "")
    elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", t):
        t = t.replace(".", "").replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def own_prices_path(country):
    return os.path.join(CONFIG, f"prices-{country}.json")


def price_targets():
    """Was geholt wird: die Platten im NAS plus die in der App beobachteten Serien."""
    out = [dict(g, q=None) for g in local_disk_groups()]   # mit «model» (None = Modell nicht lesbar: wie bis 1.11.0)
    for w in price_watchlist():
        out.append({"key": w["key"], "tb": w["tb"], "kind": w["kind"], "label": w["label"], "disks": [], "q": w})
    return out


def price_sources(country):
    """Quellen in Reihenfolge: DE/AT zuerst Geizhals, wenn dessen robots.txt die Suche verbietet, Amazon.de (diskprices)."""
    first = price_source(country)
    if first["id"] == "geizhals":
        return [first, {"id": "diskprices", "label": "Amazon.de (diskprices.com)", "currency": "EUR", "locale": "de"}]
    return [first]


def robots_rules(text, agent="nzblab-agent"):
    """robots.txt → Liste (allow, Muster) für unseren User-Agent (eigene Gruppe, sonst «*»). RFC 9309, mit * und $."""
    groups, cur, last_ua = {}, None, False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not last_ua:
                cur = []
            groups.setdefault(v.lower(), cur)
            last_ua = True
            continue
        last_ua = False
        if cur is not None and k in ("allow", "disallow") and v:
            cur.append((k == "allow", v))
    return groups.get(agent) or groups.get("*") or []


def robots_allows(rules, url):
    """Längste passende Regel gewinnt; bei Gleichstand «Allow»."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    best = (-1, True)
    for allow, pat in rules:
        rx = "^" + re.escape(pat).replace(r"\*", ".*")
        rx = rx[:-2] + "$" if rx.endswith(r"\$") else rx
        if re.match(rx, path) and (len(pat) > best[0] or (len(pat) == best[0] and allow)):
            best = (len(pat), allow)
    return best[1]


def _robots_check(url, fetch, cache):
    """robots.txt der Seite einmal pro Lauf lesen. Nicht erreichbar (4xx) = keine Einschränkung (RFC 9309);
    Serverfehler, Netzfehler oder eine Prüfseite (Captcha) statt robots.txt = vorsichtshalber nicht abfragen.
    Ergebnis: None = erlaubt, sonst der Grund (für die Meldung in der App)."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    host = f"{u.scheme}://{u.netloc}"
    if host not in cache:
        try:
            st, txt = fetch(host + "/robots.txt", "en")
            if st == 200 and CHALLENGE.search(txt[:3000]):
                cache[host] = "challenge"
            elif st == 200:
                cache[host] = robots_rules(txt)
            elif 400 <= st < 500:
                cache[host] = []
            else:
                cache[host] = "error"
        except Exception:  # noqa: BLE001
            cache[host] = "error"
    rules = cache[host]
    if rules == "challenge":
        return "die Seite zeigt eine Prüfseite statt robots.txt"
    if rules == "error":
        return "robots.txt der Seite nicht erreichbar"
    return None if robots_allows(rules, url) else "robots.txt der Seite erlaubt keine automatische Abfrage"


def _robots_ok(url, fetch, cache):
    return _robots_check(url, fetch, cache) is None


def _fetch_one(src, t, fetch, lang, pages, robots, pause, query=None, keep=None):
    """Eine Suche bei einer Quelle: (status, error, offers, url). status: ok, none, blocked, robots, unreadable.
    [query] = eigener Suchtext (z. B. Teilenummer «ST28000NT000»), [keep] = nur Angebote, für die keep(name) gilt."""
    q = t.get("q")
    url = price_search_url(src, t["tb"], t["kind"], (query or (q or {}).get("q")) if src["id"] != "diskprices" else None)
    why = _robots_check(url, fetch, robots)
    if why:
        return "robots", f"{UNREADABLE} ({why})", [], url
    if url in pages:                      # diskprices: eine Seite für alle Grössen; gleiche Suche nur einmal pro Lauf
        status, html = pages[url]
    else:
        status, html = fetch(url, lang)
        pages[url] = (status, html)
        if pause:
            time.sleep(pause)
    if page_blocked(status, html):
        return "blocked", f"{UNREADABLE} (HTTP {status})" if status != 200 else f"{UNREADABLE} (Prüfseite)", [], url
    if status != 200:
        return "unreadable", f"{UNREADABLE} (HTTP {status})", [], url
    product = parse_toppreise_product(html, t["tb"]) if src["id"] == "toppreise" and query else None
    if product:                           # Teilenummer eindeutig: Toppreise zeigt gleich die Produktseite
        found, offers = [product], [product]
    elif src["id"] == "toppreise":
        found, offers = _Blocks("div", "Plugin_Product", html).blocks, parse_toppreise(html, t["tb"])
    elif src["id"] == "geizhals":
        found, offers = _Blocks("article", "galleryview__item", html).blocks, parse_geizhals(html, t["tb"], src["base"])
    else:
        found = parse_diskprices(html)
        offers = diskprices_pick(found, t["tb"], t["kind"], q)
    if not found:                         # Seite kam, aber ohne erkennbare Liste: Layout geändert. Keine Zahl raten.
        # Ausnahme: Suche nach einer Teilenummer findet bei Toppreise nichts → Seite ohne Karten, aber mit «keine Treffer»
        if query and re.search(r"(?i)keine (Produkte|Treffer|Ergebnisse)|0 Treffer|nichts gefunden", html):
            return "none", "keine passenden Angebote gefunden", [], url
        return "unreadable", UNREADABLE, [], url
    offers = [o for o in offers if not re.search(INTERN_NOT, o["name"])]
    if q and q.get("watch"):
        offers = [o for o in offers if series_match(o["name"], q)]
    if keep:
        offers = [o for o in offers if keep(o["name"])]
    offers.sort(key=lambda o: o["per_tb"])
    return ("ok" if offers else "none"), (None if offers else "keine passenden Angebote gefunden"), offers, url


def _search(sources, t, fetch, lang, pages, robots, pause, query=None, keep=None):
    """Eine Suche über die Quellen des Landes (erste, die antwortet): (src, status, error, offers, Browser-Link)."""
    first_url, result = None, None
    for src in sources:
        try:
            status, err, offers, url = _fetch_one(src, t, fetch, lang, pages, robots, pause, query, keep)
        except Exception as ex:  # noqa: BLE001 - eine Suche darf die anderen nicht stoppen
            status, err, offers, url = "unreadable", f"{UNREADABLE} ({type(ex).__name__})", [], price_search_url(src, t["tb"], t["kind"], query)
        # Link für den Browser: die Suche der Hauptquelle (Menschen dürfen dort suchen, auch wo robots.txt Bots sperrt)
        first_url = first_url or url
        result = (src, status, err, offers)
        if status in ("ok", "none"):
            break
    return result + (first_url,)


def _best(src, offer, extra=None):
    best = dict(offer, chf=offer["price"], currency=src["currency"], t=int(time.time()), source_id=src["id"],
                source=src["label"], **(extra or {}))
    if src["id"] == "diskprices":
        best["shop"] = "Amazon." + _amazon_tld(src["locale"])
    return best


def _own_search(sources, t, fetch, lang, pages, robots, pause):
    """
    Preis des eigenen Modells: zuerst die genaue Teilenummer (Toppreise-Suche «ST28000NT000»), behalten wird nur, was
    die Teilenummer im Namen trägt oder zur gleichen Serie in gleicher Grösse gehört. Nichts gefunden: zweite Suche nach
    Serie und Grösse («Seagate IronWolf Pro 28TB»). SSDs ohne Teilenummer: gleich Serie und Grösse.
    Ergebnis: (src, status, error, offers mit «match», Browser-Link).
    """
    m, tb = t["model"], t["tb"]
    sr = MODEL_SERIES.get(m.get("series") or "")
    keep = lambda name: own_match(name, m, tb) is not None   # noqa: E731
    series_q = f"{sr['q']} {tb:g}TB" if sr else None
    first = m.get("part") or series_q or m.get("raw")
    res = _search(sources, t, fetch, lang, pages, robots, pause, query=first, keep=keep)
    if res[1] == "none" and sr and first != series_q:
        res2 = _search(sources, t, fetch, lang, pages, robots, pause, query=series_q, keep=keep)
        if res2[1] == "ok":
            res = res2[:4] + (res[4],)    # Browser-Link bleibt die Suche nach der Teilenummer
    src, status, err, offers, url = res
    offers = [dict(o, match=own_match(o["name"], m, tb)) for o in offers]
    # genaue Treffer zuerst, dann der Preis
    offers.sort(key=lambda o: (o["match"] != "exact", o["price"]))
    if status == "none":
        err = "dein Modell gerade nirgends gelistet"
    return src, status, err, offers, url


def fetch_own_prices(country, fetch=http_get, pause=None, today=None):
    """Einmal alle Platten für dieses Land holen und in CONFIG/prices-<Land>.json schreiben (Verlauf 400 Tage)."""
    pause = PRICE_PAUSE if pause is None else pause
    sources = price_sources(country)
    if not source_allowed(sources[0]["id"]):
        return None            # Quelle abgeschaltet: kein Abruf, die App zeigt nur den Suchlink (price_summary)
    sources = [x for x in sources if source_allowed(x["id"])]
    lang = {"CH": "de-CH,de", "DE": "de-DE,de", "AT": "de-AT,de"}.get(country, "en")
    path = own_prices_path(country)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"queries": {}}
    # Datei ohne «queries» (von Hand bearbeitet, abgebrochen geschrieben): neu anfangen statt KeyError
    if not isinstance(data, dict):
        data = {"queries": {}}
    if not isinstance(data.get("queries"), dict):
        data["queries"] = {}
    today = today or time.strftime("%Y-%m-%d")
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 400 * 86400))

    def add_hist(hist, offer):
        hist = [h for h in (hist or []) if isinstance(h, dict) and h.get("d") != today and h.get("d", "") >= cutoff]
        hist.append({"d": today, "chf": offer["price"], "per_tb": offer["per_tb"], "name": offer["name"]})
        return sorted(hist, key=lambda h: h["d"])

    pages, robots, used = {}, {}, set()
    for t in price_targets():
        e = data["queries"].setdefault(t["key"], {"history": []})
        if not isinstance(e, dict):
            e = data["queries"][t["key"]] = {"history": []}
        e.update({"label": t["label"], "tb": t["tb"], "kind": t["kind"], "disks": t.get("disks", [])})
        e.pop("pending", None)
        m = t.get("model")
        # Günstigstes Angebot dieser Grösse, egal welches Modell (bis 1.11.0 der einzige Preis, jetzt die «Alternative»)
        src, status, err, offers, first_url = _search(sources, t, fetch, lang, pages, robots, pause)
        if not m:
            e.update(search_url=first_url, status=status, error=err, offers=offers[:5], source=src["label"])
            if status != "ok":
                e.pop("best", None)
                continue
            used.add(src["label"])
            e["best"] = _best(src, offers[0])
            e["history"] = add_hist(e.get("history"), offers[0])
            continue
        # Ab 1.12.0: Preis des eigenen Modells. Alte Einträge (Verlauf = günstigste Platte der Grösse) einmal umziehen.
        if e.get("mode") != "model":
            e["alt_history"] = e.get("history") or []
            e["history"] = []
            e.pop("best", None)
            e["mode"] = "model"
        e.update(model=m["label"], part=m["part"], series=m["series"], warranty_years=m["warranty_years"],
                 alt_search_url=first_url)
        osrc, ostatus, oerr, own, own_url = _own_search(sources, t, fetch, lang, pages, robots, pause)
        e.update(search_url=own_url, status=ostatus, error=oerr, offers=own[:5], source=osrc["label"])
        best = None
        if ostatus == "ok":
            used.add(osrc["label"])
            best = e["best"] = _best(osrc, own[0], {"match": own[0]["match"]})
            e["history"] = add_hist(e.get("history"), own[0])
        else:
            e.pop("best", None)
        # Günstigste Alternative: anderes Modell gleicher Grösse, nur wenn billiger als das eigene
        others = [o for o in offers if own_match(o["name"], m, t["tb"]) is None] if status == "ok" else []
        alt = min(others, key=lambda o: o["price"]) if others else None
        if alt and (best is None or alt["price"] < best["price"]):
            used.add(src["label"])
            e["alternative"] = _best(src, alt, {"model": offer_model(alt["name"], t["tb"])})
            e["alt_history"] = add_hist(e.get("alt_history"), alt)
        else:
            e.pop("alternative", None)
    label = ", ".join(sorted(used)) or sources[0]["label"]
    data.update(updated=int(time.time()), country=country, source=label, currency=sources[0]["currency"])
    os.makedirs(CONFIG, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    return data


def tool_prices_fresh(max_age_h=36):
    """Externe prices.json (optionaler Preis-Sammler, siehe prices_path) vorhanden und frisch? Dann gilt sie für CH."""
    try:
        with open(prices_path()) as f:
            d = json.load(f)
        return d if time.time() - float(d.get("updated") or 0) < max_age_h * 3600 else None
    except (OSError, ValueError, TypeError):
        return None


def _model_fields(g):
    m = g.get("model") or {}
    return {"model": m.get("label", ""), "part": m.get("part", ""), "series": m.get("series", ""),
            "warranty_years": m.get("warranty_years")} if m else {}


def _own_view(queries, groups, src):
    """
    Eigene Preisdatei für die App aufbereiten (ändert die Datei nicht): Platten im NAS sofort zeigen (vor dem ersten
    Lauf ohne Preis), Modell aus sysfs dazu. Ein Eintrag aus 1.11.0 (Preis = günstigste Platte der Grösse, egal welches
    Modell) gilt nicht als Preis des eigenen Modells: er erscheint als «Günstigste Alternative», bis der nächste Lauf da ist.
    Einträge alter Platten, die nicht mehr im NAS stecken, fallen weg (nur wenn /sys/block lesbar war).
    """
    out = {k: v for k, v in queries.items() if isinstance(v, dict) and (k.startswith("w_") or not groups)}
    for g in groups:
        v = queries.get(g["key"])
        v = dict(v) if isinstance(v, dict) else {"history": [], "pending": True,
                                                    "search_url": price_search_url(src, g["tb"], g["kind"])}
        v.update(label=g["label"], tb=g["tb"], disks=g["disks"])
        if g.get("model"):
            v.update(_model_fields(g))
            if v.get("mode") != "model":
                old = v.pop("best", None)
                if old and not own_match(old.get("name", ""), g["model"], g["tb"]):
                    v["alternative"] = dict(old, model=offer_model(old.get("name", ""), g["tb"]))
                elif old:
                    v["best"] = dict(old, match=own_match(old.get("name", ""), g["model"], g["tb"]))
                v["alt_history"], v["history"] = v.get("history") or [], []   # Verlauf war die günstigste Platte
                if "best" not in v:
                    v["pending"] = True
                    v.pop("error", None)
                v["offers"] = []
        out[g["key"]] = v
    return out


def _tool_model(raw):
    """Modellname so, wie ihn die externe Preisliste speichert: «ST28000NT000», «Samsung 990 PRO»."""
    raw = (raw or "").replace("WDC ", "").strip()
    if " " in raw:
        return " ".join(t for t in raw.split() if t.upper() != "SSD" and not re.match(r"^\d+(\.\d+)?[GT]B$", t, re.I))
    return raw.split("-")[0] if re.match(r"^(ST|WD|HUH|HUS|MG)\w+-", raw) else raw


def _tool_view(queries, groups):
    """
    Externe prices.json (Toppreise mit Shops und Ausland): «best» ist dort die günstigste Platte der Grösse, der Preis des
    eigenen Modells steht in «models_now». Für Platten mit erkanntem Modell: Preis = eigenes Modell (Verlauf aus
    «model_history», falls die Liste ihn führt), das günstigste Angebot wird zur Alternative, falls billiger und ein anderes
    Modell. Shops/Ausland/weitere Angebote gehören zum günstigsten Angebot und fallen dort weg.
    """
    out = dict(queries)
    for g in groups:
        m = g.get("model")
        base = g["key"].split("-")[0]
        te = queries.get(base)
        if not m or not isinstance(te, dict):
            continue
        keys = {_norm(m["part"]), _norm(_tool_model(m["raw"]))} - {""}
        mn = next((x for x in te.get("models_now") or [] if isinstance(x, dict) and _norm(x.get("model")) in keys), None)
        if mn is None:
            continue
        v = {k: x for k, x in te.items() if k not in ("best", "shops", "intl", "offers", "models_now", "history", "error", "model_history")}
        v.update(_model_fields(g), label=g["label"], disks=g["disks"], tb=g["tb"], mode="model")
        tb = g["tb"]
        if mn.get("chf") is not None:
            chf = float(mn["chf"])
            name = re.sub(r"\s+ab CHF\s.*$", "", mn.get("name") or "").strip() or m["label"]   # Name aus dem Seitentitel
            v["best"] = {"name": name, "chf": chf, "price": chf, "per_tb": mn.get("per_tb") or round(chf / tb, 2),
                         "link": mn.get("link", ""), "currency": "CHF", "match": own_match(name, m, tb) or "exact"}
        else:
            v["error"] = "dein Modell ist nicht mehr im Handel"
        v["history"] = ((te.get("model_history") or {}).get(mn.get("model")) or []) if isinstance(te.get("model_history"), dict) else []
        tb_best = te.get("best") if isinstance(te.get("best"), dict) else None
        if tb_best and tb_best.get("chf") is not None and own_match(tb_best.get("name", ""), m, tb) is None \
                and ("best" not in v or float(tb_best["chf"]) < v["best"]["chf"]):
            v["alternative"] = dict(tb_best, model=offer_model(tb_best.get("name", ""), tb))
        v["alt_history"] = te.get("history") or []
        out[g["key"]] = v
    return out


def price_summary(today=None):
    """
    Plattenpreise für die App: pro Plattengrösse der günstigste Preis, Verlauf, Tiefst- und Höchstpreis der letzten 90 Tage,
    Mittel der letzten 30 Tage und ob jetzt ein guter Zeitpunkt ist («günstig»): unter der eigenen Grenze aus der App, oder
    so tief wie seit 90 Tagen nicht und mindestens 3 % unter dem 30-Tage-Mittel (erst ab 7 Tagen Verlauf).
    Datenquelle: für die Schweiz eine frische externe prices.json (falls vorhanden, mit Shops und Ausland),
    sonst die eigene Abfrage des Agenten (ab 1.10.0) im gewählten Land. Beträge in «chf» sind in der Währung «currency»
    (Name bleibt aus Kompatibilität mit älteren Apps).
    """
    watch = price_watchlist()
    country = price_country()
    tool = None
    if country in ("", "CH"):
        try:
            with open(prices_path()) as f:
                tool = json.load(f)
        except (OSError, ValueError):
            tool = None
        if tool is not None and country == "CH" and tool_prices_fresh() is None:
            tool = None   # alt: der Agent holt selbst
    countries = [{"code": c, "label": l, "source": s} for c, l, s in PRICE_COUNTRIES]
    search_only = bool(country) and not source_allowed(price_source(country)["id"])
    base = {"country": country, "country_set": bool(country), "countries": countries, "catalog": PRICE_SERIES,
            "watch": [{"series": w["series"], "tb": w["tb"]} for w in watch],
            "sources": [price_source_info(x) for x in (price_sources(country) if country else [])],
            "ads_available": ads_available(), "search_only": search_only}
    if tool is not None and not isinstance(tool, dict):
        tool = None
    if tool is not None and not source_allowed("toppreise"):
        tool = None
    if tool is not None:
        d, src = tool, {"label": tool.get("source", "Toppreise.ch"), "currency": "CHF", "id": "toppreise"}
    elif search_only:
        # Quelle abgeschaltet: keine Zahlen, nur Platten im NAS und beobachtete Serien mit Suchlink
        src = price_source(country)
        qs = _own_view({}, local_disk_groups(), src)
        for w in watch:
            qs.setdefault(w["key"], {"label": w["label"], "tb": w["tb"], "history": [], "pending": True,
                                     "search_url": price_search_url(src, w["tb"], w["kind"], w.get("q") if isinstance(w.get("q"), str) else None)})
        d = {"queries": qs, "updated": 0}
    elif country:
        src = price_source(country)
        try:
            with open(own_prices_path(country)) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {"queries": {}}
        if not isinstance(d, dict):
            d = {"queries": {}}
        if not isinstance(d.get("queries"), dict):
            d["queries"] = {}
        src = dict(src, label=d.get("source") or src["label"])
        d = dict(d, queries=_own_view(d["queries"], local_disk_groups(), src))
    else:
        return dict(base, available=False, sizes=[], currency="", source="")
    queries = d.get("queries")
    queries = dict(queries) if isinstance(queries, dict) else {}
    if tool is not None:
        queries = _tool_view(queries, local_disk_groups())
    queries = {k: v for k, v in queries.items() if isinstance(v, dict)}
    watch_keys = {w["key"] for w in watch}
    queries = {k: v for k, v in queries.items() if not k.startswith("w_") or k in watch_keys}
    for w in watch:
        queries.setdefault(w["key"], {"label": w["label"], "tb": w["tb"], "history": [], "pending": True})
    if not queries:
        return dict(base, available=False, sizes=[], currency=src["currency"], source=src["label"])
    today = today or time.strftime("%Y-%m-%d")
    t = time.mktime(time.strptime(today, "%Y-%m-%d"))
    d90 = time.strftime("%Y-%m-%d", time.localtime(t - 90 * 86400))
    d30 = time.strftime("%Y-%m-%d", time.localtime(t - 30 * 86400))
    alerts = settings().get("price_alerts") or {}
    cur = src["currency"]
    out = []
    for key, v in queries.items():
        hist = v.get("history") or []
        best = v.get("best")
        p90 = [h["chf"] for h in hist if h.get("d", "") >= d90]
        p30 = [h["chf"] for h in hist if h.get("d", "") >= d30]
        low90, high90 = (min(p90), max(p90)) if p90 else (None, None)
        avg30 = round(sum(p30) / len(p30), 2) if p30 else None
        thr = alerts.get(key)
        cheap, reason = False, ""
        if search_only:
            best, hist = None, []
        # Quelle des Preises: beim Angebot (ab P1), sonst die Quelle der Datei. Amazon-Preise: keine «günstig»-Meldung.
        sid = (best or {}).get("source_id") or src.get("id", "")
        amazon = bool(PRICE_SOURCES.get(sid, {}).get("amazon")) or bool(
            best and (str(best.get("shop", "")).startswith("Amazon.") or AMAZON_HOST.search(urlparse(str(best.get("link", ""))).hostname or "")))
        if best and amazon:
            pass
        elif best and thr and best["chf"] <= float(thr):
            cheap, reason = True, f"unter deiner Grenze von {cur} {float(thr):.0f}"
        elif best and len(hist) >= 7 and low90 is not None and best["chf"] <= low90 and avg30 and best["chf"] < 0.97 * avg30:
            cheap, reason = True, "so günstig wie seit 90 Tagen nicht"
        err = v.get("error")
        unreadable = bool(err) and str(err).startswith(UNREADABLE)
        alt = v.get("alternative") if isinstance(v.get("alternative"), dict) else None
        match = (best or {}).get("match") or ""
        out.append({"key": key, "label": v.get("label", key), "tb": v.get("tb"), "disks": v.get("disks", []),
                    # ab 1.12.0: eigenes Modell (Preis in «best») und günstigste Alternative (anderes Modell, nur wenn billiger)
                    "model": v.get("model") or "", "part": v.get("part") or "", "series": v.get("series") or "",
                    "warranty_years": v.get("warranty_years"), "match": match,
                    "match_label": {"exact": "exakt", "series": "gleiche Serie"}.get(match, ""),
                    "alternative": with_ad(alt), "alt_search_url": v.get("alt_search_url", ""),
                    "best": with_ad(best), "models_now": v.get("models_now", []), "offers": [with_ad(o) for o in (v.get("offers") or [])[:3]],
                    "source": (best or {}).get("source") or (src["label"] if best else ""),
                    "source_id": sid, "fetched_at": int((best or {}).get("t") or d.get("updated") or 0) if best else 0,
                    "search_only": search_only, "no_alert": amazon and bool(best),
                    "search_url": v.get("search_url", ""), "history": hist[-180:],
                    "shops": [with_ad(x) for x in (v.get("shops") or [])[:5]],
                    "intl": [with_ad(x) for x in (v.get("intl") or []) if source_allowed({"geizhals": "geizhals", "amazon": "diskprices"}.get(str(x.get("source", "")).lower(), "toppreise"))][:14], "low90": low90, "high90": high90,
                    "avg30": avg30, "threshold": thr, "cheap": cheap, "reason": reason, "error": err,
                    "unreadable": unreadable and not search_only, "browser_url": v.get("search_url", "") if unreadable or not best else "",
                    "watch": key.startswith("w_"), "pending": bool(v.get("pending"))})
    return dict(base, available=True, updated=d.get("updated"), source=src["label"], currency=cur, sizes=out,
                rates_date=(d.get("rates") or {}).get("date"))


class CrashStore:
    """Absturzberichte der App (nur Abstürze und «App reagiert nicht»), als JSON-Dateien in CONFIG/crashes."""

    def __init__(self, path):
        self.dir = path
        os.makedirs(path, exist_ok=True)

    def add(self, obj):
        # Einzelne Ersatzzeichen (\ud800) sind in UTF-8 nicht schreibbar: durch «?» ersetzen, statt abzubrechen
        # Kurze Felder (Zeit, Versionen, Art) ab 1.16.1 auf 200 Zeichen begrenzt; nur der Stacktrace darf lang sein
        keep = {k: str(obj.get(k, ""))[:CRASH_MAX_BYTES if k == "trace" else 200].encode("utf-8", "replace").decode("utf-8")
                for k in ("time", "version", "android", "device", "screen", "kind", "trace")}
        # ab 1.16.1: alle Felder auch serverseitig schwärzen (Zugangsdaten in Adressen, Bearer, Cookie-Zeilen,
        # Schlüssel/Passwörter, lange Zufallsketten); IPs, Hostnamen und Pfade bleiben, sie helfen bei der Fehlersuche
        keep = {k: redact_secrets(v) for k, v in keep.items()}
        keep["received"] = int(time.time())
        name = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}.json"
        tmp = os.path.join(self.dir, name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(self.dir, name))
        except BaseException:
            # Keine halben .tmp-Dateien liegen lassen
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        files = sorted(x for x in os.listdir(self.dir) if x.endswith(".json"))
        for old in files[:-CRASH_KEEP]:
            try:
                os.remove(os.path.join(self.dir, old))
            except OSError:
                pass

    def list(self, limit=50):
        out = []
        for name in sorted((x for x in os.listdir(self.dir) if x.endswith(".json")), reverse=True)[:limit]:
            try:
                with open(os.path.join(self.dir, name)) as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
        return out


def from_private_net(ip):
    """
    Nur das NAS selbst und das Heimnetz (für die Beobachtungsliste, die der Preis-Wächter ohne Token abholt).
    Docker-Netze nur über ihr Gateway (x.x.x.1 = der Host): ein Reverse-Proxy-Container (z. B. Nginx Proxy Manager),
    der Anfragen aus dem Internet weiterreicht, hat eine andere Adresse und kommt nicht durch.
    """
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback:
        return True
    if a in ipaddress.ip_network("172.16.0.0/12"):
        return ip.endswith(".1")
    return a in ipaddress.ip_network("192.168.0.0/16") or a in ipaddress.ip_network("10.0.0.0/8")


LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".ts.net")   # .ts.net: Tailscale MagicDNS
# ab 1.19.0: weitere erlaubte Host-Namen (VPN, eigene Domain), kommagetrennt: «nas.example.org» exakt oder «.example.org» als Endung
def _extra_hosts(raw):
    """NZBLAB_HOSTS lesen. Abgewiesen (mit Warnung): Platzhalter «*» und Endungen mit nur einem Teil wie «.com» (öffnet sonst alle)."""
    out = []
    for x in (raw or "").split(","):
        x = x.strip().lower().rstrip(".")
        if not x:
            continue
        if "*" in x or (x.startswith(".") and x.count(".") < 2):
            print(f"WARNUNG: NZBLAB_HOSTS-Eintrag «{x}» ignoriert (zu breit). WARNING: NZBLAB_HOSTS entry ignored (too broad).", flush=True)
            continue
        out.append(x)
    return tuple(out)


EXTRA_HOSTS = _extra_hosts(os.environ.get("NZBLAB_HOSTS", ""))


def ipaddress_is_loopback(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_loopback or bool(getattr(a, "ipv4_mapped", None) and a.ipv4_mapped.is_loopback)


def local_host_header(host):
    """
    Schutz gegen DNS-Rebinding für die beiden Endpunkte ohne Token: Eine fremde Website, deren Name kurz auf die Adresse des
    NAS zeigt, schickt ihren eigenen Namen im Host-Header («evil.example:8099»). Erlaubt sind nur IP-Adressen, «localhost»,
    Namen ohne Punkt («tower», «nas»), Namen im Heimnetz (.local, .lan, .home.arpa, .internal, .localdomain), Tailscale (.ts.net)
    und was in NZBLAB_HOSTS steht.
    """
    h = (host or "").strip().lower()
    if not h:
        return False
    if h.startswith("["):                     # IPv6 mit Port: [fe80::1]:8099
        h = h[1:h.find("]")] if "]" in h else ""
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    h = h.rstrip(".")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*", h):
        return False
    if h == "localhost" or "." not in h or h.endswith(LOCAL_SUFFIXES):
        return True
    return any(h == x or (x.startswith(".") and h.endswith(x)) for x in EXTRA_HOSTS)


def run_prices(stop, notify):
    """
    Eigene Preisabfrage (ab 1.10.0): einmal am Tag ab 06:00 (nach einem Neustart sofort, wenn der letzte Versuch älter als
    20 Stunden ist), sofort nach einer Länderwahl in der App. Für die Schweiz nicht, solange eine externe prices.json frisch ist.
    Dazu alle 3 Stunden: wird eine Plattengrösse günstig, einmal melden (pro Grösse und Preis).
    """
    sent, last_check = {}, time.time()
    PRICE_KICK.set()  # beim Start einmal prüfen, ob ein Lauf fällig ist
    while True:
        # In kurzen Schritten warten, damit «stop» den Faden sofort beendet (vorher bis zu 30 Minuten)
        kicked, until = False, time.time() + 1800
        while not stop.is_set() and time.time() < until:
            if PRICE_KICK.wait(5):
                kicked = True
                break
        if stop.is_set():
            return
        PRICE_KICK.clear()
        try:
            c = price_country()
            if c and not (c == "CH" and tool_prices_fresh()):
                att_path = os.path.join(CONFIG, "prices-attempt.json")
                try:
                    with open(att_path) as f:
                        att = json.load(f)
                except (OSError, ValueError):
                    att = {}
                due = time.time() - float(att.get(c, 0)) > 20 * 3600 and (time.localtime().tm_hour >= 6 or not att.get(c))
                new_watch = False
                try:
                    with open(own_prices_path(c)) as f:
                        have = json.load(f)
                    have = have.get("queries") if isinstance(have, dict) else None
                    new_watch = any(w["key"] not in (have if isinstance(have, dict) else {}) for w in price_watchlist())
                except (OSError, ValueError):
                    pass
                if due or (kicked and new_watch):
                    # Versuch vorher festhalten: scheitert der Lauf, kommt der nächste erst morgen
                    att[c] = int(time.time())
                    with open(att_path, "w") as f:
                        json.dump(att, f)
                    print(f"Preise: hole {c} ({price_source(c)['label']})", flush=True)
                    fetch_own_prices(c)
        except Exception as e:  # noqa: BLE001
            print("Preise:", e, flush=True)
        if time.time() - last_check >= 3 * 3600:
            last_check = time.time()
            try:
                s = price_summary()
                for sz in s.get("sizes", []):
                    b = sz.get("best") or {}
                    if sz.get("cheap") and sent.get(sz["key"]) != b.get("chf"):
                        sent[sz["key"]] = b.get("chf")
                        notify(f"Platte günstig: {sz['label']}", f"{b.get('name', '')} für {s.get('currency') or 'CHF'} {b.get('chf')} ({sz['reason']}).")
            except Exception as e:  # noqa: BLE001
                print("Preise:", e, flush=True)


# ---------------------------------------------------------------- Heute auf dem NAS

class Today:
    """
    Was heute auf dem NAS los war, ohne Passwörter oder API-Keys, nur aus Dateien, die ohnehin auf dem NAS liegen:
    Unraid-Meldungen (/tmp/notifications), fertige und fehlgeschlagene Downloads (Verlauf von SABnzbd in appdata,
    nur lesend), Container gestoppt oder abgestürzt (Docker-Status), Fehler im Systemprotokoll (/var/log/syslog)
    und die Alarme des Agenten selbst (Hitze, Array, neue Geräte, angelaufene Platten).
    """

    LEVELS = {"alert": "err", "warning": "warn", "normal": "info"}
    # Systemprotokoll: nur echte Probleme, keine Routine
    SYSLOG_BAD = re.compile(r"(?i)(i/o error|\berror\b|\bfail(ed|ure)?\b|critical|segfault|out of memory|oom-kill|call trace|corrupt|read-only file system|timed out)")
    SYSLOG_SKIP = re.compile(r"(?i)(error_page|errors=remount|ACPI|nginx.*favicon|failed password|no error|0 errors|ignoring|reset_tcp)")

    def __init__(self, store, files_root=FILES_ROOT, notify_dir=None, log_dir=None, docker_dir=None):
        self.store, self.root = store, files_root
        self.notify_dir = notify_dir or NOTIFY_DIR
        self.log_dir = log_dir or LOG_DIR
        self.docker_dir = docker_dir or os.environ.get("DOCKER_DIR", "/host/docker/containers")
        self._sab = (0, None)

    def sab_db(self):
        # Pfad einmal pro Stunde suchen (binhex, linuxserver und Co. legen ihn verschieden ab)
        t, path = self._sab
        if time.time() - t < 3600:
            return path
        path = None
        for pat in ("appdata/*sab*/admin/history1.db", "appdata/*sab*/*/admin/history1.db", "appdata/*sab*/*/*/admin/history1.db"):
            hits = glob.glob(f"{self.root}/{pat}", recursive=False)
            if hits:
                path = hits[0]
                break
        self._sab = (time.time(), path)
        return path

    def sources(self):
        return {"unraid": os.path.isdir(self.notify_dir), "downloads": self.sab_db() is not None,
                "docker": os.path.isdir(self.docker_dir), "syslog": LOGS_ON and os.path.isfile(f"{self.log_dir}/syslog")}

    def notifications(self, since):
        out = []
        for sub, unread in (("unread", True), ("archive", False)):
            for f in glob.glob(f"{self.notify_dir}/{sub}/*.notify"):
                kv = {}
                for line in (read(f, "") or "").splitlines():
                    k, _, v = line.partition("=")
                    kv[k.strip()] = v.strip().strip('"')
                t = int(kv.get("timestamp", "0") or 0) if kv.get("timestamp", "").isdigit() else 0
                if t >= since:
                    out.append({"t": t, "kind": "unraid", "level": self.LEVELS.get(kv.get("importance", ""), "info"),
                                "title": kv.get("subject") or kv.get("event") or "Unraid-Meldung",
                                "text": kv.get("description", ""), "unread": unread})
        return out

    def downloads(self, since):
        self.dl_counts = {}
        path = self.sab_db()
        if not path:
            return []
        try:
            # Nur lesen, aber ohne «immutable»: SABnzbd schreibt laufend hinein, sonst wären die Werte veraltet
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            rows = db.execute("SELECT completed, name, category, status, bytes, fail_message FROM history "
                              "WHERE completed >= ? ORDER BY completed DESC LIMIT 200", (since,)).fetchall()
            # Zählen ohne Grenze (bei vielen Downloads pro Woche waren es sonst höchstens 200)
            self.dl_counts = dict(db.execute("SELECT LOWER(status), COUNT(*) FROM history WHERE completed >= ? GROUP BY LOWER(status)",
                                             (since,)).fetchall())
            db.close()
        except sqlite3.Error:
            return []
        out = []
        for t, name, cat, status, size, fail in rows:
            ok = (status or "").lower() == "completed"
            # Wie SABnzbd und die App: 1 GB = 1024³ Byte
            size_txt = f"{size / 1024**3:.1f} GB" if size and size >= 1024**3 else f"{(size or 0) / 1024**2:.0f} MB"
            out.append({"t": int(t or 0), "kind": "download", "level": "info" if ok else "err",
                        "title": ("Heruntergeladen: " if ok else "Download fehlgeschlagen: ") + (name or "?"),
                        "text": " · ".join(x for x in (cat if cat and cat != "*" else "", size_txt if ok else (fail or "ohne Grund")) if x)})
        return out

    def containers(self, since):
        out = []
        for cfg in glob.glob(f"{self.docker_dir}/*/config.v2.json"):
            try:
                j = json.loads(read(cfg, "") or "{}")
            except ValueError:
                continue
            name, st = (j.get("Name") or "?").lstrip("/"), j.get("State") or {}
            started, finished = self._utc(st.get("StartedAt")), self._utc(st.get("FinishedAt"))
            if not st.get("Running") and finished >= since:
                code = st.get("ExitCode", 0)
                if st.get("OOMKilled"):
                    out.append({"t": finished, "kind": "docker", "level": "err", "title": f"{name}: zu wenig Arbeitsspeicher",
                                "text": "Docker hat den Container beendet, weil sein Speicher-Limit voll war."})
                elif code not in (0, 143, 137):
                    out.append({"t": finished, "kind": "docker", "level": "err", "title": f"{name} abgestürzt",
                                "text": f"Beendet mit Code {code}. Log: Unraid › DOCKER › {name} › Logs."})
                else:
                    out.append({"t": finished, "kind": "docker", "level": "info", "title": f"{name} gestoppt", "text": ""})
            elif st.get("Running") and started >= since:
                out.append({"t": started, "kind": "docker", "level": "info", "title": f"{name} gestartet",
                            "text": f"{st.get('RestartCount', 0)} Neustarts bisher" if st.get("RestartCount") else ""})
        return out

    @staticmethod
    def _utc(iso):
        if not iso or iso.startswith("0001"):
            return 0
        try:
            return calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return 0

    def syslog(self, since):
        if not LOGS_ON:
            return []                      # ab 1.18.0 (Audit M2): ohne NZBLAB_LOGS=1 keine Syslog-Inhalte, auch nicht in «Heute»
        path = f"{self.log_dir}/syslog"
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 3_000_000))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        year, seen, out = time.localtime().tm_year, {}, []
        for line in lines:
            if not self.SYSLOG_BAD.search(line) or self.SYSLOG_SKIP.search(line):
                continue
            try:
                t = int(time.mktime(time.strptime(f"{year} {line[:15]}", "%Y %b %d %H:%M:%S")))
                if t > time.time() + 86400:   # Dezember-Zeilen nach Neujahr
                    t = int(time.mktime(time.strptime(f"{year - 1} {line[:15]}", "%Y %b %d %H:%M:%S")))
            except ValueError:
                continue
            if t < since:
                continue
            # «Oct 17 14:28:38 nas kernel: …» → Quelle und Text; gleiche Meldungen zusammenfassen
            rest = line[16:].split(" ", 1)[1] if " " in line[16:] else line[16:]
            src, _, msg = rest.partition(": ")
            key = re.sub(r"\d+", "#", msg)[:120]
            if key in seen:
                seen[key]["n"] += 1
                seen[key]["t"] = t
                continue
            seen[key] = {"t": t, "kind": "syslog", "level": "warn", "title": redact_log(f"Systemprotokoll: {src.split('[')[0]}")[:80],
                         # ab 1.18.0: wie /api/logs schwärzen (IP, MAC, Benutzer, E-Mail, Pfade, Geheimnisse)
                         "text": redact_log(msg[:LOG_LINE_MAX])[:300], "n": 1}
        for e in seen.values():
            if e["n"] > 1:
                e["text"] += f" ({e['n']}×)"
            out.append(e)
        return sorted(out, key=lambda e: -e["t"])[:40]

    def collect(self, hours=24):
        since = int(time.time()) - hours * 3600
        items = []
        for part in (self.notifications, self.downloads, self.containers, self.syslog):
            try:
                items += part(since)
            except Exception as e:   # eine Quelle darf die anderen nicht mitreissen
                print("Heute:", part.__name__, e, flush=True)
        items += self.store.events(since)
        for w in self.store.wakes(since):
            who = ", ".join(n for n, _ in (w.get("top") or [])[:3]) or "unbekannt"
            items.append({"t": w["t"], "kind": "disk", "level": "info", "title": f"Platte {w['disk']} angelaufen",
                          "text": f"Nach {w.get('quiet_min', '?')} Min. Ruhe. Aktiv in dem Moment: {who}."})
        # Nach einem Neustart starten alle Container fast gleichzeitig: eine Zeile statt zwölf
        starts = sorted((e for e in items if e["kind"] == "docker" and e["title"].endswith(" gestartet")), key=lambda e: e["t"])
        if len(starts) >= 4 and starts[-1]["t"] - starts[0]["t"] <= 900:
            items = [e for e in items if e not in starts]
            items.append({"t": starts[-1]["t"], "kind": "docker", "level": "info", "title": f"{len(starts)} Container gestartet",
                          "text": ", ".join(e["title"][:-len(" gestartet")] for e in starts)})
        items.sort(key=lambda e: -e["t"])
        dl = [e for e in items if e["kind"] == "download"]
        counts = getattr(self, "dl_counts", None) or {}
        return {"hours": hours, "items": items[:300], "sources": self.sources(),
                "summary": {"downloads": counts.get("completed", sum(1 for e in dl if e["level"] == "info")),
                            "failed": counts.get("failed", sum(1 for e in dl if e["level"] == "err")),
                            # Fehlgeschlagene Downloads stehen schon unter «failed», nicht nochmals als Fehler zählen
                            "errors": sum(1 for e in items if e["level"] == "err" and e["kind"] != "download"),
                            "warnings": sum(1 for e in items if e["level"] == "warn"),
                            "unread": sum(1 for e in items if e.get("unread"))}}


# ---------------------------------------------------------------- Ab 1.15.0: Neustarts, Plattentemperaturen, Netz, SMART,
# Backups, RAID, Log-Auszüge. Alles nur lesen; was Geräte oder Logs braucht, ist freiwillig (Umgebungsvariable + Compose-Block).

API_LEVEL = 16              # steigt mit jeder Agent-Version, die neue Endpunkte bringt; die App vergleicht mit ihrem Minimum


def _env_int(name, default, lo, hi):
    try:
        return max(lo, min(int(os.environ.get(name, "") or default), hi))
    except ValueError:
        return default


def _env_on(name, default):
    v = os.environ.get(name, "").strip().lower()
    return default if not v else v not in ("0", "false", "no", "off", "nein")


NET_ON = _env_on("NZBLAB_NET", True)
NET_TARGET = os.environ.get("NZBLAB_NET_TARGET", "1.1.1.1:443").strip()
NET_INTERVAL = _env_int("NZBLAB_NET_INTERVAL", 60, 30, 3600)
NET_GATEWAY_PORT = _env_int("NZBLAB_NET_GATEWAY_PORT", 53, 1, 65535)
NET_PROBES = 3
SMART_ON = _env_on("NZBLAB_SMART", False)
SMART_EVERY = _env_int("NZBLAB_SMART_MINUTES", 60, 10, 1440) * 60
LOGS_ON = _env_on("NZBLAB_LOGS", False)
BACKUP_PATHS = [x.strip() for x in os.environ.get("NZBLAB_BACKUP_PATHS", "").split(",") if x.strip()]
BACKUP_AUTO = _env_on("NZBLAB_BACKUP_AUTO", True)
BIND = os.environ.get("NZBLAB_BIND", "0.0.0.0").strip() or "0.0.0.0"
MAX_CONN = _env_int("NZBLAB_MAX_CONN", 256, 16, 4096)              # ab 1.16.1: offene Verbindungen insgesamt
MAX_CONN_PER_IP = _env_int("NZBLAB_MAX_CONN_PER_IP", 32, 4, 1024)   # ab 1.16.1: je Absender-Adresse
MAX_BODY = 64 * 1024        # grösste Anfrage (Bytes); Absturzberichte behalten ihr eigenes Limit (CRASH_MAX_BYTES * 2)
AUTH_FAILS = _env_int("NZBLAB_AUTH_FAILS", 10, 3, 1000)
DISKTEMP_EVERY = 300        # Plattentemperaturen alle 5 Minuten speichern
# Ab 1.16.0: Dienste in appdata finden und ihre API-Keys auslesen (Standard an, aus mit DISCOVER_KEYS=0).
# DISCOVER_ROOTS: Container-Pfade der appdata-Ordner, durch Komma getrennt; leer = <FILES_ROOT>/appdata (Unraid),
# sonst <FILES_ROOT>/apps (TrueNAS) und <FILES_ROOT>/docker (Synology).
DISCOVER_KEYS = _env_on("DISCOVER_KEYS", True)
DISCOVER_ROOTS = [x.strip() for x in os.environ.get("DISCOVER_ROOTS", "").split(",") if x.strip()]
SAMPLER_STATE = {"last": time.time()}   # letzte fertige Messung (Gesundheitsprüfung: hängt die Messschleife?)


def sampling_age(now=None):
    return int((now if now is not None else time.time()) - SAMPLER_STATE["last"])


def sampling_ok(now=None):
    """Messschleife lebt: letzte Messung höchstens 2 Intervalle (plus 15 s Spielraum) her."""
    return sampling_age(now) <= 2 * INTERVAL + 15


# --- Neustarts

def boot_now(proc_dir=None):
    """(boot_time, uptime_s) des Hosts: btime aus /proc/stat (genau), Betriebszeit aus /proc/uptime. None, wenn nicht lesbar."""
    proc_dir = proc_dir or HOST_PROC
    up = (read(f"{proc_dir}/uptime", "") or "").split()
    try:
        uptime = float(up[0])
    except (IndexError, ValueError):
        return None
    btime = None
    for line in (read(f"{proc_dir}/stat", "") or "").splitlines():
        if line.startswith("btime "):
            try:
                btime = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return (btime if btime else int(time.time() - uptime)), int(uptime)


def check_boot(store, proc_dir=None, now=None):
    b = boot_now(proc_dir)
    if b is None:
        return False
    new = store.note_boot(b[0], b[1], now)
    if new:
        print(f"Neustart erkannt: System läuft seit {time.strftime('%d.%m.%Y %H:%M', time.localtime(b[0]))}", flush=True)
    return new


# --- Plattentemperaturen pro Gerät

# Blockgeräte für Temperaturen: klassische Namen und ab 1.17.0 Synology DSM 7 (sata1…, sas1…, usb1…)
DISK_DEV = re.compile(r"(sd[a-z]{1,3}|hd[a-z]|nvme\d+n\d+|vd[a-z]{1,2}|(?:sata|sas|usb)\d{1,3})$")


def disk_temps(sys_root=None, smart_latest=None):
    """{Gerät: °C} aus drivetemp (SATA) bzw. nvme-hwmon; fehlt ein Wert, der letzte SMART-Wert (falls SMART an)."""
    sys_root = sys_root or HOST_SYS
    try:
        devs = sorted(d for d in os.listdir(os.path.join(sys_root, "block")) if DISK_DEV.match(d))
    except OSError:
        devs = []
    out = {}
    for dev in devs:
        t = disk_temp(sys_root, dev)
        if t is not None:
            out[dev] = t
    for d in (smart_latest or {}).values():
        name = (d.get("device_name") or "").rsplit("/", 1)[-1]
        if name and name not in out and isinstance(d.get("temp"), (int, float)) and not d.get("standby") and 0 < d["temp"] < 120:
            out[name] = d["temp"]
    return out


def record_disk_temps(store, sys_root=None, smart_latest=None, now=None):
    temps = disk_temps(sys_root, smart_latest)
    if temps:
        store.add_series(int(now or time.time()), {f"disk:{k}": v for k, v in temps.items()})
    return temps


def series_res(days, res):
    if res == "raw":
        return "raw" if days <= 2 else "hour"         # Rohwerte gibt es nur 2 Tage
    if res in ("hour", "day"):
        return res
    return "raw" if days <= 2 else ("hour" if days <= 14 else "day")


def disktemps_reply(store, days, res=None, sys_root=None, now=None):
    now = int(now or time.time())
    res = series_res(days, res)
    data = store.series("disk:", now - days * 86400, res)
    latest = store.latest_series("disk:")
    devices = {}
    for dev in sorted(set(data) | set(latest)):
        try:
            model = disk_model(sys_root or HOST_SYS, dev)
        except Exception:  # noqa: BLE001
            model = ""
        devices[dev] = {"model": model or "", "latest": (latest.get(dev) or {}).get("v"), "points": data.get(dev, [])}
    return {"days": days, "res": res, "fields": ["t", "c"] if res == "raw" else ["t", "avg", "max"],
            "interval_s": DISKTEMP_EVERY, "devices": devices}


# --- Netz und Latenz (TCP-Verbindungsaufbau, keine ICMP-Rechte nötig)

def default_gateway(proc_dir=None):
    """Standard-Gateway des Hosts (IPv4) aus der Routing-Tabelle, sonst None."""
    proc_dir = proc_dir or HOST_PROC
    paths = ([f"{proc_dir}/1/net/route"] if proc_dir != "/proc" else []) + [f"{proc_dir}/net/route"]
    for p in paths:
        for line in (read(p, "") or "").splitlines()[1:]:
            f = line.split()
            if len(f) >= 8 and f[1] == "00000000" and f[7] == "00000000":
                try:
                    if int(f[3], 16) & 2:
                        return str(ipaddress.IPv4Address(int.from_bytes(bytes.fromhex(f[2]), "little")))
                except ValueError:
                    continue
    return None


def parse_target(s, default_port=443):
    """«host:port», «[v6]:port» oder «host» → (host, port) oder None."""
    s = (s or "").strip()
    m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?", s) or re.fullmatch(r"([A-Za-z0-9.-]{1,253})(?::(\d{1,5}))?", s)
    if not m:
        return None
    port = int(m.group(2) or default_port)
    return (m.group(1), port) if 0 < port < 65536 else None


def tcp_probe(host, port, timeout=2.0):
    """Zeit für den TCP-Verbindungsaufbau in ms. Eine Ablehnung (RST) zählt auch als Antwort; keine Antwort = None."""
    t0 = time.monotonic()
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except ConnectionRefusedError:
        pass
    except OSError:
        return None
    return round((time.monotonic() - t0) * 1000, 1)


def net_targets(proc_dir=None):
    out = {}
    gw = default_gateway(proc_dir)
    if gw:
        out["gateway"] = (gw, NET_GATEWAY_PORT)
    t = parse_target(NET_TARGET)
    if t:
        out["internet"] = t
    return out


def net_round(targets, probe=tcp_probe, probes=NET_PROBES):
    """Je Ziel einige Versuche: Median der Antwortzeit (ms) und Verlust in Prozent."""
    vals = {}
    for name, (host, port) in targets.items():
        ms = [probe(host, port) for _ in range(probes)]
        ok = sorted(x for x in ms if x is not None)
        vals[f"net:{name}:rtt"] = ok[len(ok) // 2] if ok else None
        vals[f"net:{name}:loss"] = round(100.0 * (probes - len(ok)) / probes, 1)
    return vals


def net_reply(store, days, res=None, now=None, proc_dir=None):
    now = int(now or time.time())
    res = series_res(days, res)
    data = store.series("net:", now - days * 86400, res)
    latest = store.latest_series("net:")
    series, last = {}, {}
    for key, pts in data.items():
        name, _, what = key.partition(":")
        series.setdefault(name, {})[what] = pts
    for key, v in latest.items():
        name, _, what = key.partition(":")
        last.setdefault(name, {"t": v["t"]})[what] = v["v"]
    return {"enabled": NET_ON, "interval_s": NET_INTERVAL, "probes": NET_PROBES, "days": days, "res": res,
            "fields": ["t", "v"] if res == "raw" else ["t", "avg", "max"],
            "targets": {k: f"{h}:{p}" for k, (h, p) in net_targets(proc_dir).items()} if NET_ON else {},
            "latest": last, "series": series}


def run_net(store, stop):
    while not stop.wait(NET_INTERVAL):
        try:
            targets = net_targets()
            if targets:
                store.add_series(int(time.time()), net_round(targets))
        except Exception as e:  # noqa: BLE001
            print("Netz-Messung fehlgeschlagen:", e, flush=True)


# --- SMART (freiwillig, nur lesen: smartctl --scan und smartctl -a, nie Selbsttests, nie Schreiben)

# ab 1.17.0 auch /dev/sata1, /dev/sas1, /dev/usb1 (Synology DSM 7)
SMART_DEV = re.compile(r"/dev/(sd[a-z]{1,3}|hd[a-z]|nvme\d{1,3}(n\d{1,3})?|sg\d{1,3}|bus/\d{1,3}|(?:sata|sas|usb)\d{1,3})")
SMART_TYPE = re.compile(r"[a-z0-9][a-z0-9,+_-]{0,31}")
SMART_TIMEOUT = 60


def smartctl_args(op, dev=None, dtype=None):
    """Die einzigen Befehle, die der Agent kennt. «-n standby»: schlafende Platten nicht aufwecken (Unraid)."""
    if op == "scan":
        return ["smartctl", "--scan", "--json=c"]
    if op == "read" and isinstance(dev, str) and SMART_DEV.fullmatch(dev) and (dtype is None or (isinstance(dtype, str) and SMART_TYPE.fullmatch(dtype))):
        return ["smartctl", "--json=c", "-a", "-n", "standby"] + (["-d", dtype] if dtype else []) + [dev]
    return None


def run_smartctl(args):
    try:
        p = subprocess.run(args, capture_output=True, timeout=SMART_TIMEOUT, check=False)
        return p.returncode, p.stdout[:4_000_000].decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 126, json.dumps({"smartctl": {"messages": [{"string": f"{type(e).__name__}", "severity": "error"}]}})


def _smart_serve(conn):
    """Kleiner Helfer, der root bleibt (nur bei NZBLAB_SMART=1 und Start als root): nimmt über ein Socket-Paar nur
    {"op": "scan"} oder {"op": "read", "dev", "type"} an, prüft sie nochmals und startet nur smartctl mit festen Argumenten."""
    f = conn.makefile("rwb")
    for line in f:
        try:
            req = json.loads(line[:4096])
            args = smartctl_args(req.get("op"), req.get("dev"), req.get("type")) if isinstance(req, dict) else None
        except ValueError:
            args = None
        rc, out = run_smartctl(args) if args else (2, "")
        f.write(json.dumps({"rc": rc, "out": out}).encode() + b"\n")
        f.flush()


class SmartHelper:
    """Rechte trennen: Der Agent selbst läuft als PUID ohne Rechte; smartctl braucht root und CAP_SYS_RAWIO (Platten-
    Befehle über SG_IO). Darum spaltet main() vor dem Rechte-Abgeben einen Helfer ab, der nur smartctl ausführt."""

    def __init__(self):
        self.sock, self.pid, self.lock = None, None, threading.Lock()

    def start(self):
        if not (hasattr(os, "fork") and hasattr(os, "geteuid") and os.geteuid() == 0):
            return False
        parent, child = socket.socketpair()
        pid = os.fork()
        if pid == 0:                                   # Helfer
            parent.close()
            try:
                _smart_serve(child)
            finally:
                os._exit(0)
        child.close()
        self.sock, self.pid = parent, pid
        return True

    def call(self, op, dev=None, dtype=None):
        args = smartctl_args(op, dev, dtype)
        if args is None:
            return 2, ""
        if self.sock is None:
            return run_smartctl(args)
        with self.lock:
            try:
                self.sock.settimeout(SMART_TIMEOUT + 10)
                self.sock.sendall(json.dumps({"op": op, "dev": dev, "type": dtype}).encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    c = self.sock.recv(65536)
                    if not c:
                        raise OSError("SMART-Helfer beendet")
                    buf += c
                    if len(buf) > 20_000_000:
                        raise OSError("Antwort zu gross")
                r = json.loads(buf)
                return int(r.get("rc", 2)), str(r.get("out", ""))
            except (OSError, ValueError) as e:
                print("SMART-Helfer:", e, flush=True)
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
                return 126, ""


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_smart_scan(text):
    try:
        js = json.loads(text or "{}")
    except ValueError:
        return []
    out = []
    for d in js.get("devices") or []:
        if isinstance(d, dict) and SMART_DEV.fullmatch(str(d.get("name", ""))):
            t = d.get("type")
            out.append({"name": d["name"], "type": t if isinstance(t, str) and SMART_TYPE.fullmatch(t) else None,
                        "protocol": d.get("protocol", "")})
    return out


def parse_smart(text, dev=None):
    """smartctl --json -a → Werte mit den Namen, die die App von Scrutiny kennt (model_name, serial_number, power_on_hours,
    temp, attrs nach ID bzw. bei NVMe nach Namen wie media_errors, percentage_used). None, wenn kein JSON."""
    try:
        js = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(js, dict):
        return None
    d = js.get("device") or {}
    msgs = [m.get("string", "") for m in ((js.get("smartctl") or {}).get("messages") or []) if isinstance(m, dict)]
    standby = any(re.search(r"(?i)standby|sleep", m) for m in msgs)
    name = d.get("name") or dev or ""
    rot = _num(js.get("rotation_rate"))
    out = {"device": name, "device_name": name.rsplit("/", 1)[-1], "device_type": d.get("type", ""),
           "device_protocol": d.get("protocol", ""), "model_name": js.get("model_name") or js.get("scsi_model_name") or "",
           "serial_number": js.get("serial_number", ""), "firmware": js.get("firmware_version", ""),
           "capacity": _num((js.get("user_capacity") or {}).get("bytes")) or _num(js.get("nvme_total_capacity")),
           "rotational_speed": rot or 0, "smart_passed": (js.get("smart_status") or {}).get("passed"),
           "power_on_hours": _num((js.get("power_on_time") or {}).get("hours")), "power_cycle_count": _num(js.get("power_cycle_count")),
           "temp": _num((js.get("temperature") or {}).get("current")), "reallocated": None, "pending": None, "uncorrectable": None,
           "crc": None, "media_errors": None, "percentage_used": None, "unsafe_shutdowns": None, "standby": standby, "attrs": {},
           "error": next((m for m in msgs if m and not standby and re.search(r"(?i)denied|failed|unable|error", m)), "")[:200]}
    for a in ((js.get("ata_smart_attributes") or {}).get("table") or []):
        if not isinstance(a, dict) or not isinstance(a.get("id"), int):
            continue
        raw = a.get("raw") or {}
        out["attrs"][str(a["id"])] = {"attribute_id": a["id"], "name": a.get("name", ""), "value": _num(a.get("value")),
                                      "worst": _num(a.get("worst")), "thresh": _num(a.get("thresh")), "raw_value": _num(raw.get("value")),
                                      "raw_string": str(raw.get("string", ""))[:60], "failing": bool(a.get("when_failed"))}

    def raw(i):
        return (out["attrs"].get(str(i)) or {}).get("raw_value")
    out["reallocated"], out["pending"], out["uncorrectable"], out["crc"] = raw(5), raw(197), raw(198), raw(199)
    if out["power_cycle_count"] is None:
        out["power_cycle_count"] = raw(12)
    out["unsafe_shutdowns"] = raw(192)
    nv = js.get("nvme_smart_health_information_log")
    if isinstance(nv, dict):
        for k in ("media_errors", "percentage_used", "unsafe_shutdowns", "power_cycles", "data_units_read", "data_units_written",
                  "num_err_log_entries", "available_spare", "critical_warning"):
            if _num(nv.get(k)) is not None:
                out["attrs"][k] = {"attribute_id": k, "name": k, "raw_value": nv[k]}
        out["media_errors"], out["percentage_used"] = _num(nv.get("media_errors")), _num(nv.get("percentage_used"))
        out["unsafe_shutdowns"] = _num(nv.get("unsafe_shutdowns"))
        out["power_on_hours"] = out["power_on_hours"] if out["power_on_hours"] is not None else _num(nv.get("power_on_hours"))
        out["power_cycle_count"] = out["power_cycle_count"] if out["power_cycle_count"] is not None else _num(nv.get("power_cycles"))
        out["temp"] = out["temp"] if out["temp"] is not None else _num(nv.get("temperature"))
    if _num(js.get("scsi_grown_defect_list")) is not None:          # SAS/SCSI: gewachsene Defektliste ≈ neu zugewiesen
        out["reallocated"] = js["scsi_grown_defect_list"]
    ecl = js.get("scsi_error_counter_log")
    if isinstance(ecl, dict):
        unc = [_num((ecl.get(k) or {}).get("total_uncorrected_errors")) for k in ("read", "write", "verify")]
        if any(x is not None for x in unc):
            out["uncorrectable"] = sum(x or 0 for x in unc)
    return out


def smart_day_row(p):
    """Tageswert mit den Namen der App-Frühwarnung (DiskDay)."""
    return {"device": p.get("device_name"), "model": p.get("model_name"), "realloc": p.get("reallocated"), "pending": p.get("pending"),
            "uncorr": p.get("uncorrectable"), "crc": p.get("crc"), "media": p.get("media_errors"), "temp_max": p.get("temp"),
            "hours": p.get("power_on_hours"), "wear": p.get("percentage_used"), "passed": p.get("smart_passed")}


class SmartMonitor:
    def __init__(self, store, helper=None, enabled=None):
        self.store, self.helper = store, helper or SmartHelper()
        self.enabled = SMART_ON if enabled is None else enabled
        self.latest, self.checked, self.error, self.lock = {}, 0, "", threading.Lock()

    def poll(self, now=None):
        now = int(now or time.time())
        rc, out = self.helper.call("scan")
        devs = parse_smart_scan(out)
        err = ""
        if rc == 127:
            err = "smartctl fehlt im Image"
        elif not devs:
            err = "Keine Platten gefunden: im Compose-Block «devices» die Platten freigeben (siehe README, SMART)"
        latest = {}
        for d in devs:
            rc, out = self.helper.call("read", d["name"], d["type"])
            p = parse_smart(out, d["name"])
            if p is None:
                continue
            old = self.latest.get(d["name"])
            if p["standby"] and old:
                p = dict(old, standby=True)          # schlafende Platte: letzter Stand bleibt, nicht aufwecken
            latest[d["name"]] = p
            if not p["standby"] and (p["serial_number"] or p["model_name"]):
                self.store.add_smart_day(p["serial_number"] or p["device_name"], smart_day_row(p), now)
        if devs and not latest:
            err = "smartctl lieferte keine Werte (Rechte: cap_add SYS_RAWIO, Platten unter devices, PUID-Start als root)"
        with self.lock:
            self.latest, self.checked, self.error = latest, now, err
        return latest

    def current(self):
        with self.lock:
            return {"enabled": self.enabled, "checked": self.checked or None, "error": self.error if self.enabled else
                    "SMART ist aus (NZBLAB_SMART=1 und SMART-Block im Compose)", "disks": list(self.latest.values())}

    def history(self, days, now=None):
        now = int(now or time.time())
        return {"enabled": self.enabled, "days": days,
                "disks": [{"serial": k, "model": (rows[-1].get("model") or ""), "device": rows[-1].get("device") or "", "rows": rows}
                          for k, rows in self.store.smart_days(now - days * 86400).items()]}


def run_smart(smart, stop):
    if stop.wait(20):
        return
    while True:
        try:
            smart.poll()
        except Exception as e:  # noqa: BLE001
            print("SMART fehlgeschlagen:", e, flush=True)
        if stop.wait(SMART_EVERY):
            return


# --- Backups erkennen (nur lesen, aus Dateien und Dateizeiten; restic/borg/Kopia werden nie gestartet)

BACKUP_WORDS = re.compile(r"(?i)backup|restic|borg|kopia|sicherung|rsync|snapshot|duplicati|timemachine|time.machine|bak")
BACKUP_OK = re.compile(r"(?i)(sent [\d,.]+ bytes\s+received|total size is [\d,.]+|speedup is|backup (completed|finished|successful|done|ok)|"
                       r"snapshot [0-9a-f]+ saved|archive fingerprint|created snapshot|sicherung (erfolgreich|abgeschlossen|fertig))")
BACKUP_ERR = re.compile(r"(?i)(rsync error|rsync: \[|\berror\b|\bfailed\b|fatal|permission denied|no space left|fehlgeschlagen|abgebrochen)")
LOG_TIME_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")
LOG_TIME_BSD = re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2})")


def _newest_mtime(paths):
    best = None
    for p in paths:
        try:
            m = int(os.stat(p).st_mtime)
        except OSError:
            continue
        best = m if best is None or m > best else best
    return best


def _listdir(p):
    try:
        return os.listdir(p)
    except OSError:
        return []


def detect_repo(path):
    """restic, borg oder kopia (Dateisystem-Repository) an seinen Kennzeichen erkennen, sonst None."""
    if os.path.isfile(os.path.join(path, "kopia.repository.f")):
        return "kopia"
    if all(os.path.isdir(os.path.join(path, x)) for x in ("snapshots", "data", "keys")) and os.path.isfile(os.path.join(path, "config")):
        return "restic"
    if os.path.isfile(os.path.join(path, "README")) and "borg backup repository" in read_head(os.path.join(path, "README"), 300).lower():
        return "borg"
    return None


def repo_info(kind, path, now=None):
    """Letzte Sicherung aus Dateizeiten: restic = neueste Datei in snapshots/, borg = index/hints/integrity (bei jedem
    Abschluss neu geschrieben), Kopia = neueste Index- und Metadaten-Blobs (x…, q…). Inhalt ist verschlüsselt: nie gelesen."""
    if kind == "restic":
        snaps = [os.path.join(path, "snapshots", f) for f in _listdir(os.path.join(path, "snapshots"))]
        return {"last_success": _newest_mtime(snaps), "snapshots": len(snaps), "source": "restic: Dateizeit in snapshots/"}
    if kind == "borg":
        files = [os.path.join(path, f) for f in _listdir(path) if re.match(r"(index|hints|integrity)\.\d+$", f)]
        return {"last_success": _newest_mtime(files), "source": "borg: Dateizeit von index/hints"}
    files, budget = [], 5000
    for top in _listdir(path):
        if not top.startswith(("x", "q")):
            continue
        p = os.path.join(path, top)
        if os.path.isfile(p):
            files.append(p)
            continue
        for root, dirs, fs in os.walk(p):
            files += [os.path.join(root, f) for f in fs]
            budget -= len(fs) + 1
            if budget <= 0:
                break
    return {"last_success": _newest_mtime(files), "source": "kopia: Dateizeit der Index-/Metadaten-Blobs"}


def _log_time(line, fallback):
    m = LOG_TIME_ISO.search(line[:40])
    try:
        if m:
            return int(time.mktime(time.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")))
        m = LOG_TIME_BSD.match(line)
        if m:
            year = time.localtime().tm_year
            t = int(time.mktime(time.strptime(f"{year} {' '.join(m.group(1).split())}", "%Y %b %d %H:%M:%S")))
            return t if t <= time.time() + 86400 else int(time.mktime(time.strptime(f"{year - 1} {' '.join(m.group(1).split())}", "%Y %b %d %H:%M:%S")))
    except (ValueError, OverflowError):
        pass
    return fallback


def backup_log_info(path):
    """Log-Datei eines Backup-Skripts (rsync, cron …): Zeitpunkt der letzten Erfolgs- und Fehlerzeile (letzte 256 KB)."""
    try:
        mtime = int(os.stat(path).st_mtime)
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 262_144))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    ok = err = None
    err_text = ""
    for line in lines:
        if BACKUP_OK.search(line):
            ok = _log_time(line, mtime)
        elif BACKUP_ERR.search(line):
            err, err_text = _log_time(line, mtime), redact_log(line.strip()[:LOG_LINE_MAX])[:200]
    kind = "rsync" if "rsync" in os.path.basename(path).lower() or any("rsync" in l for l in lines[-200:]) else "log"
    return {"kind": kind, "last_success": ok, "last_error": err, "error_text": err_text, "source": "Log-Datei"}


def _host_path(p):
    """Pfad im Container → Pfad auf dem NAS, so gut es geht (nur Anzeige)."""
    return p[len("/host"):] if p.startswith("/host/mnt/") else p


def scan_backups(explicit=None, auto_roots=None, max_depth=3, budget=2000, deadline_s=20.0):
    """Explizite Pfade (Ordner = Repository oder Ordner mit Repositories, Datei = Log) und automatisch gefundene
    Repositories unter den eingebundenen Ordnern. Automatisch: oberste Ebene jeder Freigabe, darunter nur Ordner mit
    Backup-Namen (backup, restic, borg, kopia, sicherung, rsync …), höchstens 3 Ebenen tief."""
    found, seen, t_end = [], set(), time.monotonic() + deadline_s

    def add(kind, path, info, origin):
        rp = os.path.realpath(path)
        if rp in seen:
            return
        seen.add(rp)
        found.append({"kind": kind, "name": os.path.basename(path.rstrip("/")) or path, "path": _host_path(path),
                      "last_success": info.get("last_success"), "last_error": info.get("last_error"),
                      "error_text": info.get("error_text", ""), "snapshots": info.get("snapshots"),
                      "source": info.get("source", "unknown"), "found_by": origin})

    def visit(path, depth, origin, only_named):
        nonlocal budget
        if budget <= 0 or time.monotonic() > t_end:
            return
        budget -= 1
        kind = detect_repo(path)
        if kind:
            return add(kind, path, repo_info(kind, path), origin)
        if depth >= max_depth:
            return
        for name in sorted(_listdir(path))[:500]:
            p = os.path.join(path, name)
            if name.startswith(".") or os.path.islink(p):
                continue
            if os.path.isdir(p):
                if not only_named or BACKUP_WORDS.search(name) or BACKUP_WORDS.search(os.path.basename(path)):
                    visit(p, depth + 1, origin, only_named)
            elif origin == "path" and name.endswith(".log") and BACKUP_WORDS.search(name):
                info = backup_log_info(p)
                if info:
                    add(info["kind"], p, info, origin)

    for p in explicit or []:
        if os.path.isfile(p):
            info = backup_log_info(p)
            if info:
                add(info["kind"], p, info, "path")
        elif os.path.isdir(p):
            visit(p, 0, "path", False)
        else:
            found.append({"kind": "unknown", "name": os.path.basename(p.rstrip("/")) or p, "path": _host_path(p), "last_success": None,
                          "last_error": None, "error_text": "Pfad nicht gefunden (im Compose nur lesend einbinden)", "snapshots": None,
                          "source": "unknown", "found_by": "path"})
    for root in auto_roots or []:
        for share in sorted(_listdir(root))[:200]:
            p = os.path.join(root, share)
            if os.path.isdir(p) and not share.startswith(".") and not top_excluded(share, FILES_EXCLUDE - {"appdata"}):
                visit(p, 0, "auto", True)
    return found


class BackupScanner:
    """Ergebnis wird im Hintergrund gerechnet (explizite Pfade stündlich, Suche unter den Freigaben alle 6 Stunden,
    wie der Datei-Index), Anfragen bekommen sofort den letzten Stand."""

    def __init__(self, explicit=None, auto_roots=None, auto=None):
        self.explicit = BACKUP_PATHS if explicit is None else explicit
        self.auto = BACKUP_AUTO if auto is None else auto
        self.auto_roots = auto_roots if auto_roots is not None else [r for r in (FILES_ROOT, HOST_FS) if os.path.isdir(r)]
        self.items, self.auto_items, self.checked, self.auto_checked = [], [], 0, 0
        self.lock = threading.Lock()

    def enabled(self):
        return bool(self.explicit) or (self.auto and bool(self.auto_roots))

    def refresh(self, auto=False):
        items = scan_backups(self.explicit)
        auto_items = scan_backups(auto_roots=self.auto_roots) if auto and self.auto else None
        with self.lock:
            self.items, self.checked = items, int(time.time())
            if auto_items is not None:
                self.auto_items, self.auto_checked = auto_items, int(time.time())

    def get(self):
        with self.lock:
            paths = {i["path"] for i in self.items}
            items = self.items + [i for i in self.auto_items if i["path"] not in paths]
            return {"enabled": self.enabled(), "checked": self.checked or None, "auto": self.auto, "auto_checked": self.auto_checked or None,
                    "paths": [_host_path(p) for p in self.explicit], "backups": items,
                    "note": "Zeiten aus Dateizeiten und Log-Zeilen; last_error nur aus Logs, sonst null (unbekannt)"}


def run_backups(scanner, stop):
    n = 0
    if stop.wait(30):
        return
    while True:
        try:
            if scanner.enabled():
                scanner.refresh(auto=n % 6 == 0)
        except Exception as e:  # noqa: BLE001
            print("Backup-Suche fehlgeschlagen:", e, flush=True)
        n += 1
        if stop.wait(3600):
            return


# --- RAID und Pools: mdadm (/proc/mdstat), ZFS (kstat, zpool falls vorhanden), Btrfs (/sys/fs/btrfs)

MD_HEAD = re.compile(r"^(md\S*)\s*:\s*(\S+)\s*(?:\((auto-read-only|read-only)\)\s*)?(.*)$")
MD_DEV = re.compile(r"^(\S+?)\[(\d+)\]((?:\([A-Z]\))*)$")
MD_COUNT = re.compile(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]")
MD_SYNC = re.compile(r"(resync|recovery|check|reshape|repair)\s*=\s*([\d.]+)%(?:.*?finish=([\d.]+)min)?")
MD_WAIT = re.compile(r"(resync|recovery|check|reshape)\s*=\s*(DELAYED|PENDING)")


def parse_mdstat(text):
    """Linux-Software-RAID (mdadm). Unraid hat ein eigenes Format (mdState=…): dafür gibt es /api/array."""
    if not text:
        return []
    if "mdState=" in text:
        kv = dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
        return [{"kind": "unraid", "name": "array", "state": kv.get("mdState", ""), "degraded": None, "details": "/api/array"}]
    out, cur = [], None
    for line in text.splitlines():
        m = MD_HEAD.match(line)
        if m and not line.startswith(("Personalities", "unused")):
            rest = m.group(4).split()
            level = rest[0] if rest and not MD_DEV.match(rest[0]) else ""
            devs = []
            for tok in rest[1 if level else 0:]:
                dm = MD_DEV.match(tok)
                if dm:
                    flags = dm.group(3)
                    devs.append({"name": dm.group(1), "role": int(dm.group(2)), "failed": "(F)" in flags, "spare": "(S)" in flags})
            cur = {"kind": "mdadm", "name": m.group(1), "state": m.group(2) + (f" ({m.group(3)})" if m.group(3) else ""), "level": level,
                   "devices": devs, "total": None, "active": None, "degraded": any(d["failed"] for d in devs) or m.group(2) == "inactive",
                   "sync": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        c = MD_COUNT.search(line)
        if c:
            cur["total"], cur["active"] = int(c.group(1)), int(c.group(2))
            cur["degraded"] = cur["degraded"] or cur["active"] < cur["total"] or "_" in c.group(3)
        s = MD_SYNC.search(line)
        if s:
            cur["sync"] = {"action": s.group(1), "percent": float(s.group(2)), "finish_min": float(s.group(3)) if s.group(3) else None}
        w = MD_WAIT.search(line)
        if w:
            cur["sync"] = {"action": w.group(1), "percent": None, "finish_min": None, "waiting": w.group(2).lower()}
    return out


def zfs_pools(proc_dir=None, run=None):
    """ZFS-Pools: Zustand aus /proc/spl/kstat/zfs/<pool>/state (ab OpenZFS 0.8, ohne Werkzeuge). Ist «zpool» im Container
    vorhanden (normal nicht), wird es zusätzlich gefragt. (pools, Quelle)."""
    proc_dir = proc_dir or HOST_PROC
    base = f"{proc_dir}/spl/kstat/zfs"
    pools = []
    for name in sorted(_listdir(base)):
        st = read(f"{base}/{name}/state")
        if st:
            pools.append({"kind": "zfs", "name": name, "state": st, "degraded": st != "ONLINE"})
    if pools:
        return pools, "kstat"
    import shutil
    if run is None and shutil.which("zpool"):
        def run():
            return subprocess.run(["zpool", "list", "-H", "-o", "name,health"], capture_output=True, timeout=15, check=False).stdout.decode()
    if run is not None:
        try:
            for line in run().splitlines():
                f = line.split("\t")
                if len(f) == 2:
                    pools.append({"kind": "zfs", "name": f[0], "state": f[1], "degraded": f[1] != "ONLINE"})
            return pools, "zpool"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return pools, ("none" if os.path.isdir(base) else "unavailable")


def btrfs_pools(sys_dir=None):
    """Btrfs aus /sys/fs/btrfs/<uuid>: Name, Profile (raid1, raid5 …), Geräte, fehlende Geräte, Fehlerzähler je Gerät."""
    base = f"{sys_dir or HOST_SYS}/fs/btrfs"
    out = []
    for uuid in sorted(_listdir(base)):
        if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
            continue
        d = f"{base}/{uuid}"
        profiles = {}
        for kind in ("data", "metadata", "system"):
            prof = [p for p in _listdir(f"{d}/allocation/{kind}") if re.fullmatch(r"single|dup|raid\d+c?\d*", p)]
            if prof:
                profiles[kind] = ",".join(sorted(prof))
        devs, errors = [], 0
        for devid in sorted(_listdir(f"{d}/devinfo"), key=lambda x: int(x) if x.isdigit() else 0):
            stats = {}
            for line in (read(f"{d}/devinfo/{devid}/error_stats", "") or "").splitlines():
                k, _, v = line.partition(" ")
                if v.strip().isdigit():
                    stats[k] = int(v)
            errors += sum(stats.values())
            devs.append({"devid": int(devid) if devid.isdigit() else devid, "missing": read(f"{d}/devinfo/{devid}/missing", "0") == "1",
                         "errors": stats})
        out.append({"kind": "btrfs", "name": read(f"{d}/label", "") or uuid[:8], "uuid": uuid, "profiles": profiles,
                    "devices": devs, "members": sorted(_listdir(f"{d}/devices")), "errors": errors,
                    "degraded": any(x["missing"] for x in devs)})
    return out


def raid_status(proc_dir=None, sys_dir=None):
    proc_dir = proc_dir or HOST_PROC
    md = parse_mdstat(read(f"{proc_dir}/mdstat", "") or "")
    zfs, zsrc = zfs_pools(proc_dir)
    bt = btrfs_pools(sys_dir)
    pools = md + zfs + bt
    return {"pools": pools, "degraded": any(p.get("degraded") for p in pools),
            "sources": {"mdadm": os.path.exists(f"{proc_dir}/mdstat"), "zfs": zsrc, "btrfs": os.path.isdir(f"{sys_dir or HOST_SYS}/fs/btrfs")}}


# --- Log-Auszüge (freiwillig: NZBLAB_LOGS=1, /var/log nur lesend). Vor der Antwort lokal gefiltert, nie von selbst verschickt

_REDACT = [
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)[^/\s:@]+:[^/\s@]+@"), r"\1<user>:<redacted>@"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}"), "Bearer <redacted>"),
    # ab 1.16.1 (Audit Lauf 2): Cookie-Kopfzeilen ganz schwärzen (vorher nur das erste «a=b» bis zum «;»)
    (re.compile(r"(?i)\b((?:set-)?cookie)(\s*:\s*)[^\r\n]+"), r"\1\2<redacted>"),
    # ab 1.16.1: auch nach «_» (access_token, client_secret), mit JSON-Anführungszeichen ("apiKey":"…") und samt Schema
    # («Authorization: Basic …» schwärzte vorher nur das Wort «Basic»)
    (re.compile(r"(?i)(?<![a-z0-9])(pass(?:word|wd|phrase)?|pwd|token|api[_-]?key|apikey|secret|auth[a-z]*|private[_-]?key|access[_-]?key|session(?:id)?|"
                r"cookie|sig(?:nature)?|key)(\"?\s*[=:]\s*)((?:(?:basic|digest|token)\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+))"), r"\1\2<redacted>"),
    # ab 1.16.1 (Audit Lauf 2): Anfang nur am Wortanfang und Länge begrenzt, sonst quadratisch auf langen Wörtern ohne «@»
    (re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}"), "<email>"),
    (re.compile(r"(?i)(?<![0-9a-f:])[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5,}(?![0-9a-f:])"), "<mac>"),
    (re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"), "<ip>"),
    (re.compile(r"(?i)(?<![\w:.])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?:%\w+)?(?![\w:])"), None),      # IPv6, siehe _ipv6
    # Wörter nach «user»/«login», die kein Benutzername sind («login failed for user=bob»: «failed» bleibt stehen)
    (re.compile(r"(?i)\b(user(?:name)?|login|for user|invalid user|illegal user|uid)([=: ]+)"
                r"(?!(?:failed|failure|fail|error|errors|not|is|was|has|have|logged|authentication|auth|attempt|attempts|denied|ok|"
                r"unknown|name|id|from|for|by|on|in|at|and|the|a)\b)([^\s,;:'\"()\[\]]+)"), r"\1\2<user>"),
    # ab 1.18.0 (VM-Test M2): PAM-/sshd-Zeilen allgemein: «authentication failure for bob from …», «Failed password for bob from …»,
    # «Accepted publickey for bob from …», «error: maximum authentication attempts exceeded for bob from …»
    (re.compile(r"(?i)\b((?:authentication failure|failed|failure|accepted|invalid|illegal|refused|denied|error|session (?:opened|closed))\b"
                r"[^\r\n]{0,80}?\bfor )(?!invalid user\b|illegal user\b|user\b)[^\s]+(?= from\b)"), r"\1<user>"),
    # PAM: «logname=… ruser=… rhost=…» (leere Werte bleiben leer; eine schon geschwärzte IP bleibt «<ip>»)
    (re.compile(r"(?i)\b(logname|ruser)(=)(?!<)[^\s]+"), r"\1\2<user>"),
    (re.compile(r"(?i)\b(rhost)(=)(?!<)[^\s]+"), r"\1\2<host>"),
    (re.compile(r"(/(?:home|Users))/[^/\s]+"), r"\1/<user>"),
    (re.compile(r"(/mnt/(?:user|disk\d+|cache[^/\s]*|remotes|disks)/[^/\s]+)/[^\s'\"]+"), r"\1/…"),
    (re.compile(r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{32,}={0,2}"), "<secret>"),
]


def _ipv6(m):
    s = m.group(0)
    return "<ip>" if "::" in s or s.count(":") >= 4 else s       # «12:34:56» (Uhrzeit) bleibt


_SECRET_RULES = _REDACT[:4] + _REDACT[-1:]      # Adress-Zugangsdaten, Bearer, Cookie-Zeilen, key=wert, lange Zufallsketten


def redact_secrets(s):
    """Nur Geheimnisse schwärzen (Zugangsdaten in Adressen, Bearer-Tokens, key/passwort=wert, lange Zufallsketten);
    IPs, E-Mails und Pfade bleiben. Für Absturzberichte (ab 1.16.1)."""
    for rx, rep in _SECRET_RULES:
        s = rx.sub(rep, s)
    return s


def redact_log(s):
    """Entfernt IP-Adressen (v4/v6), MAC-Adressen, E-Mails, Tokens, Schlüssel und Passwörter (key=wert, Bearer, user:pass@),
    Benutzernamen, Heimordner und Datei-/Ordnernamen unter den Freigaben, lange Zufallsketten."""
    for rx, rep in _REDACT:
        s = rx.sub(_ipv6 if rep is None else rep, s)
    return s


LOG_LINE_MAX = 4000          # ab 1.16.1: längere Logzeilen vor dem Schwärzen kürzen (Rechenzeit bleibt begrenzt)
LOG_FILES = ("syslog", "messages", "kern.log", "daemon.log", "syslog.1", "messages.1")
LOG_ERR = re.compile(r"(?i)(i/o error|\berror\b|\berr\b|\bfail(ed|ure)?\b|critical|\bcrit\b|panic|segfault|out of memory|oom-kill|call trace|corrupt|"
                     r"read-only file system|\bemerg|\balert\b)")
LOG_WARN = re.compile(r"(?i)(\bwarn(ing)?\b|timed out|timeout|degraded|retry|retrying|not responding|link is down|throttl|deprecated)")


def collect_logs(log_dir=None, hours=24, limit=200, now=None):
    """Fehler- und Warnzeilen der letzten Stunden aus Text-Logs (syslog, messages, kern.log, daemon.log), gefiltert und
    zusammengefasst (gleiche Meldung = eine Zeile mit Anzahl). Journald-Binärdateien werden nicht gelesen."""
    log_dir = log_dir or LOG_DIR
    now = int(now or time.time())
    since = now - hours * 3600
    seen, files = {}, []
    for name in LOG_FILES:
        path = os.path.join(log_dir, name)
        try:
            if not os.path.isfile(path) or os.stat(path).st_mtime < since:
                continue
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4_000_000))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        files.append(name)
        for line in lines:
            err = LOG_ERR.search(line)
            if not err and not LOG_WARN.search(line):
                continue
            if Today.SYSLOG_SKIP.search(line):
                continue
            t = _log_time(line, None)
            if t is None or t < since or t > now + 300:
                continue
            m = LOG_TIME_ISO.match(line)
            rest = re.sub(r"^\S*", "", line[m.end():]) if m else ""        # Sekundenbruchteile und Zeitzone weg
            if not m:
                m = LOG_TIME_BSD.match(line)
                rest = line[m.end():] if m else line
            rest = rest.strip()
            if m and re.match(r"^[^\s:]+\s+\S+?(\[\d+\])?:", rest):
                rest = re.sub(r"^\S+\s+", "", rest, count=1)                # Rechnername weg
            src, sep, msg = rest.partition(": ")
            if not sep:
                src, msg = "", rest
            msg = redact_log(msg.strip()[:LOG_LINE_MAX])[:300]
            src = re.sub(r"\[\d+\]$", "", src.strip())[:40]
            key = (src, re.sub(r"\d+", "#", msg)[:120])
            if key in seen:
                seen[key]["n"] += 1
                seen[key]["t"] = max(seen[key]["t"], t)
                continue
            seen[key] = {"t": t, "level": "err" if err else "warn", "src": redact_log(src), "text": msg, "n": 1}
    lines = sorted(seen.values(), key=lambda e: -e["t"])[:limit]
    return {"enabled": True, "hours": hours, "files": files, "lines": lines, "redacted": True,
            "mover": mover_log(log_dir, now),
            "note": "IP, MAC, E-Mail, Tokens/Passwörter, Benutzer und Pfade sind entfernt"}


# Mover-Zeilen von Unraid im Systemprotokoll (ab 1.17.0, für «Mover hängt» ohne Raten): das Skript schreibt «mover: started»
# und «mover: finished», «mover stop» meldet «mover: stopped». Unraid leitet es mit Kennzeichen «move» weiter («move: mover: started»).
MOVER_LINE = re.compile(r"(?i)\bmove(?:r)?:\s*(?:mover:\s*)?(started|finished|completed|done|stopped|not running|exit(?:ed|ing)?)\b")


def mover_log(log_dir=None, now=None, max_age=12 * 3600):
    """Letzter Mover-Start und -Ende aus syslog/messages. {"state": "running"|"finished"|None, "started": t, "ended": t}.
    «running» = Start ohne Ende-Zeile danach und nicht älter als 12 Stunden. Keine Dateinamen, nichts Geschwärztes nötig."""
    log_dir = log_dir or LOG_DIR
    now = int(now or time.time())
    started = ended = None
    for name in ("syslog", "messages"):
        path = os.path.join(log_dir, name)
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4_000_000))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if "move" not in line.lower() or "file:" in line:
                continue
            m = MOVER_LINE.search(line)
            if not m:
                continue
            t = _log_time(line, None)
            if t is None or t > now + 300:
                continue
            if m.group(1).lower() == "started":
                if started is None or t > started:      # gleiche Sekunde (doppelte Zeile in syslog und messages) setzt das Ende nicht zurück
                    started, ended = t, None
            elif started is not None and t >= started and ended is None:
                ended = t
    if started is None:
        return {"state": None, "started": None, "ended": None}
    state = "finished" if ended is not None else ("running" if now - started <= max_age else None)
    return {"state": state, "started": started, "ended": ended}


def agent_features(smart=None, backups=None, log_dir=None):
    feats = ["boots", "disktemps", "health"]
    if SMART_ON or (smart is not None and smart.enabled):
        feats.append("smart")
    if backups is not None and backups.enabled():
        feats.append("backups")
    try:
        r = cached("raid", raid_status, ttl=60)
        if r["pools"] or r["sources"]["mdadm"] or r["sources"]["btrfs"] or r["sources"]["zfs"] != "unavailable":
            feats.append("raid")
    except Exception:  # noqa: BLE001
        pass
    if NET_ON:
        feats.append("net")
    if LAN_ON:
        feats.append("lan")
    if LOGS_ON and os.path.isdir(log_dir or LOG_DIR):
        feats.append("logs")
    feats.append("discover")                 # ab 1.16.0: /api/discover; «discover_keys» = liefert auch API-Keys
    if DISCOVER_KEYS:
        feats.append("discover_keys")
    return feats


def rate_key(ip):
    """Schlüssel für die Bremsen: IPv4 und IPv4-in-IPv6 je Adresse, IPv6 je /64 (ein Absender hat dort Milliarden Adressen)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    m = getattr(a, "ipv4_mapped", None)
    if m is not None:
        return str(m)
    if a.version == 6:
        return str(ipaddress.ip_network((a, 64), strict=False))
    return str(a)


class AuthLimiter:
    """Bremse gegen Token-Raten: höchstens AUTH_FAILS falsche Tokens pro Minute und Absender-IP, danach 5 Minuten 429."""

    def __init__(self, max_fail=None, window=60, block=300):
        self.max_fail, self.window, self.block = max_fail or AUTH_FAILS, window, block
        self.fails, self.blocked, self.lock = {}, {}, threading.Lock()
        self._pruned = float("-inf")

    def blocked_for(self, ip, now=None):
        now = now if now is not None else time.monotonic()
        ip = rate_key(ip)
        with self.lock:
            until = self.blocked.get(ip)
            if until is None:
                return 0
            if until <= now:
                self.blocked.pop(ip, None)
                return 0
            return int(until - now) + 1

    def fail(self, ip, now=None):
        now = now if now is not None else time.monotonic()
        ip = rate_key(ip)
        with self.lock:
            # Viele Absender: alte Einträge weg, Speicher bleibt klein. Ab 1.16.1 höchstens einmal pro Sekunde aufräumen
            # (vorher bei über 10000 Einträgen bei jedem Fehlversuch, unter derselben Sperre wie jede Token-Prüfung)
            if (len(self.fails) > 10000 or len(self.blocked) > 10000) and now - self._pruned >= 1:
                self._pruned = now
                self.fails = {k: v for k, v in self.fails.items() if v and v[-1] > now - self.window}
                self.blocked = {k: v for k, v in self.blocked.items() if v > now}
            lst = [x for x in self.fails.get(ip, []) if x > now - self.window] + [now]
            if len(lst) >= self.max_fail:
                self.blocked[ip] = now + self.block
                self.fails.pop(ip, None)
                print(f"Token-Bremse: {len(lst)} falsche Tokens in {self.window} s von einer Adresse, 5 Minuten gesperrt", flush=True)
                return True
            self.fails[ip] = lst
            return False



# ---------------------------------------------------------------- Ab 1.16.0: Dienste finden («Alles in einer Minute verbunden»)

class DiscoverLimiter:
    """Höchstens [max_calls] Abrufe von /api/discover pro [window] Sekunden, für alle Absender zusammen (gegen Durchprobieren)."""

    def __init__(self, max_calls=10, window=60):
        self.max_calls, self.window = max_calls, window
        self.calls, self.lock = [], threading.Lock()

    def allow(self, now=None):
        """0 = erlaubt (und gezählt), sonst Sekunden bis zum nächsten freien Abruf."""
        now = now if now is not None else time.monotonic()
        with self.lock:
            self.calls = [t for t in self.calls if t > now - self.window]
            if len(self.calls) >= self.max_calls:
                return max(1, int(self.calls[0] + self.window - now) + 1)
            self.calls.append(now)
            return 0


DISCOVER_DEPTH = 3                  # so viele Ordner-Ebenen unter dem Container-Ordner
DISCOVER_MAX_BYTES = 1024 * 1024    # grössere Einstellungsdateien nicht lesen
DISCOVER_MAX_ENTRIES = 3000         # Einträge pro Container-Ordner (Plex-Bibliotheken sind riesig)
DISCOVER_MAX_FOLDERS = 500
DISCOVER_BUDGET_S = 8.0             # ab 1.16.1: so lange höchstens suchen (App-Zeitlimit 10 s), dann partial: true
DISCOVER_CACHE_S = 60               # ab 1.16.1: Ergebnis ohne Schlüssel (?keys=0) so lange wiederverwenden
DISCOVER_FILES = {"config.xml", "sabnzbd.ini", "config.ini", "settings.json", "preferences.xml", "network.xml", "system.xml"}
# Grosse Datenordner gar nicht erst betreten (Vergleich klein geschrieben)
DISCOVER_SKIP = {"cache", "caches", "logs", "log", "backups", "backup", "mediacover", "metadata", "media", "transcode",
                 "transcodes", "crash reports", "codecs", "plug-in support", "plug-ins", "updates", "drivers", "tmp", "temp",
                 "node_modules", "diagnostics", "thumbnails", "images", "db", "database", "admin",
                 "pgdata", "postgres", "postgresql", "mysql", "mariadb", "model-cache", "model_cache"}
# Datenbank-Container (Postgres, MySQL/MariaDB): Ordner mit diesen Einträgen nicht weiter durchsuchen (riesige data-Ordner)
DISCOVER_DB_MARKERS = {"pg_version", "pgdata", "postgresql.conf", "ibdata1", "mysql", "mariadb", "mysql.ibd", "auto.cnf"}
# Ordnernamen, die einen Dienst vermuten lassen (für «Ordner nicht lesbar»); Namen mit Zusatzwerkzeugen zählen nicht
DISCOVER_NAME_HINTS = (("sabnzbd", "sab", 8080, None), ("tautulli", "tautulli", 8181, None), ("jellyseerr", "seerr", 5055, "jellyseerr"),
                       ("overseerr", "seerr", 5055, "overseerr"), ("seerr", "seerr", 5055, "seerr"), ("plex", "plex", 32400, None),
                       ("jellyfin", "jellyfin", 8096, "jellyfin"), ("emby", "jellyfin", 8096, "emby"))
DISCOVER_NAME_NOT = ("meta", "auto", "tauticord", "trakt", "rating", "wrapped", "debrid", "request", "utilit", "kometa", "ombi")
# Aussehen wie Tautulli ([General] http_root), sind es aber nicht
DISCOVER_NOT_TAUTULLI = ("headphones", "lazylibrarian", "mylar", "lazy-librarian", "sickchill", "sickgear", "sickrage", "couchpotato")
DISCOVER_OWN = {"nzblab-agent", "naslab-agent"}
DISCOVER_KEY_RX = re.compile(r"[A-Za-z0-9_\-+/=]{16,128}")
DISCOVER_ARR = (("prowlarr", 9696), ("sonarr", 8989), ("radarr", 7878))
DISCOVER_OTHER_ARR = ("lidarr", "readarr", "whisparr", "mylar", "bazarr")
DISCOVER_NOTES = {
    "ok": "Schlüssel gefunden",
    "keys_off": "Auslesen im Agent ausgeschaltet (DISCOVER_KEYS=0)",
    "plex_token": "Gefunden; Plex-Token selbst eintragen (Konto-Schlüssel, der Agent liest ihn nie aus)",
    "jellyfin_key": "Gefunden; Schlüssel in der Jellyfin-Weboberfläche erstellen (Dashboard › API-Schlüssel)",
    "emby_key": "Gefunden; Schlüssel in der Emby-Weboberfläche erstellen (Einstellungen › API-Schlüssel)",
    "unreadable": "Einstellungsdatei nicht lesbar (Rechte: PUID=0, oder grösser als 1 MB)",
    "unreadable_dir": "Ordner nicht lesbar (Rechte: PUID=0 setzen und den Container neu starten)",
    "broken": "Einstellungsdatei kaputt oder ohne gültigen Schlüssel",
}
DISCOVER_PORTS_NOTE = ("Ports stammen aus den Einstellungsdateien und gelten im Container; im Bridge-Netz kann der Port "
                       "am NAS anders sein (Docker-Portzuordnung).")


def _disc_read(path):
    """Datei lesen, ohne einem Symlink zu folgen (O_NOFOLLOW), nur normale Dateien bis 1 MB. None = nicht lesbar."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > DISCOVER_MAX_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(DISCOVER_MAX_BYTES + 1)
        if len(data) > DISCOVER_MAX_BYTES:
            return None
        return data.decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _disc_xml(text, root_tag):
    """Einfaches XML (Kinder ohne Namensraum) → {Name: Text}. Kein DOCTYPE/ENTITY (keine Entity-Tricks)."""
    if text is None:
        return None
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return {}
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        return {}
    if root.tag != root_tag:
        return {}
    return {c.tag: (c.text or "").strip() for c in root if isinstance(c.tag, str)}


def _disc_ini(text):
    """ConfigObj-Datei (SABnzbd, Tautulli) → {abschnitt: {schlüssel: wert}}, nur oberste Abschnitte ([[…]] wird übersprungen)."""
    if text is None:
        return None
    out, sec = {}, None
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith(("#", ";")):
            continue
        if t.startswith("[["):
            sec = None
            continue
        if t.startswith("[") and t.endswith("]"):
            sec = t[1:-1].strip().lower()
            out.setdefault(sec, {})
            continue
        if sec is not None and "=" in t:
            k, v = t.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[sec][k.strip().lower()] = v
    return out


def _disc_port(v):
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 65536 else None


def _disc_bool(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _disc_base(v):
    """URL-Basis «/radarr» (ohne Schrägstrich am Ende, nur harmlose Zeichen), sonst leer."""
    v = str(v or "").strip().strip("/")
    if not v or len(v) > 100 or not re.fullmatch(r"[A-Za-z0-9._~\-/]+", v):
        return ""
    return "/" + v


def _disc_key(v):
    v = str(v or "").strip()
    return v if DISCOVER_KEY_RX.fullmatch(v) else None


class Discover:
    """Sucht nur lesend unter appdata nach bekannten Diensten (Ordnernamen beliebig), max. 3 Ebenen tief, ohne Symlinks,
    Dateien bis 1 MB. Liefert pro Dienst Ordnername (ohne Pfad), Port, SSL, URL-Basis und API-Key (Plex und Jellyfin nie).
    Schreibt nichts ins Log, auch nicht im Fehlerfall."""

    def __init__(self, roots=None, limit=10, window=60, budget=None, cache_s=None, clock=None):
        self.explicit = roots
        self.limiter = DiscoverLimiter(limit, window)
        self.budget = DISCOVER_BUDGET_S if budget is None else budget
        self.cache_s = DISCOVER_CACHE_S if cache_s is None else cache_s
        self.clock = clock or time.monotonic
        self._cache, self._cache_lock = None, threading.Lock()

    def roots(self):
        if self.explicit is not None:
            return [r for r in self.explicit if os.path.isdir(r)]
        if DISCOVER_ROOTS:
            return [r for r in DISCOVER_ROOTS if os.path.isdir(r)]
        main = os.path.join(FILES_ROOT, "appdata")
        if os.path.isdir(main):
            return [main]
        return [p for p in (os.path.join(FILES_ROOT, "apps"), os.path.join(FILES_ROOT, "docker")) if os.path.isdir(p)]

    def _walk(self, folder, deadline=None):
        """Breitensuche bis DISCOVER_DEPTH Ebenen: ({dateiname klein: [Pfade, flachste zuerst]}, abgebrochen?, Ordner nicht
        lesbar?). «abgebrochen» = Eintragsgrenze oder Zeitbudget erreicht. Datenbank-Ordner werden nicht durchsucht."""
        found, queue, seen, denied = {}, [(folder, 0)], 0, False
        while queue:
            if deadline is not None and self.clock() > deadline:
                return found, True, denied
            d, lvl = queue.pop(0)
            entries = []
            try:
                with os.scandir(d) as it:      # nicht ganz einlesen: riesige Ordner brechen bei der Grenze ab
                    for e in it:
                        seen += 1
                        if seen > DISCOVER_MAX_ENTRIES:
                            return found, True, denied
                        entries.append(e)
            except PermissionError:
                denied = True
                continue
            except OSError:
                continue
            if any(e.name.lower() in DISCOVER_DB_MARKERS for e in entries):
                continue                        # Datenbank-Container (Postgres, MariaDB): dort liegt kein Dienst
            for e in sorted(entries, key=lambda e: e.name):
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if lvl < DISCOVER_DEPTH and not e.name.startswith(".") and e.name.lower() not in DISCOVER_SKIP:
                            queue.append((e.path, lvl + 1))
                    elif e.name.lower() in DISCOVER_FILES and e.is_file(follow_symlinks=False):
                        found.setdefault(e.name.lower(), []).append(e.path)
                except OSError:
                    continue
        return found, False, denied

    @staticmethod
    def _entry(service, folder, port, ssl=False, base="", key=None, code="ok", variant=None, http_port=None, from_config=True,
               note=None):
        if code == "ok" and not DISCOVER_KEYS:
            code, key = "keys_off", None
        if not DISCOVER_KEYS:
            key = None
        e = {"service": service, "folder": folder, "port": port, "ssl": bool(ssl), "urlBase": base,
             "apiKey": key, "code": code, "note": note or DISCOVER_NOTES[code], "portSource": "config" if from_config else "default"}
        if variant:
            e["variant"] = variant
        if http_port and http_port != port:
            e["httpPort"] = http_port
        return e

    def _arr(self, folder, path):
        name = folder.lower()
        cfg = _disc_xml(_disc_read(path), "Config")
        inst = (cfg or {}).get("InstanceName", "").lower()
        if any(x in inst or (not inst and x in name) for x in DISCOVER_OTHER_ARR):
            return None
        svc = next((s for s, _ in DISCOVER_ARR if s in inst), None) or next((s for s, _ in DISCOVER_ARR if s in name), None)
        if svc is None and cfg:
            svc = next((s for s, p in DISCOVER_ARR if _disc_port(cfg.get("Port")) == p), None)
        if svc is None:
            return None
        default = dict(DISCOVER_ARR)[svc]
        if cfg is None:
            return self._entry(svc, folder, default, code="unreadable", from_config=False)
        key = _disc_key(cfg.get("ApiKey"))
        if not key:
            return self._entry(svc, folder, _disc_port(cfg.get("Port")) or default, code="broken", from_config=False)
        http = _disc_port(cfg.get("Port")) or default
        ssl = _disc_bool(cfg.get("EnableSsl")) and _disc_port(cfg.get("SslPort")) is not None
        port = _disc_port(cfg.get("SslPort")) if ssl else http
        return self._entry(svc, folder, port, ssl, _disc_base(cfg.get("UrlBase")), key, http_port=http,
                           from_config=_disc_port(cfg.get("Port")) is not None)

    def _sab(self, folder, path):
        ini = _disc_ini(_disc_read(path))
        if ini is None:
            return self._entry("sab", folder, 8080, code="unreadable", from_config=False)
        m = ini.get("misc", {})
        key = _disc_key(m.get("api_key"))
        http = _disc_port(m.get("port")) or 8080
        ssl = _disc_bool(m.get("enable_https"))
        port = (_disc_port(m.get("https_port")) or http) if ssl else http
        if not key:
            return self._entry("sab", folder, port, ssl, code="broken", from_config=False)
        return self._entry("sab", folder, port, ssl, _disc_base(m.get("url_base")), key, http_port=http,
                           from_config=_disc_port(m.get("port")) is not None)

    def _tautulli(self, folder, path):
        text = _disc_read(path)
        ini = _disc_ini(text)
        name = folder.lower()
        by_name = "tautulli" in name
        if ini is None:
            return self._entry("tautulli", folder, 8181, code="unreadable", from_config=False) if by_name else None
        g = ini.get("general")
        # ab 1.16.1: Abschnitt [PMS] (Plex-Verbindung) oder Ordnername «tautulli»; Headphones, LazyLibrarian, Mylar & Co.
        # haben auch [General] http_root, aber nie [PMS]
        if g is None or not (by_name or "pms" in ini):
            return None
        if not by_name and any(x in name for x in DISCOVER_NOT_TAUTULLI):
            return None
        key = _disc_key(g.get("api_key"))
        port = _disc_port(g.get("http_port")) or 8181
        ssl = _disc_bool(g.get("enable_https"))
        if not key:
            return self._entry("tautulli", folder, port, ssl, code="broken", from_config=False)
        return self._entry("tautulli", folder, port, ssl, _disc_base(g.get("http_root")), key,
                           from_config=_disc_port(g.get("http_port")) is not None)

    def _seerr(self, folder, path):
        name = folder.lower()
        variant = next((v for v in ("jellyseerr", "overseerr") if v in name), "seerr")
        text = _disc_read(path)
        if text is None:
            return self._entry("seerr", folder, 5055, code="unreadable", variant=variant, from_config=False) if "seerr" in name else None
        try:
            js = json.loads(text)
        except ValueError:
            js = None
        main = js.get("main") if isinstance(js, dict) else None
        if not isinstance(main, dict) or "apiKey" not in main:
            return self._entry("seerr", folder, 5055, code="broken", variant=variant, from_config=False) if "seerr" in name else None
        key = _disc_key(main.get("apiKey"))
        return self._entry("seerr", folder, 5055, key=key, code="ok" if key else "broken", variant=variant, from_config=False)

    def _jellyfin(self, folder, found):
        name = folder.lower()
        net = None
        for p in found.get("network.xml", []):
            net = _disc_xml(_disc_read(p), "NetworkConfiguration")
            if net:
                break
        # Emby und Jellyfin vor 10.9: Port in system.xml (HttpServerPortNumber), ab 1.16.1 gelesen
        sysc = None
        for p in found.get("system.xml", []):
            sysc = _disc_xml(_disc_read(p), "ServerConfiguration")
            if sysc:
                break
        emby = "emby" in name
        if not net and not emby and "jellyfin" not in name:
            return None
        if emby:
            sysc = sysc or {}
            http = _disc_port(sysc.get("HttpServerPortNumber"))
            ssl = _disc_bool(sysc.get("EnableHttps")) and _disc_port(sysc.get("HttpsPortNumber")) is not None
            port = _disc_port(sysc.get("HttpsPortNumber")) if ssl else (http or 8096)
            return self._entry("jellyfin", folder, port, ssl, code="emby_key", variant="emby", http_port=http,
                               from_config=http is not None)
        net = net or {}
        http = _disc_port(net.get("InternalHttpPort") or net.get("HttpServerPortNumber") or (sysc or {}).get("HttpServerPortNumber"))
        ssl = _disc_bool(net.get("EnableHttps")) and _disc_port(net.get("InternalHttpsPort")) is not None
        port = _disc_port(net.get("InternalHttpsPort")) if ssl else (http or 8096)
        return self._entry("jellyfin", folder, port, ssl, _disc_base(net.get("BaseUrl")), code="jellyfin_key",
                           variant="jellyfin", http_port=http, from_config=http is not None)

    def _hinted(self, folder):
        """(Dienst, Standard-Port, Variante) nach Ordnername, oder None. Für Ordner, die sich nicht öffnen lassen."""
        name = folder.lower()
        for svc, port in DISCOVER_ARR:
            if svc in name and not any(x in name for x in DISCOVER_OTHER_ARR):
                return svc, port, None
        if any(x in name for x in DISCOVER_NAME_NOT) or any(x in name for x in DISCOVER_OTHER_ARR):
            return None
        for key, svc, port, variant in DISCOVER_NAME_HINTS:
            if key in name:
                return svc, port, variant
        return None

    def _folder(self, folder, path, deadline=None):
        """Alle erkannten Dienste eines Container-Ordners (meist einer). Zweiter Wert «partial»: ein Ordner, der nach einem
        Dienst aussieht, wurde abgebrochen (Eintragsgrenze oder Zeit), bevor eine Einstellungsdatei gefunden war."""
        found, cut, denied = self._walk(path, deadline)
        out = []
        if found.get("config.xml"):
            e = self._arr(folder, found["config.xml"][0])
            if e:
                out.append(e)
        if found.get("sabnzbd.ini"):
            out.append(self._sab(folder, found["sabnzbd.ini"][0]))
        if found.get("config.ini"):
            e = self._tautulli(folder, found["config.ini"][0])
            if e:
                out.append(e)
        if found.get("settings.json"):
            e = self._seerr(folder, found["settings.json"][0])
            if e:
                out.append(e)
        # Plex: nur ob Preferences.xml im Ordner «Plex Media Server» liegt; die Datei wird nie geöffnet (Konto-Token)
        if any(os.path.basename(os.path.dirname(p)) == "Plex Media Server" for p in found.get("preferences.xml", [])):
            out.append(self._entry("plex", folder, 32400, code="plex_token", from_config=False))
        if found.get("network.xml") or found.get("system.xml"):
            e = self._jellyfin(folder, found)
            if e:
                out.append(e)
        hint = self._hinted(folder)
        if denied and hint and not out:
            # ab 1.16.1: Ordner (oder ein Unterordner) für den Agent nicht lesbar → melden statt still weglassen
            svc, port, variant = hint
            out.append(self._entry(svc, folder, port, code="unreadable", variant=variant, from_config=False,
                                   note=DISCOVER_NOTES["unreadable_dir"]))
        partial = bool(cut and not out and hint)
        return out, partial

    def scan(self, with_keys=True):
        """with_keys=False («?keys=0»): gleiche Funde und Codes, aber alle apiKey-Werte null (für reines Zählen auf der Startseite).
        «keys» in der Antwort = ob wirklich Schlüssel mitkommen (aus bei DISCOVER_KEYS=0 oder ?keys=0). Das Ergebnis ohne
        Schlüssel wird DISCOVER_CACHE_S Sekunden wiederverwendet (ab 1.16.1). Höchstens DISCOVER_BUDGET_S Sekunden Suche,
        danach «partial: true»."""
        roots = self.roots()
        cache_key = tuple(roots)
        if not with_keys and self.cache_s > 0:
            with self._cache_lock:
                c = self._cache
                if c and c[0] == cache_key and self.clock() - c[1] < self.cache_s:
                    return json.loads(json.dumps(c[2]))
        res = self._scan(roots, with_keys)
        if not with_keys and self.cache_s > 0:
            with self._cache_lock:
                self._cache = (cache_key, self.clock(), json.loads(json.dumps(res)))
        return res

    def _scan(self, roots, with_keys):
        deadline = self.clock() + self.budget
        res = {"available": bool(roots), "keys": bool(DISCOVER_KEYS and with_keys), "services": [], "partial": False,
               "at": int(time.time()), "ports_note": DISCOVER_PORTS_NOTE}
        seen = set()
        for root in roots:
            try:
                with os.scandir(root) as it:
                    folders = sorted((e for e in it if not e.name.startswith(".") and e.name.lower() not in DISCOVER_OWN), key=lambda e: e.name)
            except OSError:
                continue
            for e in folders[:DISCOVER_MAX_FOLDERS]:
                if self.clock() > deadline:
                    res["partial"] = True           # Zeitbudget aufgebraucht: Rest nicht mehr durchsuchen
                    break
                try:
                    if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                found, partial = self._folder(e.name, e.path, deadline)
                res["partial"] = res["partial"] or partial
                for s in found:
                    k = (s["service"], s["folder"])
                    if k not in seen:
                        seen.add(k)
                        res["services"].append(s)
            if len(folders) > DISCOVER_MAX_FOLDERS:
                res["partial"] = True
            if self.clock() > deadline:
                res["partial"] = True
                break
        res["services"].sort(key=lambda s: (s["service"], s["folder"].lower()))
        if not with_keys:
            for s in res["services"]:
                s["apiKey"] = None
        return res


def run_extras(store, stop, smart=None):
    """Neustarts (jede Minute) und Plattentemperaturen (alle 5 Minuten)."""
    try:
        check_boot(store)
    except Exception as e:  # noqa: BLE001
        print("Neustart-Prüfung fehlgeschlagen:", e, flush=True)
    last_temp = 0
    while not stop.wait(60):
        try:
            check_boot(store)
            if time.time() - last_temp >= DISKTEMP_EVERY - 5:
                last_temp = time.time()
                record_disk_temps(store, smart_latest=smart.latest if smart and smart.enabled else None)
        except Exception as e:  # noqa: BLE001
            print("Zusatz-Messung fehlgeschlagen:", e, flush=True)


CACHE_SHARES = CacheShares()
APPDATA_SIZES = AppdataSizes()


# ---------------------------------------------------------------- Ab 1.16.1: Sicherheits-Kopfzeilen, Browser-Seite, Bremsen

# Für alle JSON-Antworten: nichts zwischenspeichern (Token-Daten, API-Keys), nie als HTML deuten, nie in einem Rahmen zeigen
API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
               "X-Frame-Options": "DENY", "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"}
_WEB = {"key": None, "body": b"", "csp": ""}
_WEB_LOCK = threading.Lock()
SCRIPT_RX = re.compile(r"<script>(.*?)</script>", re.S)


def web_csp(html):
    """CSP der Browser-Seite: Skripte nur mit genau dem SHA-256 der eigenen Inline-Skripte (kein 'unsafe-inline' mehr).
    Stile bleiben 'unsafe-inline' (style="…"-Attribute für Balken und Farben; alle Daten laufen durch esc())."""
    norm = html.replace("\r\n", "\n").replace("\r", "\n")       # der HTML-Parser macht aus CR/CRLF ein LF
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(m.encode("utf-8")).digest()).decode() + "'"
              for m in SCRIPT_RX.findall(norm)]
    return ("default-src 'none'; script-src " + (" ".join(hashes) or "'none'") + "; style-src 'unsafe-inline'; img-src data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def web_page(path=None):
    """web.html einmal lesen und mit passender CSP im Speicher halten; neu lesen nur, wenn sich die Datei ändert."""
    path = path or WEB_PAGE
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    with _WEB_LOCK:
        if _WEB["key"] != key:
            html = read(path, "") or ""
            _WEB.update(key=key, body=html.encode() or "Weboberfläche fehlt (web.html)".encode(), csp=web_csp(html))
        return _WEB["body"], _WEB["csp"]


class HelloLimiter:
    """Höchstens [max_calls] Anfragen an /api/hello pro Minute und Absender (die App braucht wenige pro Prüfung)."""

    def __init__(self, max_calls=30, window=60):
        self.max_calls, self.window = max_calls, window
        self.calls, self.lock = {}, threading.Lock()
        self._pruned = float("-inf")

    def allow(self, ip, now=None):
        """0 = erlaubt, sonst Sekunden bis zur nächsten freien Anfrage."""
        now = now if now is not None else time.monotonic()
        ip = rate_key(ip)           # ab 1.19.0: IPv6 je /64
        with self.lock:
            # Viele Absender: abgelaufene weg, Speicher bleibt klein. Höchstens einmal pro Sekunde aufräumen (Review 1.16.1:
            # sonst bei über 5000 frischen Einträgen bei jeder Anfrage, wie vorher bei der Token-Bremse)
            if len(self.calls) > 5000 and now - self._pruned >= 1:
                self._pruned = now
                self.calls = {k: v for k, v in self.calls.items() if v and v[-1] > now - self.window}
            lst = [t for t in self.calls.get(ip, []) if t > now - self.window]
            if len(lst) >= self.max_calls:
                self.calls[ip] = lst
                return max(1, int(lst[0] + self.window - now) + 1)
            self.calls[ip] = lst + [now]
            return 0


def make_handler(store, sampler_now, tok, index=None, array=None, lan=None, today=None, crashes=None, smart=None, backups=None,
                 limiter=None, log_dir=None, discover=None, hello_limiter=None):
    limiter = limiter or AuthLimiter()       # je Server eine eigene Token-Bremse (Tests starten viele Server)
    hello_limiter = hello_limiter or HelloLimiter()
    discover = discover or Discover()        # je Server eine eigene Bremse für /api/discover
    smart = smart or SmartMonitor(store, SmartHelper(), enabled=False)
    backups = backups or BackupScanner(explicit=[], auto_roots=[], auto=False)

    class H(BaseHTTPRequestHandler):
        # Lese-Timeout je Verbindung: halbe Anfragen (Slowloris) oder eine zu grosse Content-Length halten sonst einen
        # Arbeits-Thread für immer fest (Chaos-Test 28.10.2026)
        timeout = 10
        server_version = "NASlab-Agent"     # ab 1.16.1: keine Python-Version im Server-Kopf
        sys_version = ""

        def log_message(self, *a):
            pass

        def version_string(self):
            return self.server_version          # ohne Leerzeichen am Ende (sys_version ist leer)

        def send_response(self, code, message=None):
            self._sent_headers = set()
            super().send_response(code, message)

        def send_header(self, keyword, value):
            getattr(self, "_sent_headers", set()).add(keyword.lower())
            super().send_header(keyword, value)

        def end_headers(self):
            # Review 1.16.1: auch Antworten, die nicht über reply() laufen (send_error: 400, 414, 431, 501 für HEAD/PUT …),
            # bekommen die Sicherheits-Kopfzeilen; was die Antwort schon selbst gesetzt hat, bleibt
            sent = getattr(self, "_sent_headers", None)
            if sent is not None:
                for k, v in API_HEADERS.items():
                    if k.lower() not in sent:
                        super().send_header(k, v)
                self._sent_headers = None
            super().end_headers()

        def authorized(self, tok):
            """Token als Bytes vergleichen (Umlaute im Header würden compare_digest sonst abbrechen lassen)."""
            if not tok:
                return False                    # ohne geladenen Token nie «Bearer » mit leerem Token annehmen
            ip = self.client_address[0]
            self._limited = 0
            got = self.headers.get("Authorization", "").encode("utf-8", "replace")
            if secrets.compare_digest(got, f"Bearer {tok}".encode()):
                return True                     # ab 1.18.0 (Audit N5): der richtige Token wird nie gebremst (Proxy-/Gateway-Adresse)
            wait = limiter.blocked_for(ip)
            if wait:
                self._limited = wait            # ab 1.15.0: zu viele falsche Tokens von dieser Adresse → 429
                return False
            if got:
                limiter.fail(ip)
            return False

        def refuse(self):
            """401 bei falschem Token; 503 mit genauem Grund, wenn der Agent selbst keinen Token laden konnte (ab 1.13.0);
            429 nach zu vielen falschen Tokens (ab 1.15.0)."""
            if getattr(self, "_limited", 0):
                return self.reply(429, {"error": "Zu viele falsche Tokens, später nochmals versuchen", "retry_after": self._limited},
                                  {"Retry-After": str(self._limited)})
            if not tok:
                why = TOKEN_STATE["error"] or "Kein Token geladen (token.txt fehlt oder ist nicht lesbar)."
                code = TOKEN_STATE.get("code") or "unusable"
                # ab 1.19.0: nur der Code (keine Länge, kein Pfad, kein Fehlertext), auch für 127.0.0.1: hinter einem Reverse-Proxy
                # im Host-Modus kommt Internet-Verkehr als 127.0.0.1 an. Den vollen Text gibt es nur im Log des Containers.
                return self.reply(503, {"error": "Token nicht nutzbar, Details im Log des Containers", "token_error": code})
            return self.reply(401, {"error": "Token fehlt oder ist falsch"})

        def local_request(self):
            """Ohne Token: nur aus dem Heimnetz und nur mit IP oder lokalem Namen im Host-Header (gegen DNS-Rebinding)."""
            return from_private_net(self.client_address[0]) and local_host_header(self.headers.get("Host", ""))

        def reply(self, code, obj, headers=None):
            # «replace»: ein einzelnes Ersatzzeichen (\ud800) aus einem Bericht darf die Antwort nicht abbrechen
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in dict(API_HEADERS, **(headers or {})).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def body_too_big(self, limit=MAX_BODY):
            """Ab 1.15.0: Anfragen über 64 KiB (Content-Length) gleich mit 413 abweisen und die Verbindung schliessen."""
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                return False                    # ungültige Länge: der Endpunkt antwortet 400
            if n > limit:
                self.close_connection = True
                self.reply(413, {"error": f"Anfrage zu gross (höchstens {limit} Bytes)"})
                return True
            return False

        def do_GET(self):
            u = urlparse(self.path)
            if self.body_too_big():
                return None
            if self.headers.get("Content-Length") not in (None, "", "0") or self.headers.get("Transfer-Encoding"):
                self.close_connection = True    # GET mit Inhalt: nicht lesen, Verbindung danach schliessen
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                # Übersicht im Browser: die Seite selbst ist ohne Token, die Werte holt sie mit dem Token
                body, csp = web_page()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                # ab 1.16.1: Skripte nur mit dem Hash der eigenen Seite (kein 'unsafe-inline')
                self.send_header("Content-Security-Policy", csp)
                self.end_headers()
                return self.wfile.write(body)
            if u.path == "/api/ping":
                # Ohne Token: nur ob /config bleibt (ab 1.13.0), nichts Geheimes. Unbekannt (kein Linux) zählt als bleibend.
                # ab 1.19.0: Host-Header wie bei hello (DNS-Rebinding); die Version nur mit gültigem Token
                if not local_host_header(self.headers.get("Host", "")):
                    return self.reply(403, {"error": "nur mit IP-Adresse oder lokalem Namen"})
                out = {"ok": True, "api_level": API_LEVEL,
                       "config_persistent": CONFIG_STATE.get("persistent") is not False, "sampling_ok": sampling_ok()}
                if self.authorized(tok):
                    out["version"] = VERSION
                return self.reply(200, out)
            if u.path == "/api/health":
                # Ohne Token, für die Docker-Gesundheitsprüfung (ab 1.15.0): 503, wenn die Messschleife hängt
                if not local_host_header(self.headers.get("Host", "")):
                    return self.reply(403, {"error": "nur mit IP-Adresse oder lokalem Namen"})
                ok = sampling_ok()
                return self.reply(200 if ok else 503, {"ok": ok, "sample_age_s": sampling_age(), "interval": INTERVAL})
            if u.path == "/api/hello":
                # Ohne Token (die App prüft damit, ob sie im eigenen Heimnetz ist, bevor sie Schlüssel sendet). Antwort:
                # feste Zufalls-Kennung und HMAC über die Nonce der App; der Token selbst verlässt den Agent nie.
                # Host-Header wie bei den anderen Endpunkten ohne Token prüfen (DNS-Rebinding)
                if not local_host_header(self.headers.get("Host", "")):
                    return self.reply(403, {"error": "nur mit IP-Adresse oder lokalem Namen"})
                if not tok:
                    return self.refuse()
                wait = hello_limiter.allow(self.client_address[0])   # ab 1.16.1: gegen Massenabfragen ohne Token
                if wait:
                    return self.reply(429, {"error": "Zu viele Anfragen, später nochmals versuchen", "retry_after": wait},
                                      {"Retry-After": str(wait)})
                nonce = q.get("nonce", "")
                if not HELLO_NONCE.fullmatch(nonce):
                    return self.reply(400, {"error": "nonce: 32 Zeichen 0-9a-f"})
                return self.reply(200, {"id": agent_id(), "proof": hello_proof(tok, nonce)})   # ab 1.19.0 ohne Version (ohne Token)
            if u.path == "/api/prices/watch":
                # Ohne Token, aber nur aus dem Heimnetz: enthält nur Plattenserien und Grössen, keine Werte vom NAS
                if not self.authorized(tok) and not self.local_request():
                    return self.reply(403, {"error": "nur aus dem Heimnetz"})
                return self.reply(200, {"queries": price_watchlist()})
            if u.path == "/api/crashes" and crashes:
                # Absturzberichte lesen: ab 1.16.1 immer mit Token (Audit Lauf 2: ein Reverse-Proxy auf dem NAS selbst ohne
                # Weiterleitungs-Kopf sähe sonst wie Loopback aus). Die App sendet den Token ohnehin; von Hand:
                # curl -H "Authorization: Bearer $(cat /config/token.txt)" http://127.0.0.1:8099/api/crashes
                if not self.authorized(tok):
                    return self.refuse()            # 401 falscher/fehlender Token, 429 Bremse, 503 kein Token geladen
                try:
                    lim = max(1, min(int(q.get("limit", "50")), CRASH_KEEP))
                except ValueError:
                    return self.reply(400, {"error": "limit muss eine Zahl sein"})
                return self.reply(200, {"crashes": crashes.list(lim)})
            if not self.authorized(tok):
                return self.refuse()
            now = int(time.time())
            try:
                hours = max(1, min(int(q.get("hours", "24")), 24 * 730))
                int(q.get("days", "30")); int(q.get("limit", "200")); int(q.get("max", "200"))
            except ValueError:
                return self.reply(400, {"error": "hours, days, limit und max müssen Zahlen sein"})
            # Ab 1.15.0: Neustarts, Plattentemperaturen, Netz, SMART, Backups, RAID, Logs. Fehler als JSON, nie den Agenten mitreissen
            try:
                if u.path == "/api/boots":
                    days = max(1, min(int(q.get("days", "30")), 730))
                    return self.reply(200, {"days": days, "boots": store.boots(now - days * 86400)})
                if u.path == "/api/disktemps":
                    return self.reply(200, disktemps_reply(store, max(1, min(int(q.get("days", "7")), 400)), q.get("res")))
                if u.path == "/api/net":
                    return self.reply(200, net_reply(store, max(1, min(int(q.get("days", "1")), 400)), q.get("res")))
                if u.path == "/api/smart":
                    return self.reply(200, smart.current())
                if u.path == "/api/smart/history":
                    return self.reply(200, smart.history(max(1, min(int(q.get("days", "90")), 730))))
                if u.path == "/api/backups":
                    return self.reply(200, backups.get())
                if u.path == "/api/raid":
                    return self.reply(200, cached("raid", raid_status, ttl=30))
                if u.path == "/api/logs":
                    if not LOGS_ON:
                        return self.reply(403, {"enabled": False, "error": "Log-Auszüge sind aus (NZBLAB_LOGS=1 und /var/log nur lesend einbinden)"})
                    if not os.path.isdir(log_dir or LOG_DIR):
                        return self.reply(409, {"enabled": True, "error": f"{log_dir or LOG_DIR} fehlt: /var/log nur lesend nach /host/log einbinden"})
                    return self.reply(200, collect_logs(log_dir, max(1, min(hours, 168)), max(1, min(int(q.get("max", "200")), 1000))))
            except Exception as e:  # noqa: BLE001
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"[:150]})
            if u.path == "/api/discover":
                # Ab 1.16.0: Dienste und API-Keys aus appdata (nur mit Token). Nie zwischenspeichern, nie loggen, höchstens
                # 10 Abrufe pro Minute; Fehlertext ohne Einzelheiten (könnte sonst Dateiinhalt enthalten)
                nostore = {"Cache-Control": "no-store", "Pragma": "no-cache"}
                wait = discover.limiter.allow()
                if wait:
                    return self.reply(429, {"error": "Zu viele Abrufe, später nochmals versuchen", "retry_after": wait},
                                      dict(nostore, **{"Retry-After": str(wait)}))
                try:
                    return self.reply(200, discover.scan(with_keys=q.get("keys", "1").strip().lower() not in ("0", "false", "no", "off", "nein")), nostore)
                except Exception:  # noqa: BLE001
                    return self.reply(500, {"error": "Suche fehlgeschlagen"}, nostore)
            if u.path == "/api/info":
                return self.reply(200, {"version": VERSION, "hostname": socket.gethostname(), "interval": INTERVAL,
                                        "heat_alert": HEAT_ALERT, "push": bool(NTFY_URL), "since": store.first_t() or now,
                                        # Fehlen die Host-Ordner, sagt die App, wie man sie ergänzt
                                        "host_sys": HOST_SYS == "/host/sys", "host_proc": HOST_PROC == "/host/proc",
                                        "files": index.available() if index else False,
                                        "push": bool(ntfy_url()), "lan": lan.available() if lan else False,
                                        # ab 1.18.0: ntfy nur Kritisches (false) oder auch Info-Meldungen (NZBLAB_NTFY_ALL=1)
                                        "ntfy_all": NTFY_ALL,
                                        # ab 1.18.0: Heimnetz-Suche bewusst ein/aus (NZBLAB_LAN, Standard aus); die App zeigt «LAN-Scan aus»
                                        "lan_enabled": LAN_ON,
                                        "array": array.read() is not None if array else False,
                                        # ab 1.13.0: bleibt /config beim Neuerstellen erhalten? Sonst Warnung mit Lösung
                                        "config_persistent": CONFIG_STATE.get("persistent") is not False,
                                        "token_source": TOKEN_STATE["source"] or "unknown",
                                        # ab 1.14.0: Linux-Server (os-release eingebunden, Ordner unter /host/fs)
                                        "host_os": os.path.isfile(HOST_OS_RELEASE),
                                        "host_fs": sorted(n for n in (os.listdir(HOST_FS) if os.path.isdir(HOST_FS) else [])
                                                          if os.path.isdir(os.path.join(HOST_FS, n)))[:50],
                                        "warnings": config_warnings() + privilege_warnings(),
                                        # ab 1.15.0: Stand der Schnittstelle für die App (vergleicht mit ihrem Minimum; der Agent
                                        # aktualisiert sich nie selbst), was eingeschaltet ist, letzter Neustart
                                        "api_level": API_LEVEL, "features": agent_features(smart, backups, log_dir),
                                        "last_boot": (boot_now() or (None,))[0],
                                        "boots_7d": len(store.boots(now - 7 * 86400)),
                                        "sampling_ok": sampling_ok()})
            if u.path == "/api/array" and array:
                return self.reply(200, array.read() or {"state": "", "disks": []})
            if u.path == "/api/devices" and lan:
                return self.reply(200, {"available": lan.available(), "reason": lan.reason(), "enabled": LAN_ON,
                                        "scanning": lan.running, "devices": lan.devices() if LAN_ON else []})
            if u.path == "/api/speedtest":
                # Netzwerkkarte auch ohne Messung, damit die App sie gleich zeigt
                return self.reply(200, dict(LAST_SPEED, nic=nic_info()) if LAST_SPEED else {"nic": nic_info()})
            if u.path == "/api/now":
                return self.reply(200, sampler_now())
            # Ab 1.14.0: Linux-Server (Debian, Ubuntu …), nur lesen. Fehler als JSON, nie den Agenten mitreissen
            if u.path in ("/api/host", "/api/filesystems"):
                try:
                    return self.reply(200, cached("host", host_info) if u.path == "/api/host" else cached("fs", filesystems))
                except Exception as e:  # noqa: BLE001
                    return self.reply(500, {"error": f"{type(e).__name__}: {e}"[:150]})
            if u.path == "/api/prices":
                return self.reply(200, price_summary())
            if u.path == "/api/today":
                return self.reply(200, (today or Today(store)).collect(max(1, min(int(q.get("hours", "24")), 24 * 7))))
            if u.path == "/api/history":
                # bis 8 Tage: volle Auflösung; darüber Stundenwerte
                if hours <= 192 and q.get("res") != "hour":
                    return self.reply(200, {"res": "raw", "rows": store.raw(now - hours * 3600)})
                return self.reply(200, {"res": "hour", "rows": store.hourly(now - hours * 3600)})
            if u.path == "/api/wakes":
                return self.reply(200, {"idle_minutes": IDLE_MIN, "wakes": store.wakes(now - hours * 3600)})
            if u.path == "/api/files/status" and index:
                return self.reply(200, index.status())
            if u.path == "/api/files/cleanup" and index:
                # Riesige Zahlen (10^20) liessen SQLite mit OverflowError abbrechen: auf 0 bis 100 Jahre begrenzen
                return self.reply(200, {"folders": index.cleanup(max(0, min(int(q.get("days", "30")), 36500)))})
            if u.path == "/api/cache/shares":
                return self.reply(200, CACHE_SHARES.get())
            if u.path == "/api/appdata":
                # Grösse pro Container-Ordner unter appdata (nur lesen, im Hintergrund gemessen)
                return self.reply(200, APPDATA_SIZES.get())
            if u.path == "/api/files/tree" and index:
                return self.reply(200, index.tree(q.get("path", "")))
            if u.path == "/api/files/largest" and index:
                return self.reply(200, {"files": index.largest(q.get("path", ""), q.get("limit", "50"))})
            if u.path == "/api/files/search" and index:
                return self.reply(200, {"results": index.search(q.get("q", ""), int(q.get("limit", "200")))})
            # Ab 1.8.0: WireGuard, doppelte Dateien, Medien-Bestand. Fehler als JSON melden, nie den Agenten mitreissen
            try:
                if u.path == "/api/wireguard":
                    return self.reply(200, wireguard_status())
                if u.path == "/api/files/duplicates" and index:
                    try:
                        mb = float(q.get("min_mb", "100"))
                        lim = int(q.get("limit", "100"))
                        if not 0 <= mb <= 10_000_000:
                            raise ValueError
                    except ValueError:
                        return self.reply(400, {"error": "min_mb und limit müssen Zahlen sein"})
                    return self.reply(200, index.duplicates(mb, lim))
                if u.path == "/api/inventory" and index:
                    return self.reply(200, index.inventory())
            except CalcError as e:
                # Rechnen fehlgeschlagen: klarer Text für die App (zeigt ihn mit «Nochmals versuchen» und fragt nicht weiter nach)
                return self.reply(500, {"error": str(e)[:150], "calc_failed": True, "retry_after": int(CALC_RETRY)})
            except Exception as e:  # noqa: BLE001
                return self.reply(500, {"error": f"{type(e).__name__}: {e}"})
            return self.reply(404, {"error": "unbekannt"})

        def do_POST(self):
            u = urlparse(self.path)
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return self.reply(411, {"error": "Content-Length nötig (kein chunked)"})
            if self.body_too_big(CRASH_MAX_BYTES * 2 if u.path == "/api/crash" else MAX_BODY):
                return None
            if not self.authorized(tok):
                return self.refuse()
            if u.path == "/api/config":
                try:
                    n = int(self.headers.get("Content-Length", "0") or 0)
                    if n < 0 or self.headers.get("Transfer-Encoding"):
                        self.close_connection = True
                        return self.reply(400, {"error": "Content-Length ungültig"})
                    if n > 10000:
                        self.close_connection = True
                        return self.reply(413, {"error": "Konfiguration höchstens 10000 Bytes"})
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, UnicodeDecodeError, RecursionError):
                    # RecursionError: tief verschachteltes JSON ({"a":{"a":…}}) bricht json.loads ab
                    return self.reply(400, {"error": "kein JSON"})
                if not isinstance(body, dict):
                    return self.reply(400, {"error": "JSON-Objekt erwartet"})
                if "price_country" in body:
                    # Land für die Preise (ab 1.10.0): «CH», «DE», «US» …, leer = zurücksetzen. «price_country_if_unset»: nur
                    # setzen, wenn noch keins gewählt ist (die App schickt beim Einrichten das Land ihrer Spracheinstellung)
                    pc = body.get("price_country")
                    if not isinstance(pc, str) or not re.fullmatch(r"[A-Za-z]{2}|", pc.strip()):
                        return self.reply(400, {"error": "price_country: Ländercode aus 2 Buchstaben, z. B. CH"})
                    pc = pc.strip().upper()
                    if not (body.get("price_country_if_unset") and price_country()):
                        if pc != price_country():
                            save_settings({"price_country": pc})
                            PRICE_KICK.set()
                    if not any(k in body for k in ("ntfy_url", "price_alerts", "price_watch")):
                        return self.reply(200, {"ok": True, "country": price_country(), "source": price_source(price_country())["label"] if price_country() else ""})
                if "price_watch" in body:
                    pw = body.get("price_watch") or []
                    ids = {x["id"]: x for x in PRICE_SERIES}
                    if not isinstance(pw, list) or len(pw) > 20 or not all(isinstance(w, dict) and w.get("series") in ids and w.get("tb") in ids[w["series"]]["sizes"] for w in pw):
                        return self.reply(400, {"error": "price_watch: [{series, tb}]"})
                    save_settings({"price_watch": [{"series": w["series"], "tb": w["tb"]} for w in pw]})
                    PRICE_KICK.set()
                    if "ntfy_url" not in body and "price_alerts" not in body:
                        return self.reply(200, {"ok": True})
                if "price_alerts" in body:
                    # Gewollt: price_alerts ersetzt immer die ganze Liste. Leer ({}, [] oder null) löscht alle Preisgrenzen;
                    # so schaltet die App die letzte Grenze aus (Chaos-Test 29.10.2026 geprüft, bleibt so).
                    pa = body.get("price_alerts") or {}
                    if not isinstance(pa, dict) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < 100000 for v in pa.values()):
                        return self.reply(400, {"error": "price_alerts: {Grösse: CHF}"})
                    save_settings({"price_alerts": {str(k)[:20]: float(v) for k, v in pa.items()}})
                    if "ntfy_url" not in body:
                        return self.reply(200, {"ok": True})
                if "ntfy_url" not in body:
                    # Keine bekannte Einstellung: nichts ändern. Vorher löschte {} oder {"x": 1} die ntfy-Adresse
                    return self.reply(400, {"error": "keine bekannte Einstellung (ntfy_url, price_country, price_alerts, price_watch)"})
                raw_url = body.get("ntfy_url") or ""
                if not isinstance(raw_url, str):
                    return self.reply(400, {"error": "ntfy_url muss Text sein"})
                url = raw_url.strip()
                if url and not NTFY_URL_RE.match(url):
                    return self.reply(400, {"error": "ntfy-Adresse ungültig (https://server/thema, Thema 8–64 Zeichen)"})
                save_settings({"ntfy_url": url})
                return self.reply(200, {"ok": True, "push": bool(ntfy_url())})
            if u.path == "/api/crash" and crashes:
                try:
                    n = int(self.headers.get("Content-Length", "0") or 0)
                    if n < 0 or n > CRASH_MAX_BYTES * 2 or self.headers.get("Transfer-Encoding"):
                        self.close_connection = True
                        return self.reply(413, {"error": "Bericht zu gross"})
                    body = json.loads(self.rfile.read(n) or b"{}")
                except (ValueError, UnicodeDecodeError, RecursionError):
                    # RecursionError: tief verschachteltes JSON ({"a":{"a":…}}) bricht json.loads ab
                    return self.reply(400, {"error": "kein JSON"})
                if not isinstance(body, dict) or not body.get("trace"):
                    return self.reply(400, {"error": "trace fehlt"})
                crashes.add(body)
                return self.reply(200, {"ok": True})
            # Ab 1.16.1 (Audit Restteile): jeder Auslöser höchstens einmal gleichzeitig. Speedtest und Test-Push melden
            # 409 «läuft schon» (die App zeigt den Text); Gerätesuche und Index antworten wie bisher 202, aber started=false
            if u.path == "/api/notify/test":
                if not NOTIFY_TEST_LOCK.acquire(blocking=False):
                    return self.reply(409, {"error": "Test-Meldung läuft schon", "running": True})
                try:
                    ok = ntfy("NZBLab-Agent: Test", "So sehen Alarme vom NAS aus. Kommt diese Meldung an, ist alles eingerichtet.", tags="white_check_mark", priority="default")
                finally:
                    NOTIFY_TEST_LOCK.release()
                return self.reply(200 if ok else 409, {"ok": ok} if ok else {"error": "Keine ntfy-Adresse eingerichtet oder ntfy nicht erreichbar"})
            if u.path == "/api/speedtest":
                if not SPEED_LOCK.acquire(blocking=False):
                    return self.reply(409, {"error": "Speedtest läuft schon, gleich nochmals versuchen", "running": True})
                try:
                    return self.reply(200, speedtest())
                except OSError as e:
                    return self.reply(502, {"error": f"Speedtest fehlgeschlagen: {e}"[:200]})
                finally:
                    SPEED_LOCK.release()
            if u.path == "/api/devices/scan" and lan:
                if lan.running or lan._scan_guard.locked():
                    return self.reply(202, {"started": False, "running": True})
                if not LAN_ON:
                    return self.reply(202, {"started": False, "running": False, "enabled": False, "reason": "off"})
                threading.Thread(target=lan.scan, daemon=True).start()
                return self.reply(202, {"started": True})
            if u.path == "/api/files/refresh" and index:
                if not index.available():
                    return self.reply(409, {"error": f"{index.root} fehlt: Pfad /mnt/user nur lesen nach /host/mnt/user einbinden"})
                if index.state.get("running") or index._build_guard.locked():
                    return self.reply(202, {"started": False, "running": True})
                threading.Thread(target=index.build, daemon=True).start()
                return self.reply(202, {"started": True})
            return self.reply(404, {"error": "unbekannt"})
    return H


def main():
    # Ab 1.16.1: neue Dateien in /config nur für den Agenten lesbar (Datei-Index, Verlauf, Absturzberichte; vorher 644)
    os.umask(0o077)
    try:
        os.makedirs(CONFIG, exist_ok=True)
    except OSError as e:
        print(f"FEHLER: {CONFIG} lässt sich nicht anlegen: {e}", flush=True)
    CONFIG_STATE.update(config_mount())    # vor dem Token: bleibt /config beim Neuerstellen erhalten?
    tok = token()                          # als root anlegen und anzeigen, danach gehört er dem Benutzer (drop_privileges)
    announce_config()                      # laute Warnung mit Lösung, wenn nicht (nach dem Token, damit sie danach im Log steht)
    load_container_names()                 # braucht root (config.v2.json ist 600); einmal beim Start reicht
    smart_helper = SmartHelper()
    if SMART_ON and smart_helper.start():  # ab 1.15.0: nur smartctl bleibt root (eigener kleiner Prozess), vor dem Rechte-Abgeben
        print("SMART an: Helfer für smartctl gestartet (nur --scan und -a, nie Selbsttests).", flush=True)
    who = drop_privileges()
    if who:
        print(f"Läuft als Benutzer {who[0]}:{who[1]} (PUID/PGID). Für alle Container-Namen und appdata-Grössen: PUID=0.", flush=True)
    agent_id()                             # Kennung für /api/hello einmal beim Start anlegen (als Benutzer, nicht root)
    store = Store(f"{CONFIG}/history.db")
    sampler = Sampler()
    global EVENT_SINK
    EVENT_SINK = store.add_event
    array = ArrayWatch(alerter("array", "err", critical=True))
    watcher = Watcher(store, alerter("heat", "err", critical=True), array)
    stop = threading.Event()
    threading.Thread(target=run_sampler, args=(store, sampler, watcher, stop), daemon=True).start()
    index = FileIndex(f"{CONFIG}/files.db")
    threading.Thread(target=run_indexer, args=(index, stop), daemon=True).start()
    lan = LanScan(f"{CONFIG}/devices.db", alerter("lan", "warn"))
    threading.Thread(target=run_prices, args=(stop, alerter("prices", "info")), daemon=True).start()
    if LAN_ON:
        threading.Thread(target=run_lan, args=(lan, stop), daemon=True).start()
    else:
        print("Heimnetz-Suche aus (NZBLAB_LAN=1 schaltet sie ein: alle 30 Min ein leeres UDP-Paket an jede Adresse des /24-Netzes).", flush=True)
    smart = SmartMonitor(store, smart_helper)
    backups = BackupScanner()
    threading.Thread(target=run_extras, args=(store, stop, smart), daemon=True).start()
    if SMART_ON:
        threading.Thread(target=run_smart, args=(smart, stop), daemon=True).start()
    if NET_ON:
        threading.Thread(target=run_net, args=(store, stop), daemon=True).start()
    threading.Thread(target=run_backups, args=(backups, stop), daemon=True).start()
    print(f"Ab 1.15.0: SMART {'an' if SMART_ON else 'aus'}, Netz-Messung {'an' if NET_ON else 'aus'}, Log-Auszüge {'an' if LOGS_ON else 'aus'}, "
          f"Backup-Pfade {len(BACKUP_PATHS)}, Backup-Suche {'an' if BACKUP_AUTO else 'aus'}.", flush=True)
    latest = {"v": None, "t": 0}

    def now_values():
        # Für die Live-Anzeige höchstens alle 2 Sekunden neu messen (eigener Sampler, damit die Differenzen stimmen)
        if time.time() - latest["t"] > 2:
            latest["v"], latest["t"] = live.sample(), time.time()
        return latest["v"]
    live = Sampler()
    live.sample()
    print(f"NZBLab-Agent {VERSION} auf Port {PORT}. Token steht in {CONFIG}/token.txt (oder NZBLAB_AGENT_TOKEN).", flush=True)
    if not tok:
        print("Token nicht geladen: alle Abfragen mit Token antworten 503 mit dem Grund (siehe FEHLER oben).", flush=True)
    else:
        announce_config()                  # nochmals am Ende des Starts, damit sie im Log nicht untergeht
    print("Heute: Quellen", Today(store).sources(), flush=True)
    crashes = CrashStore(f"{CONFIG}/crashes")
    make_server(BIND, PORT, make_handler(store, now_values, tok, index, array, lan, Today(store), crashes, smart, backups)).serve_forever()


def make_server(bind, port, handler):
    """Ab 1.15.0: Adresse wählbar (NZBLAB_BIND, z. B. 127.0.0.1 hinter einem Proxy oder die LAN-Adresse); IPv6 mit «::»."""
    try:
        fam = socket.AF_INET6 if ipaddress.ip_address(bind).version == 6 else socket.AF_INET
    except ValueError:
        print(f"NZBLAB_BIND «{bind}» ist keine IP-Adresse: nehme 0.0.0.0", flush=True)
        bind, fam = "0.0.0.0", socket.AF_INET

    class Server(ThreadingHTTPServer):
        address_family = fam
        daemon_threads = True

        def __init__(self, *a, **kw):
            self._conns, self._conns_lock = {}, threading.Lock()
            super().__init__(*a, **kw)

        def process_request(self, request, client_address):
            # Ab 1.16.1: höchstens MAX_CONN_PER_IP offene Verbindungen je Absender und MAX_CONN insgesamt (Slowloris:
            # der Lese-Timeout gilt je Lesevorgang, ein tröpfelnder Absender hielte sonst beliebig viele Fäden fest)
            ip = client_address[0]
            with self._conns_lock:
                if self._conns.get(ip, 0) >= MAX_CONN_PER_IP or sum(self._conns.values()) >= MAX_CONN:
                    self.shutdown_request(request)
                    return
                self._conns[ip] = self._conns.get(ip, 0) + 1
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._release(ip)
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._release(client_address[0])

        def _release(self, ip):
            with self._conns_lock:
                n = self._conns.get(ip, 0) - 1
                if n > 0:
                    self._conns[ip] = n
                else:
                    self._conns.pop(ip, None)
    return Server((bind, port), handler)


if __name__ == "__main__":
    main()

