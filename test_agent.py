"""Tests für den NZBLab-Agenten mit nachgebautem /sys und /proc (kein echtes NAS nötig)."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import nzblab_agent as a
from unittest.mock import patch


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class FakeHost:
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.sys = f"{self.dir}/sys"
        self.proc = f"{self.dir}/proc"
        write(f"{self.sys}/class/hwmon/hwmon5/name", "coretemp")
        write(f"{self.sys}/class/hwmon/hwmon5/temp1_label", "Package id 0")
        write(f"{self.sys}/class/hwmon/hwmon5/temp1_input", "71000")
        write(f"{self.sys}/class/hwmon/hwmon5/temp2_label", "Core 0")
        write(f"{self.sys}/class/hwmon/hwmon5/temp2_input", "69000")
        write(f"{self.sys}/class/hwmon/hwmon0/name", "nvme")
        write(f"{self.sys}/class/hwmon/hwmon0/temp1_label", "Composite")
        write(f"{self.sys}/class/hwmon/hwmon0/temp1_input", "41850")
        os.makedirs(f"{self.sys}/class/drm/card0/gt/gt1", exist_ok=True)
        self.set_gpu(0)
        write(f"{self.sys}/class/drm/card0/gt/gt1/rps_act_freq_mhz", "900")
        write(f"{self.proc}/meminfo", "MemTotal:  1000 kB\nMemAvailable: 250 kB\n")
        self.set_cpu(0, 0)
        self.set_disk(0)
        write(f"{self.proc}/42/stat", "42 (Plex Media Serv) S 1 1 1 0 -1 0 0 0 0 0 100 50 0 0 20 0 1 0 0 0 0")
        write(f"{self.proc}/42/cgroup", "0::/docker/0123456789abcdef")

    def set_gpu(self, rc6_ms):
        write(f"{self.sys}/class/drm/card0/gt/gt1/rc6_residency_ms", str(rc6_ms))

    def set_cpu(self, busy, idle):
        write(f"{self.proc}/stat", f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 {busy} 0 0 {idle} 0 0 0 0 0 0\n")

    def set_disk(self, sectors):
        write(f"{self.proc}/diskstats", f"   8      16 sdb 0 0 {sectors} 0 0 0 0 0 0 0 0\n")


class AgentTest(unittest.TestCase):
    def test_messen(self):
        h = FakeHost()
        s = a.Sampler(h.sys, h.proc)
        s.sample()
        time.sleep(1.1)
        h.set_cpu(75, 25)
        h.set_gpu(300)            # 0,3 s Ruhe in ~1,1 s → Medien-Einheit ~73 % beschäftigt
        h.set_disk(2048)
        r = s.sample()
        self.assertEqual(r["cpu"], 75.0)
        self.assertEqual(r["temps"]["CPU"], 71.0)
        self.assertEqual(r["temps"]["CPU Kern 0"], 69.0)
        self.assertIn("NVMe 0", r["temps"])
        self.assertTrue(60 < r["gpu"]["Medien"] < 80, r["gpu"])
        self.assertEqual(r["gpu"]["Medien MHz"], 900)
        self.assertGreater(r["disks"]["sdb"], 500)
        self.assertEqual(r["mem"]["percent"], 75.0)

    def test_stunden_und_wecker(self):
        store = a.Store(":memory:")
        alerts = []
        w = a.Watcher(store, lambda t, x: alerts.append(t))
        t0 = 1_760_000_000 // 3600 * 3600
        for i in range(120):                       # eine Stunde, alle 30 s
            t = t0 + i * 30
            busy = 1500.0 if i == 100 else 0.0     # Platte nach 50 Minuten Ruhe angelaufen
            s = {"t": t, "cpu": 20.0, "temps": {"CPU": 97.0 if i > 80 else 60.0}, "gpu": {"Medien": 10.0},
                 "disks": {"sdb": busy}, "top": [["plex", 150.0]]}
            store.add(s)
            w.feed(s)
        store.roll_hour(t0)
        h = store.hourly(0)[0]
        self.assertEqual(h["temps"]["CPU"][0], 97.0)
        self.assertEqual(h["top"][0][0], "plex")
        wakes = store.wakes(0)
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["disk"], "sdb")
        self.assertEqual(alerts, ["NAS zu heiss"])  # genau einmal

    def test_http_mit_token(self):
        store = a.Store(":memory:")
        store.add({"t": int(time.time()), "cpu": 5.0})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(store, lambda: {"cpu": 1.0}, "geheim"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        self.assertTrue(json.load(urllib.request.urlopen(base + "/api/ping"))["ok"])
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/now")
        self.assertEqual(e.exception.code, 401)
        req = urllib.request.Request(base + "/api/history?hours=1", headers={"Authorization": "Bearer geheim"})
        self.assertEqual(len(json.load(urllib.request.urlopen(req))["rows"]), 1)
        # Übersicht im Browser: Seite ohne Token, aber mit strenger CSP; Werte bleiben hinter dem Token
        page = urllib.request.urlopen(base + "/")
        self.assertIn("text/html", page.headers["Content-Type"])
        self.assertIn("connect-src 'self'", page.headers["Content-Security-Policy"])
        html = page.read().decode()
        self.assertIn("NZBLab", html)
        self.assertNotIn("geheim", html)
        # 1.9.0: Token nur im Tab (sessionStorage), keine NAS-Werte in der Seite, Seite holt alles per Token
        self.assertIn("sessionStorage", html)
        self.assertNotIn("localStorage.setItem", html)
        self.assertNotIn(a.socket.gethostname(), html.replace("NZBLab", ""))
        self.assertIn("Authorization", html)
        self.assertIn("/api/cache/shares", html)
        page2 = urllib.request.urlopen(base + "/index.html")
        self.assertEqual(page2.read().decode(), html)
        for path in ("/api/info", "/api/now", "/api/history?hours=24", "/api/cache/shares", "/api/today"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + path, headers={"Authorization": "Bearer falsch"}))
            self.assertEqual(e.exception.code, 401, path)
        info = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/info", headers={"Authorization": "Bearer geheim"})))
        self.assertEqual(info["version"], "1.15.0")
        srv.shutdown()


class HelloTest(unittest.TestCase):
    """Heimnetz-Prüfung der App (ab 1.11.0): /api/hello ohne Token, Beweis per HMAC, Token verlässt den Agent nie."""

    # Fester Prüfwert, derselbe steht im Kotlin-Test (HomeCheckTest.hmacWieImAgent)
    VECTOR = ("test-token", "00112233445566778899aabbccddeeff",
              "4f8277a88a5775033e37ff956e881dc823e3898966b2228b11fa617827b563f5")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = patch.object(a, "CONFIG", self.dir)
        self.cfg.start()
        a.AGENT_ID["v"] = None

    def tearDown(self):
        self.cfg.stop()
        a.AGENT_ID["v"] = None

    def test_fester_pruefwert(self):
        import hashlib, hmac
        tok, nonce, proof = self.VECTOR
        want = hmac.new(tok.encode(), ("nzblab-hello:" + nonce).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(a.hello_proof(tok, nonce), want)
        self.assertEqual(a.hello_proof(tok, nonce), proof)

    def test_hello_endpunkt(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, "geheim"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            nonce = "0123456789abcdef0123456789abcdef"
            r = json.load(urllib.request.urlopen(base + "/api/hello?nonce=" + nonce))
            self.assertEqual(r["proof"], a.hello_proof("geheim", nonce))
            self.assertEqual(r["version"], "1.15.0")
            self.assertRegex(r["id"], r"^[0-9a-f]{32}$")
            self.assertNotIn("geheim", json.dumps(r))
            # Kennung bleibt fest (settings.json), auch nach einem Neustart des Agents
            a.AGENT_ID["v"] = None
            r2 = json.load(urllib.request.urlopen(base + "/api/hello?nonce=" + "f" * 32))
            self.assertEqual(r2["id"], r["id"])
            self.assertNotEqual(r2["proof"], r["proof"])
            with open(os.path.join(self.dir, "settings.json")) as f:
                self.assertEqual(json.load(f)["agent_id"], r["id"])
            # Ungültige Nonce: 400, fremder Host-Name (DNS-Rebinding): 403
            for bad in ("", "abc", "G" * 32, "A" * 32, "0" * 33, "0" * 31 + "%0a"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + "/api/hello?nonce=" + bad)
                self.assertEqual(e.exception.code, 400, bad)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + "/api/hello?nonce=" + nonce, headers={"Host": "evil.example"}))
            self.assertEqual(e.exception.code, 403)
        finally:
            srv.shutdown()


class RequestBoundsTest(unittest.TestCase):
    def test_konfiguration_lehnt_unvollstaendige_und_grosse_requests_ab(self):
        from http.client import HTTPConnection
        from unittest.mock import patch
        store = a.Store(":memory:")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(store, lambda: {}, "geheim"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with patch.object(a, "save_settings") as save:
                for length, body, status in [(10001, b"", 413), (-1, b"", 400), ("abc", b"", 400),
                                             (1, b"{", 400), (2, b"[]", 400), (1, b"\xff", 400)]:
                    conn = HTTPConnection("127.0.0.1", srv.server_port, timeout=3)
                    conn.request("POST", "/api/config", body=body,
                                 headers={"Authorization": "Bearer geheim", "Content-Length": str(length)})
                    response = conn.getresponse()
                    self.assertEqual(response.status, status, (length, response.read()))
                    response.read()
                    conn.close()
                save.assert_not_called()
        finally:
            srv.shutdown()
            srv.server_close()


class FileIndexTest(unittest.TestCase):
    def test_index_und_suche(self):
        import tempfile, nzblab_agent as a
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cfg:
            for d in ["Media/Serien/One Piece (1999)", "Media/Filme/Tenzing (2026)", "appdata/plex"]:
                os.makedirs(os.path.join(root, d))
            open(os.path.join(root, "Media/Serien/One Piece (1999)/One Piece - S01E1023.mkv"), "wb").write(b"x" * 10)
            open(os.path.join(root, "Media/Filme/Tenzing (2026)/Tenzing.mkv"), "wb").write(b"x")
            open(os.path.join(root, "appdata/plex/geheim.db"), "wb").write(b"x")
            ix = a.FileIndex(os.path.join(cfg, "files.db"), root=root, exclude={"appdata"})
            ix.build()
            st = ix.status()
            self.assertEqual(st["files"], 2)          # appdata ausgelassen
            self.assertFalse(st["running"])
            r = ix.search("one piece 1023")
            self.assertEqual([x["name"] for x in r], ["One Piece - S01E1023.mkv"])
            self.assertEqual(r[0]["path"], "Media/Serien/One Piece (1999)/One Piece - S01E1023.mkv")
            self.assertEqual(r[0]["size"], 10)
            self.assertEqual(ix.search("geheim"), [])
            self.assertTrue(ix.search("tenzing")[0]["dir"])   # Ordner zuerst

    def test_aufraeumen(self):
        import tempfile, nzblab_agent as a
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cfg:
            os.makedirs(os.path.join(root, "Media/Downloads/alt_film"))
            os.makedirs(os.path.join(root, "Media/_Papierkorb"))
            old = os.path.join(root, "Media/Downloads/alt_film/a.mkv"); open(old, "wb").write(b"x" * 100)
            os.utime(old, (time.time() - 40 * 86400, time.time() - 40 * 86400))
            open(os.path.join(root, "Media/Downloads/neu.mkv"), "wb").write(b"x" * 5)
            open(os.path.join(root, "Media/_Papierkorb/weg.mkv"), "wb").write(b"x" * 7)
            ix = a.FileIndex(os.path.join(cfg, "files.db"), root=root, exclude=set())
            ix.build()
            c = {x["path"]: x for x in ix.cleanup(30)}
            self.assertEqual(c["Media/Downloads"]["size"], 105)
            self.assertEqual(c["Media/Downloads"]["old_size"], 100)
            self.assertEqual(c["Media/_Papierkorb"]["files"], 1)


class ArrayNetTest(unittest.TestCase):
    def md(self, d, state="STARTED", status="DISK_OK", errors=0):
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "mdstat"), "w").write(
            f"mdState={state}\nmdResync=0\ndiskName.1=md1p1\nrdevName.1=sdc\nrdevStatus.1={status}\nrdevNumErrors.1={errors}\n"
            "diskName.0=\nrdevName.0=\nrdevStatus.0=DISK_NP_DSBL\nrdevNumErrors.0=0\n")

    def test_array_alarme(self):
        import tempfile, nzblab_agent as a
        sent = []
        with tempfile.TemporaryDirectory() as proc:
            w = a.ArrayWatch(lambda t, x: sent.append(t), proc=proc)
            self.md(proc); r = w.check()
            self.assertEqual(r["state"], "STARTED"); self.assertEqual([d["device"] for d in r["disks"]], ["sdc"]); self.assertEqual(sent, [])
            self.md(proc, errors=3); w.check()
            self.assertEqual(sent, ["Neue Plattenfehler"])
            self.md(proc, status="DISK_DSBL", errors=3); w.check()
            self.md(proc, state="STOPPED", status="DISK_DSBL", errors=3); w.check()
            self.assertEqual(sent, ["Neue Plattenfehler", "Festplatte ausgefallen", "Array nicht gestartet"])
            w.check()                                     # gleicher Zustand: keine zweite Meldung
            self.assertEqual(len(sent), 3)

    def test_netz(self):
        import tempfile, nzblab_agent as a
        with tempfile.TemporaryDirectory() as root:
            proc, sysd = os.path.join(root, "proc"), os.path.join(root, "sys")
            os.makedirs(os.path.join(proc, "1/net")); os.makedirs(sysd)
            def dev(rx, tx):
                open(os.path.join(proc, "1/net/dev"), "w").write("h1\nh2\n    lo: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n"
                    f"  eth0: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n docker0: 9 0 0 0 0 0 0 0 9 0 0 0 0 0 0 0\n")
            smp = a.Sampler(sysd, proc)
            dev(1000, 500); smp.net(100.0)
            dev(3000, 1500); n = smp.net(102.0)
            self.assertEqual(n, {"eth0": {"rx": 1000, "tx": 500}})



class TodayTest(unittest.TestCase):
    def test_plattenpreise(self):
        d = tempfile.mkdtemp()
        days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(20, -1, -1)]
        hist = [{"d": x, "chf": 1000.0 - (50 if i == 20 else 0), "per_tb": 35.0} for i, x in enumerate(days)]
        write(f"{d}/prices.json", json.dumps({"updated": int(time.time()), "queries": {
            "hdd28": {"label": "28 TB Festplatte", "tb": 28, "history": hist, "best": {"name": "Seagate Exos", "chf": 950.0, "per_tb": 33.9}},
            "hdd3": {"label": "3 TB Festplatte", "tb": 3, "history": hist[-3:], "best": {"name": "WD Blue", "chf": 120.0, "per_tb": 40.0}}}}))
        old = a.PRICES_FILE, a.CONFIG
        a.PRICES_FILE, a.CONFIG = f"{d}/prices.json", d
        try:
            s = {x["key"]: x for x in a.price_summary()["sizes"]}
            # 28 TB: heute tiefster Preis seit 90 Tagen und > 3 % unter dem Mittel → günstig
            self.assertTrue(s["hdd28"]["cheap"]); self.assertEqual(s["hdd28"]["low90"], 950.0); self.assertEqual(s["hdd28"]["high90"], 1000.0)
            # 3 TB: zu wenig Verlauf, keine Grenze → nicht günstig
            self.assertFalse(s["hdd3"]["cheap"])
            # Eigene Grenze aus der App
            a.save_settings({"price_alerts": {"hdd3": 130}})
            s = {x["key"]: x for x in a.price_summary()["sizes"]}
            self.assertTrue(s["hdd3"]["cheap"]); self.assertIn("Grenze", s["hdd3"]["reason"])
            # Beobachtete Serie aus der App: erscheint sofort (noch ohne Preis), mit fertiger Suche für den Preis-Wächter
            a.save_settings({"price_watch": [{"series": "ironwolfpro", "tb": 16}, {"series": "ironwolf", "tb": 99}]})
            w = a.price_watchlist()
            self.assertEqual([x["key"] for x in w], ["w_ironwolfpro_16"])
            self.assertEqual(w[0]["q"], "Seagate IronWolf Pro 16TB")
            s = {x["key"]: x for x in a.price_summary()["sizes"]}
            self.assertTrue(s["w_ironwolfpro_16"]["pending"]); self.assertTrue(s["w_ironwolfpro_16"]["watch"])
            # «Alle NAS-Platten» einer Grösse: eine Suche über alle Serien, Treffer passt zu irgendeiner davon
            a.save_settings({"price_watch": [{"series": "allnas", "tb": 28}]})
            w = a.price_watchlist()
            self.assertEqual([x["key"] for x in w], ["w_allnas_28"])
            self.assertEqual(w[0]["q"], "28TB HDD intern")
            self.assertTrue(any("exos" in x["must"] for x in w[0]["any"]))
            self.assertTrue(a.from_private_net("192.168.1.10")); self.assertFalse(a.from_private_net("8.8.8.8"))
            a.PRICES_FILE = f"{d}/fehlt.json"
            a.save_settings({"price_watch": []})
            self.assertFalse(a.price_summary()["available"])
        finally:
            a.PRICES_FILE, a.CONFIG = old

    def test_heute_aus_allen_quellen(self):
        import calendar, sqlite3, tempfile
        d = tempfile.mkdtemp()
        now = int(time.time())
        # Unraid-Meldung (ungelesen)
        os.makedirs(f"{d}/notify/unread"); os.makedirs(f"{d}/notify/archive")
        with open(f"{d}/notify/unread/a.notify", "w") as f:
            f.write(f'timestamp={now - 60}\nevent=Docker\nsubject="Docker-Image fast voll"\ndescription="92 % belegt"\nimportance="warning"\n')
        with open(f"{d}/notify/archive/alt.notify", "w") as f:
            f.write(f"timestamp={now - 3 * 86400}\nsubject=Alt\nimportance=normal\n")
        # SABnzbd-Verlauf
        os.makedirs(f"{d}/shares/appdata/binhex-sabnzbd/data/admin")
        db = sqlite3.connect(f"{d}/shares/appdata/binhex-sabnzbd/data/admin/history1.db")
        db.execute("CREATE TABLE history (completed INTEGER, name TEXT, category TEXT, status TEXT, bytes INTEGER, fail_message TEXT)")
        db.execute("INSERT INTO history VALUES (?,?,?,?,?,?)", (now - 120, "Film.2025.German.1080p", "movies", "Completed", 4_200_000_000, ""))
        db.execute("INSERT INTO history VALUES (?,?,?,?,?,?)", (now - 300, "Serie.S01E02.German", "tv", "Failed", 0, "Fehlende Blöcke"))
        db.commit(); db.close()
        # Docker: einer abgestürzt
        os.makedirs(f"{d}/docker/abc")
        iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 600))
        with open(f"{d}/docker/abc/config.v2.json", "w") as f:
            json.dump({"Name": "/plex", "State": {"Running": False, "ExitCode": 1, "StartedAt": iso, "FinishedAt": iso}}, f)
        # Systemprotokoll
        os.makedirs(f"{d}/log")
        stamp = time.strftime("%b %d %H:%M:%S", time.localtime(now - 30))
        with open(f"{d}/log/syslog", "w") as f:
            for _ in range(3):
                f.write(f"{stamp} nas kernel: sdb: I/O error, dev sdb, sector 1234\n")
            f.write(f"{stamp} nas emhttpd: alles gut\n")
        store = a.Store(":memory:")
        store.add_event("heat", "err", "NAS zu heiss", "CPU über 95 °C")
        t = a.Today(store, files_root=f"{d}/shares", notify_dir=f"{d}/notify", log_dir=f"{d}/log", docker_dir=f"{d}/docker")
        r = t.collect(24)
        titles = [e["title"] for e in r["items"]]
        self.assertIn("Docker-Image fast voll", titles)
        self.assertNotIn("Alt", titles)                         # älter als 24 Std.
        self.assertIn("Heruntergeladen: Film.2025.German.1080p", titles)
        self.assertIn("Download fehlgeschlagen: Serie.S01E02.German", titles)
        self.assertIn("plex abgestürzt", titles)
        self.assertIn("NAS zu heiss", titles)
        sys_items = [e for e in r["items"] if e["kind"] == "syslog"]
        self.assertEqual(len(sys_items), 1)                     # dreimal dieselbe Meldung = eine Zeile
        self.assertIn("(3×)", sys_items[0]["text"])
        self.assertEqual(r["summary"]["downloads"], 1)
        self.assertEqual(r["summary"]["failed"], 1)
        self.assertEqual(r["summary"]["unread"], 1)
        self.assertTrue(all(r["sources"].values()))
        self.assertEqual(r["items"], sorted(r["items"], key=lambda e: -e["t"]))



class NicTest(unittest.TestCase):
    def test_karte_hinter_bruecke_und_bond(self):
        import tempfile
        d = tempfile.mkdtemp()
        os.makedirs(f"{d}/proc/1/net")
        with open(f"{d}/proc/1/net/route", "w") as f:
            f.write("Iface\tDestination\tGateway\n shim-br0\t00000000\t0101A8C0\nbr0\t00000000\t0101A8C0\n".replace("\n shim", "\nshim"))
        net = f"{d}/sys/class/net"
        os.makedirs(f"{net}/br0/brif/bond0"); os.makedirs(f"{net}/bond0/bonding")
        with open(f"{net}/bond0/bonding/slaves", "w") as f:
            f.write("eth0 eth1\n")
        for i, sp, st in (("eth0", "10000", "up"), ("eth1", "-1", "down")):
            os.makedirs(f"{net}/{i}/device")
            for k, v in (("speed", sp), ("operstate", st), ("device/vendor", "0x1d6a"), ("device/device", "0x04c0")):
                with open(f"{net}/{i}/{k}", "w") as f:
                    f.write(v + "\n")
        n = a.nic_info(f"{d}/sys", f"{d}/proc")
        self.assertEqual(n["iface"], "eth0")
        self.assertEqual(n["via"], "br0")
        self.assertEqual(n["speed_mbit"], 10000)
        self.assertEqual(n["model"], "Marvell AQtion AQC113")



class StorageTreeTest(unittest.TestCase):
    """Speicher-Analyse (1.7.0): Ordnergrössen aus dem Index, direkt und aufsummiert."""

    def test_tree_and_largest(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "root")
            os.makedirs(os.path.join(root, "Media", "Filme", "A"))
            os.makedirs(os.path.join(root, "Dokumente"))
            with open(os.path.join(root, "Media", "Filme", "A", "a.mkv"), "wb") as f:
                f.write(b"x" * 5000)
            with open(os.path.join(root, "Dokumente", "x_y.txt"), "wb") as f:
                f.write(b"x" * 100)
            idx = a.FileIndex(os.path.join(d, "i.db"), root=root, exclude=set())
            self.assertFalse(idx.tree("")["ready"])
            idx.build()
            t = idx.tree("")
            self.assertEqual(t["size"], 5100)
            self.assertEqual([c["name"] for c in t["children"]], ["Media", "Dokumente"])
            self.assertEqual(idx.tree("Media/Filme")["children"][0]["size"], 5000)
            # «_» im Namen darf nicht als Platzhalter wirken
            self.assertEqual([c["name"] for c in idx.tree("Dokumente")["children"]], ["x_y.txt"])
            self.assertEqual(idx.largest("", 1)[0]["name"], "a.mkv")



class MoverUndAbstuerzeTest(unittest.TestCase):
    def test_mover_erkennen(self):
        h = FakeHost()
        smp = a.Sampler(h.sys, h.proc)
        self.assertFalse(smp.sample()["mover"])
        write(f"{h.proc}/77/stat", "77 (move) R 1 1 1 0 -1 0 0 0 0 0 10 5 0 0 20 0 1 0 0 0 0")
        write(f"{h.proc}/77/cmdline", "/usr/libexec/unraid/move\0-e\0")
        self.assertTrue(smp.sample()["mover"])
        # «mover status» (nur Abfrage) zählt nicht
        write(f"{h.proc}/77/stat", "77 (mover) S 1 1 1 0 -1 0 0 0 0 0 10 5 0 0 20 0 1 0 0 0 0")
        write(f"{h.proc}/77/cmdline", "/bin/bash\0/usr/local/sbin/mover\0status\0")
        self.assertFalse(smp.sample()["mover"])

    def test_absturzberichte(self):
        crashes = a.CrashStore(tempfile.mkdtemp())
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, "geheim", crashes=crashes))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        body = json.dumps({"version": "2.18.14", "screen": "dashboard", "kind": "crash", "trace": "java.lang.IllegalStateException"}).encode()
        # Ohne Token: abgelehnt
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/crash", data=body, method="POST"))
        self.assertEqual(e.exception.code, 401)
        req = urllib.request.Request(base + "/api/crash", data=body, method="POST", headers={"Authorization": "Bearer geheim", "Content-Type": "application/json"})
        self.assertTrue(json.load(urllib.request.urlopen(req))["ok"])
        # Lesen aus dem Heimnetz (127.0.0.1) ohne Token
        got = json.load(urllib.request.urlopen(base + "/api/crashes"))["crashes"]
        self.assertEqual(got[0]["version"], "2.18.14")
        self.assertIn("IllegalStateException", got[0]["trace"])
        # DNS-Rebinding: fremder Name im Host-Header ohne Token → abgelehnt, mit Token erlaubt
        for path in ("/api/crashes", "/api/prices/watch"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + path, headers={"Host": "evil.example:8099"}))
            self.assertEqual(e.exception.code, 403)
        ok = urllib.request.Request(base + "/api/crashes", headers={"Host": "evil.example", "Authorization": "Bearer geheim"})
        self.assertEqual(json.load(urllib.request.urlopen(ok))["crashes"][0]["version"], "2.18.14")
        srv.shutdown()

    def test_host_header_gegen_dns_rebinding(self):
        for h in ("127.0.0.1:8099", "192.168.1.10", "[::1]:8099", "localhost:8099", "nas", "tower:8099", "nas.local",
                  "nas.lan:8099", "nas.home.arpa", "box.internal"):
            self.assertTrue(a.local_host_header(h), h)
        for h in ("", "evil.example", "evil.example:8099", "192.168.1.10.evil.example", "nas.local.evil.example",
                  "a b", "x/y", "[::1"):
            self.assertFalse(a.local_host_header(h), h)

    def test_externe_preisliste_wird_in_freigabe_gefunden(self):
        d = tempfile.mkdtemp()
        os.makedirs(f"{d}/Medien"); os.makedirs(f"{d}/daten/nzblab-prices")
        write(f"{d}/daten/nzblab-prices/prices.json", "{}")
        old = (a.PRICES_FILE, a.FILES_ROOT, dict(a._PRICES_FOUND))
        try:
            a.PRICES_FILE, a.FILES_ROOT = "", d
            a._PRICES_FOUND.update(path="", checked=None)
            self.assertEqual(a.prices_path(now=1000), f"{d}/daten/nzblab-prices/prices.json")
            a.PRICES_FILE = "/x/prices.json"
            self.assertEqual(a.prices_path(), "/x/prices.json")
            # nichts da: Rückfall auf <FILES_ROOT>/nzblab-prices/prices.json, neue Suche erst nach 6 Stunden
            a.PRICES_FILE, a.FILES_ROOT = "", tempfile.mkdtemp()
            a._PRICES_FOUND.update(path="", checked=None)
            self.assertEqual(a.prices_path(now=1000), f"{a.FILES_ROOT}/nzblab-prices/prices.json")
            os.makedirs(f"{a.FILES_ROOT}/s/nzblab-prices"); write(f"{a.FILES_ROOT}/s/nzblab-prices/prices.json", "{}")
            self.assertEqual(a.prices_path(now=2000), f"{a.FILES_ROOT}/nzblab-prices/prices.json")
            self.assertEqual(a.prices_path(now=1000 + 6 * 3600), f"{a.FILES_ROOT}/s/nzblab-prices/prices.json")
        finally:
            a.PRICES_FILE, a.FILES_ROOT = old[0], old[1]
            a._PRICES_FOUND.clear(); a._PRICES_FOUND.update(old[2])


class Agent18Test(unittest.TestCase):
    """1.8.0: doppelte Dateien, Medien-Bestand, WireGuard."""

    def put(self, root, rel, data=b"x"):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_doppelte_dateien(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "root")
            same = os.urandom(3 * 1024 * 1024)            # grösser als 2 MiB: erstes und letztes MiB zählen
            self.put(root, "Media/Filme/A/a.mkv", same)
            self.put(root, "Backup/a-kopie.mkv", same)
            other = bytearray(same)
            other[-1] ^= 0xFF                             # gleiche Grösse, anderes Ende
            self.put(root, "Media/Filme/B/b.mkv", bytes(other))
            self.put(root, "klein1.txt", b"abc")
            self.put(root, "klein2.txt", b"abc")          # unter min_mb: keine Kandidaten
            ix = a.FileIndex(os.path.join(d, "i.db"), root=root, exclude=set())
            ix.build()
            first = ix.duplicates(min_mb=1, limit=10)
            self.assertEqual(first["groups"], [])          # noch keine Prüfsummen
            self.assertEqual(first["pending"], 3)
            ix.hasher.join(10)
            run1 = ix.hasher
            r = ix.duplicates(min_mb=1, limit=10)
            self.assertFalse(r["running"])
            self.assertEqual(r["pending"], 0)
            self.assertEqual(r["checked_groups"], 1)
            self.assertEqual(len(r["groups"]), 1)
            self.assertEqual(sorted(f["path"] for f in r["groups"][0]["files"]), ["Backup/a-kopie.mkv", "Media/Filme/A/a.mkv"])
            self.assertEqual(r["wasted"], len(same))
            self.assertIs(ix.hasher, run1)                 # alles im Cache: kein zweiter Prüflauf
            # Höchstens 2 MiB pro Datei gelesen: Prüfsumme hängt nicht von der Mitte ab
            mid = bytearray(same)
            mid[len(same) // 2] ^= 0xFF
            self.put(root, "x.bin", bytes(mid))
            self.assertEqual(a.partial_hash(os.path.join(root, "x.bin"), len(same)),
                             a.partial_hash(os.path.join(root, "Backup/a-kopie.mkv"), len(same)))

    def test_bestand_und_neu(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "root")
            self.put(root, "Media/Filme/Babylon (1994) [imdbid-tt0105946]/Babylon (1994).mkv", b"x" * 10)
            self.put(root, "Media/Filme/Babylon (1994) [imdbid-tt0105946]/Babylon.de.srt", b"s")
            self.put(root, "Media/Serien/Test Serie (2001)/Season 1/Test Serie - S01E01.mkv", b"x" * 5)
            self.put(root, "Media/Serien/Test Serie (2001)/Season 1/Test Serie - S01E03.mkv", b"x" * 5)
            self.put(root, "Media/Serien/One Piece (1999)/One Piece - S01E001.mkv")
            self.put(root, "Media/Serien/One Piece (1999)/One Piece - S01E003.mkv")
            ix = a.FileIndex(os.path.join(d, "i.db"), root=root, exclude=set())
            ix.build()
            inv = ix.inventory()
            self.assertEqual(inv["previous"], 0)
            self.assertEqual(len(inv["movies"]), 1)
            m = inv["movies"][0]
            self.assertEqual((m["title"], m["year"], m["imdb"], m["files"], m["size"]), ("Babylon", 1994, "tt0105946", 1, 10))
            self.assertEqual(m["folder"], "Media/Filme/Babylon (1994) [imdbid-tt0105946]")
            self.assertFalse(m["new"])                      # erster Lauf: nichts ist «neu»
            series = {s["title"]: s for s in inv["series"]}
            t = series["Test Serie"]
            self.assertEqual((t["year"], t["episodes"], t["seasons"], t["gaps"]), (2001, 2, {"1": 2}, ["S01E02"]))
            self.assertFalse(t["mixed_numbering"])
            self.assertEqual(series["One Piece"]["gaps"], ["S01E002"])   # durchlaufende Nummern
            # Zweiter Lauf mit neuem Film und neuer Folge: «neu» vergleicht die letzten zwei Läufe
            self.put(root, "Media/Filme/Neu.Film.2020.1080p/Neu.Film.2020.1080p.mkv")
            self.put(root, "Media/Serien/Test Serie (2001)/Season 1/Test Serie - S01E02.mkv")
            time.sleep(1.1)                                  # anderer Zeitstempel für «last»
            ix.build()
            inv = ix.inventory()
            self.assertGreater(inv["previous"], 0)
            self.assertEqual(inv["new"]["movies"], ["Neu Film (2020)"])
            self.assertEqual(inv["new"]["episodes"], ["Test Serie – S01E02"])
            t = {s["title"]: s for s in inv["series"]}["Test Serie"]
            self.assertEqual((t["gaps"], t["new_episodes"]), ([], 1))
            self.assertFalse({m["title"]: m for m in inv["movies"]}["Babylon"]["new"])

    def test_folgen_erkennen(self):
        self.assertEqual(a.parse_episodes("show.s1e2.mkv"), [(1, 2, False)])
        self.assertEqual(a.parse_episodes("Show 2x05 Titel.mkv"), [(2, 5, False)])
        self.assertEqual(a.parse_episodes("Show S01E01E02.mkv"), [(1, 1, False), (1, 2, False)])
        self.assertEqual(a.parse_episodes("Film 1920x1080.mkv"), [])
        self.assertEqual(a.compact([2, 3, 4, 9], lambda n: f"S03E{n:02d}"), ["S03E02-S03E04", "S03E09"])

    PRIV = "cPrIvAtEkEyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    PSK = "pReShArEdAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    PUB_IF = "SeRvErPuBlIcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    PUB1 = "HaNdYpUbLiCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    PUB2 = "MaCpUbLiCkEyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

    def test_wireguard_ohne_private_schluessel(self):
        now = 1_800_000_000
        dump = "\n".join([
            "\t".join(["wg0", self.PRIV, self.PUB_IF, "51820", "off"]),
            "\t".join(["wg0", self.PUB1, self.PSK, "203.0.113.5:40000", "10.8.0.2/32", str(now - 30), "1000", "2000", "off"]),
            "\t".join(["wg0", self.PUB2, "(none)", "(none)", "10.8.0.3/32", "0", "0", "0", "25"]),
        ]) + "\n"
        with tempfile.TemporaryDirectory() as conf:
            write(f"{conf}/wg0.conf", f"[Interface]\n#Heim\nPrivateKey={self.PRIV}\nListenPort=51820\n"
                                      f"[Peer]\n#Peer 1\nPublicKey={self.PUB1}\nPresharedKey={self.PSK}\nAllowedIPs=10.8.0.2\n"
                                      f"#Peer 2\n[Peer]\nPublicKey={self.PUB2}\nAllowedIPs=10.8.0.3\n")
            r = a.wireguard_status(run=lambda: (0, dump), conf_dir=conf, now=now)
        text = json.dumps(r)
        for secret in (self.PRIV, self.PSK, self.PUB1, self.PUB2, self.PUB_IF):
            self.assertNotIn(secret, text)
        self.assertEqual((r["available"], r["reason"]), (True, "ok"))
        wg0 = r["interfaces"][0]
        self.assertEqual((wg0["name"], wg0["listen_port"]), ("wg0", 51820))
        p1, p2 = wg0["peers"]
        self.assertEqual((p1["id"], p1["name"], p1["endpoint"], p1["rx"], p1["tx"], p1["online"]),
                         (self.PUB1[:8], "Peer 1", "203.0.113.5:40000", 1000, 2000, True))
        self.assertEqual((p2["name"], p2["endpoint"], p2["handshake"], p2["online"]), ("Peer 2", None, 0, False))
        self.assertEqual(set(p1), {"id", "name", "endpoint", "allowed_ips", "handshake", "rx", "tx", "online"})

    def test_wireguard_gruende(self):
        def missing():
            raise FileNotFoundError("wg")
        self.assertEqual(a.wireguard_status(run=missing)["reason"], "no_wg")
        self.assertEqual(a.wireguard_status(run=lambda: (1, ""))["reason"], "no_permission")
        self.assertEqual(a.wireguard_status(run=lambda: (0, ""))["reason"], "no_interfaces")

    def test_http_neue_pfade(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "root")
            self.put(root, "Media/Filme/X (2000)/x.mkv")
            ix = a.FileIndex(os.path.join(d, "i.db"), root=root, exclude=set())
            ix.build()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, "geheim", index=ix))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            def get(path):
                req = urllib.request.Request(base + path, headers={"Authorization": "Bearer geheim"})
                return json.load(urllib.request.urlopen(req))
            try:
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + "/api/inventory")
                self.assertEqual(e.exception.code, 401)
                self.assertEqual(get("/api/inventory")["movies"][0]["title"], "X")
                self.assertEqual(get("/api/files/duplicates?min_mb=100")["groups"], [])
                self.assertIn(get("/api/wireguard")["reason"], ("ok", "no_wg", "no_permission", "no_interfaces"))
                with self.assertRaises(urllib.error.HTTPError) as e:
                    get("/api/files/duplicates?min_mb=abc")
                self.assertEqual(e.exception.code, 400)
            finally:
                srv.shutdown()
                srv.server_close()



class CacheSharesTest(unittest.TestCase):
    """1.8.1: Grösse jeder Freigabe auf dem Cache."""

    def test_sizes_per_share(self):
        with tempfile.TemporaryDirectory() as d:
            for share, n in (("Dokumente", 3), ("system", 1)):
                os.makedirs(os.path.join(d, share, "sub"))
                for i in range(n):
                    with open(os.path.join(d, share, "sub", f"f{i}"), "wb") as f:
                        f.write(b"x" * 8192)
            r = a.CacheShares(roots=[d], ttl=0).get()
            self.assertTrue(r["available"])
            names = [x["name"] for x in r["shares"]]
            self.assertEqual(names[0], "Dokumente")
            self.assertEqual(r["shares"][0]["files"], 3)
            self.assertGreaterEqual(r["shares"][0]["bytes"], 3 * 8192)

    def test_missing_root(self):
        r = a.CacheShares(roots=["/nicht/da"], ttl=0).get()
        self.assertFalse(r["available"])
        self.assertEqual(r["shares"], [])

class AppdataSizesTest(unittest.TestCase):
    """1.8.2: Grösse jedes Container-Ordners unter appdata, im Hintergrund gemessen."""

    def test_sizes_per_container(self):
        with tempfile.TemporaryDirectory() as d:
            for name, n in (("plex", 4), ("sonarr", 1)):
                os.makedirs(os.path.join(d, name, "db"))
                for i in range(n):
                    with open(os.path.join(d, name, "db", f"f{i}"), "wb") as f:
                        f.write(b"x" * 8192)
            s = a.AppdataSizes(root=d, ttl=3600)
            r = s.get(wait=10)
            self.assertTrue(r["available"])
            self.assertFalse(r["measuring"])
            self.assertEqual([x["name"] for x in r["containers"]], ["plex", "sonarr"])
            self.assertEqual(r["containers"][0]["files"], 4)
            self.assertGreaterEqual(r["containers"][0]["bytes"], 4 * 8192)
            first = r["at"]
            self.assertEqual(s.get()["at"], first)          # innerhalb der Frist: kein neues Messen

    def test_missing_root(self):
        r = a.AppdataSizes(root="/nicht/da").get()
        self.assertFalse(r["available"])
        self.assertEqual(r["containers"], [])

    def test_endpoint(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "jellyfin"))
            with open(os.path.join(d, "jellyfin", "x"), "wb") as f:
                f.write(b"y" * 4096)
            old = a.APPDATA_SIZES
            a.APPDATA_SIZES = a.AppdataSizes(root=d)
            a.APPDATA_SIZES.get(wait=10)
            try:
                store = a.Store(os.path.join(d, "s.db"))
                srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(store, lambda: {}, "tok"))
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/appdata", headers={"Authorization": "Bearer tok"})
                body = json.loads(urllib.request.urlopen(req, timeout=5).read())
                srv.shutdown()
                self.assertEqual(body["containers"][0]["name"], "jellyfin")
            finally:
                a.APPDATA_SIZES = old


class PriceFetchTest(unittest.TestCase):
    """Eigene Preisabfrage (ab 1.10.0): Leser mit gespeicherten Beispielseiten (test_fixtures/), keine Netz-Anfragen."""
    FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fixtures")

    def fx(self, name):
        with open(os.path.join(self.FIX, name), encoding="utf-8") as f:
            return f.read()

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = a.CONFIG, a.PRICES_FILE, a.HOST_SYS, os.environ.pop("PRICE_COUNTRY", None)
        a.CONFIG, a.PRICES_FILE, a.HOST_SYS = self.d, f"{self.d}/kein-tool.json", f"{self.d}/sys"
        # Ein NAS mit 28-TB-HDD, 3-TB-HDD, 2-TB-NVMe und einem USB-Stick (zu klein)
        for dev, tb, rot in (("sdb", 28e12, 1), ("sdc", 3e12, 1), ("nvme0n1", 2e12, 0), ("sda", 32e9, 1), ("loop0", 5e12, 0), ("md1p1", 28e12, 1)):
            write(f"{self.d}/sys/block/{dev}/size", str(int(tb // 512)))
            write(f"{self.d}/sys/block/{dev}/queue/rotational", str(rot))

    def tearDown(self):
        a.CONFIG, a.PRICES_FILE, a.HOST_SYS, env = self.old
        if env is not None:
            os.environ["PRICE_COUNTRY"] = env

    def test_platten_aus_sys_block(self):
        g = {x["key"]: x for x in a.local_disk_groups()}
        self.assertEqual(sorted(g), ["hdd28", "hdd3", "nvme2"])
        self.assertEqual(g["hdd28"]["disks"], ["sdb"])

    def test_quelle_nach_land(self):
        self.assertEqual(a.price_source("CH")["id"], "toppreise")
        self.assertEqual(a.price_source("DE")["base"], "https://geizhals.de")
        self.assertEqual(a.price_source("AT")["base"], "https://geizhals.at")
        us = a.price_source("US"); self.assertEqual((us["id"], us["locale"], us["currency"]), ("diskprices", "us", "USD"))
        self.assertEqual(a.price_source("GB")["locale"], "uk")
        self.assertEqual(a.price_source("JP")["locale"], "us")        # unbekannt: Amazon USA, als Ersatz markiert
        self.assertTrue(a.price_source("JP")["fallback"])
        self.assertIn("toppreise.ch/produktsuche?q=28TB+HDD+intern", a.price_search_url(a.price_source("CH"), 28, "hdd"))
        self.assertIn("geizhals.at/?fs=28TB&cat=hde7s", a.price_search_url(a.price_source("AT"), 28, "hdd"))

    def test_toppreise_leser(self):
        o = a.parse_toppreise(self.fx("toppreise_search.html"), 28)
        names = [x["name"] for x in o]
        self.assertEqual(len(o), 3)                                    # 20-TB-Karte fällt weg
        self.assertIn("SEAGATE Exos M, 28 TB (ST28000NM003K)", names)
        ex = next(x for x in o if "Exos" in x["name"])
        self.assertEqual((ex["price"], ex["per_tb"]), (1019.0, 36.39))
        self.assertTrue(ex["link"].startswith("https://www.toppreise.ch/preisvergleich/"))

    def test_geizhals_leser(self):
        o = a.parse_geizhals(self.fx("geizhals_list.html"), 28, "https://geizhals.de")
        self.assertEqual([x["price"] for x in o], [479.9, 399.0, 1029.0])
        self.assertEqual(o[0]["link"], "https://geizhals.de/seagate-exos-m-x28-28tb-st28000nm000c-a3200000.html")
        self.assertEqual(o[0]["shop"], "Geizhals (34 Angebote)")

    def test_diskprices_leser(self):
        rows = a.parse_diskprices(self.fx("diskprices_us.html"))
        self.assertEqual(len(rows), 6)
        hdd = a.diskprices_pick(rows, 28, "hdd")
        self.assertEqual([x["price"] for x in hdd], [369.99])          # gebraucht, recertified, extern fallen weg
        self.assertEqual(hdd[0]["link"], "https://www.amazon.com/dp/B0D1234567")
        self.assertEqual(a.diskprices_pick(rows, 2, "nvme")[0]["name"], "Samsung 990 PRO 2TB")

    def test_geld(self):
        for s, v in (("€ 1.029,00", 1029.0), ("$1,234.56", 1234.56), ("1'019.00", 1019.0), ("£99", 99.0), ("₹1,45,999", 145999.0), ("", None)):
            self.assertEqual(a.money(s), v, s)

    def test_gesperrt_und_pruefseite(self):
        self.assertTrue(a.page_blocked(403, self.fx("toppreise_blocked.html")))
        self.assertTrue(a.page_blocked(200, self.fx("geizhals_challenge.html")))
        self.assertFalse(a.page_blocked(200, self.fx("geizhals_list.html") * 20))

    def run_country(self, country, pages):
        calls = []

        def fake(url, lang):
            if not url.endswith("/robots.txt"):
                calls.append(url)
            for key, (status, name) in pages.items():
                if key in url:
                    return status, self.fx(name) if name else ""
            return 404, ""
        a.save_settings({"price_country": country})
        a.fetch_own_prices(country, fetch=fake, pause=0)
        return calls, {x["key"]: x for x in a.price_summary()["sizes"]}

    def test_ablauf_schweiz(self):
        calls, s = self.run_country("CH", {"28TB": (200, "toppreise_search.html"), "3TB": (403, "toppreise_blocked.html"), "2TB": (200, "geizhals_list.html")})
        self.assertEqual(len(calls), 3)                                # eine Anfrage pro Grösse
        self.assertEqual(s["hdd28"]["best"]["name"], "SEAGATE IronWolf Pro, 28 TB (ST28000NT000)")   # günstigster pro TB
        self.assertEqual(s["hdd28"]["best"]["currency"], "CHF")
        self.assertEqual(len(s["hdd28"]["history"]), 1)
        # Gesperrt: keine Zahl, aber Hinweis und Link für den Browser
        self.assertIsNone(s["hdd3"]["best"]); self.assertTrue(s["hdd3"]["unreadable"])
        self.assertTrue(s["hdd3"]["error"].startswith("Preise gerade nicht lesbar"))
        self.assertIn("toppreise.ch/produktsuche?q=3TB", s["hdd3"]["browser_url"])
        # Seite ohne Toppreise-Karten (anderes Layout): ebenfalls nicht lesbar, nicht «keine Angebote»
        self.assertTrue(s["nvme2"]["unreadable"])
        p = a.price_summary()
        self.assertEqual((p["country"], p["currency"], p["source"]), ("CH", "CHF", "Toppreise.ch"))
        self.assertTrue(p["available"])

    def test_ablauf_usa_eine_anfrage(self):
        calls, s = self.run_country("US", {"diskprices.com": (200, "diskprices_us.html")})
        self.assertEqual(len(calls), 1)                                # eine Seite für alle Grössen
        self.assertEqual(s["hdd28"]["best"]["chf"], 369.99)
        self.assertEqual(s["hdd28"]["best"]["shop"], "Amazon.com")
        self.assertEqual(s["hdd3"]["best"]["price"], 64.99)
        self.assertEqual(a.price_summary()["currency"], "USD")

    def test_robots(self):
        g = a.robots_rules(self.fx("robots_geizhals.txt"))
        self.assertFalse(a.robots_allows(g, "https://geizhals.de/?fs=28TB&cat=hde7s&sort=p"))
        self.assertFalse(a.robots_allows(g, "https://geizhals.de/?cat=hde7s&sort=p"))
        self.assertTrue(a.robots_allows(g, "https://geizhals.de/?cat=hde7s"))
        self.assertFalse(a.robots_allows(a.robots_rules(self.fx("robots_geizhals.txt"), "claudebot"), "https://geizhals.de/"))
        t = a.robots_rules(self.fx("robots_toppreise.txt"))
        self.assertTrue(a.robots_allows(t, a.price_search_url(a.price_source("CH"), 28, "hdd")))
        self.assertFalse(a.robots_allows(t, "https://www.toppreise.ch/ext_de/123"))
        self.assertTrue(a.robots_allows([], "https://diskprices.com/?locale=us"))

    def test_deutschland_geizhals_verboten_dann_amazon_de(self):
        calls, s = self.run_country("DE", {"geizhals.de/robots.txt": (200, "robots_geizhals.txt"), "diskprices.com/?locale=de": (200, "diskprices_us.html")})
        self.assertEqual(calls, ["https://diskprices.com/?locale=de&condition=new"])   # Geizhals-Suche nie abgefragt
        self.assertEqual(s["hdd28"]["best"]["shop"], "Amazon.de")
        self.assertTrue(s["hdd28"]["search_url"].startswith("https://geizhals.de/"))     # Browser-Link bleibt Geizhals
        self.assertEqual(a.price_summary()["source"], "Amazon.de (diskprices.com)")

    def test_ablauf_deutschland_pruefseite(self):
        calls, s = self.run_country("DE", {"geizhals.de": (200, "geizhals_challenge.html")})
        self.assertEqual(len([c for c in calls if "geizhals" in c]), 0)   # Prüfseite statt robots.txt: vorsichtshalber nicht abfragen
        self.assertTrue(all(x["unreadable"] and x["best"] is None for x in s.values()))
        self.assertTrue(s["hdd28"]["browser_url"].startswith("https://geizhals.de/?fs=28TB"))

    def test_land_per_api(self):
        store = a.Store(f"{self.d}/h.db")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(store, lambda: {}, "geheim"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def post(body):
            r = urllib.request.Request(base + "/api/config", data=json.dumps(body).encode(), method="POST",
                                       headers={"Authorization": "Bearer geheim", "Content-Type": "application/json"})
            try:
                return 200, json.load(urllib.request.urlopen(r))
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)
        try:
            self.assertEqual(post({"price_country": "de", "price_country_if_unset": True})[1]["country"], "DE")
            self.assertEqual(post({"price_country": "US", "price_country_if_unset": True})[1]["country"], "DE")  # schon gesetzt
            self.assertEqual(post({"price_country": "US"})[1]["source"], "Amazon.com (diskprices.com)")
            self.assertEqual(post({"price_country": "USA"})[0], 400)
            self.assertNotIn("ntfy_url", a.settings())                 # Land allein ändert ntfy nicht
            p = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/prices", headers={"Authorization": "Bearer geheim"})))
            self.assertEqual((p["country"], p["currency"]), ("US", "USD"))
        finally:
            srv.shutdown()

    def test_ohne_land_nichts(self):
        p = a.price_summary()
        self.assertFalse(p["available"]); self.assertFalse(p["country_set"])
        self.assertTrue(any(c["code"] == "US" for c in p["countries"]))

    def test_land_gewaehlt_vor_erstem_lauf(self):
        a.save_settings({"price_country": "AT"})
        s = {x["key"]: x for x in a.price_summary()["sizes"]}
        self.assertTrue(s["hdd28"]["pending"]); self.assertIsNone(s["hdd28"]["best"])
        self.assertIn("geizhals.at", s["hdd28"]["search_url"])

    def test_frische_tool_datei_hat_vorrang_in_ch(self):
        write(a.PRICES_FILE, json.dumps({"updated": int(time.time()), "source": "Toppreise.ch, Geizhals, Amazon (diskprices.com)",
                                         "queries": {"hdd28": {"label": "28 TB", "tb": 28, "history": [], "best": {"name": "X", "chf": 1.0, "per_tb": 0.1}}}}))
        a.save_settings({"price_country": "CH"})
        p = a.price_summary()
        self.assertEqual(p["source"], "Toppreise.ch, Geizhals, Amazon (diskprices.com)")
        self.assertEqual([x["key"] for x in p["sizes"]], ["hdd28"])
        a.save_settings({"price_country": "US"})                      # anderes Land: eigene Abfrage
        self.assertIn("diskprices", a.price_summary()["source"])

    def test_preisdatei_ohne_queries(self):
        # Kaputte oder alte Datei ohne «queries»: kein KeyError, weder beim Abfragen noch für die App
        a.save_settings({"price_country": "US"})
        write(a.own_prices_path("US"), json.dumps({"updated": 1}))
        p = a.price_summary()
        self.assertTrue(p["available"])
        write(a.own_prices_path("US"), json.dumps({"queries": None}))
        self.assertTrue(a.price_summary()["available"])
        write(a.own_prices_path("US"), json.dumps([1, 2]))
        self.assertTrue(a.price_summary()["available"])
        a.fetch_own_prices("US", fetch=lambda url, lang: (503, ""), pause=0)
        with open(a.own_prices_path("US")) as f:
            self.assertIn("queries", json.load(f))

    def test_robots_pruefseite_eigene_meldung(self):
        cache = {}
        why = a._robots_check("https://geizhals.de/?fs=28TB", lambda url, lang: (200, self.fx("geizhals_challenge.html")), cache)
        self.assertIn("Prüfseite", why)
        self.assertNotIn("erlaubt keine", why)
        self.assertIn("nicht erreichbar", a._robots_check("https://x.example/", lambda url, lang: (503, ""), {}))
        self.assertIsNone(a._robots_check("https://y.example/a", lambda url, lang: (404, ""), {}))

    def test_preisfaden_endet_bei_stop(self):
        stop = threading.Event()
        t = threading.Thread(target=a.run_prices, args=(stop, lambda *x: None), daemon=True)
        old = a.price_country
        a.price_country = lambda: ""
        try:
            t.start()
            time.sleep(0.3)
            stop.set()
            t.join(8)
            self.assertFalse(t.is_alive())
        finally:
            a.price_country = old


def vpd89(model, serial="SERIAL-GEHEIM-123"):
    """ATA-Informationsseite (VPD 89h) wie im Kernel: IDENTIFY ab Byte 60, Wörter je zwei Bytes vertauscht."""
    ident = bytearray(512)

    def put(word, text, n):
        raw = text.ljust(n * 2).encode()
        for i in range(0, n * 2, 2):
            ident[word * 2 + i], ident[word * 2 + i + 1] = raw[i + 1], raw[i]
    put(10, serial, 10)
    put(27, model, 20)
    return bytes([0, 0x89, 0x02, 0x38]) + bytes(56) + bytes(ident)


class DiskModelPriceTest(unittest.TestCase):
    """Ab 1.12.0: Preis des eigenen Plattenmodells (aus sysfs), nicht «günstigste Platte gleicher Grösse»."""
    FIX = PriceFetchTest.FIX
    fx = PriceFetchTest.fx

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = a.CONFIG, a.PRICES_FILE, a.HOST_SYS, os.environ.pop("PRICE_COUNTRY", None)
        # wie im Container: /host/sys statt /sys
        a.CONFIG, a.PRICES_FILE, a.HOST_SYS = self.d, f"{self.d}/kein-tool.json", f"{self.d}/host/sys"
        sysd = a.HOST_SYS

        def disk(dev, size, rot, model=None, vpd=None, vendor="ATA"):
            write(f"{sysd}/block/{dev}/size", str(int(size // 512)))
            write(f"{sysd}/block/{dev}/queue/rotational", str(rot))
            if model is not None:
                write(f"{sysd}/block/{dev}/device/model", model)
                write(f"{sysd}/block/{dev}/device/vendor", vendor)
            if vpd is not None:
                with open(f"{sysd}/block/{dev}/device/vpd_pg89", "wb") as f:
                    f.write(vpd)
        # IronWolf Pro 28 TB: device/model nur 16 Zeichen, der volle Name steht in VPD 89h
        disk("sdc", 28e12, 1, "ST28000NT000-4AB", vpd89("ST28000NT000-4AB103"))
        disk("sdd", 4e12, 1, "WDC WD40EFPX-68C")                        # zwei WD Red Plus 4 TB
        disk("sde", 4e12, 1, "WDC WD40EFPX-68C")
        disk("sdf", 8e12, 1, "HX8000Q", vendor="ACME")                  # unbekanntes Modell
        disk("nvme0n1", 2e12, 0)                                         # NVMe: Modell nur unter class/nvme
        write(f"{sysd}/class/nvme/nvme0/model", "Samsung SSD 990 PRO 2TB                 ")
        self.pages = {"q=ST28000NT000": "toppreise_product_ST28000NT000.html", "q=28TB+HDD+intern": "toppreise_28tb.html",
                      "q=WD40EFPX": "toppreise_wd40efpx.html", "q=4TB+HDD+intern": "toppreise_4tb.html",
                      "q=Samsung+990+PRO+2TB": "toppreise_990pro_2tb.html", "q=2TB+NVMe+SSD+M.2": "toppreise_2tb_nvme.html",
                      "q=ACME+HX8000Q": "toppreise_no_hits.html", "q=8TB+HDD+intern": "toppreise_8tb.html",
                      "q=Seagate+IronWolf+Pro+28TB": "toppreise_ironwolfpro_28tb.html"}

    def tearDown(self):
        a.CONFIG, a.PRICES_FILE, a.HOST_SYS, env = self.old
        if env is not None:
            os.environ["PRICE_COUNTRY"] = env

    def run_ch(self, pages=None):
        calls = []

        def fake(url, lang):
            if url.endswith("/robots.txt"):
                return 404, ""
            calls.append(url)
            for key, name in (pages or self.pages).items():
                if key in url:
                    return 200, self.fx(name)
            return 404, ""
        a.save_settings({"price_country": "CH"})
        a.fetch_own_prices("CH", fetch=fake, pause=0)
        return calls, {x["key"]: x for x in a.price_summary()["sizes"]}

    def test_modelle_erkennen(self):
        g = {x["key"]: x for x in a.local_disk_groups()}
        self.assertEqual(sorted(g), ["hdd28", "hdd4", "hdd8", "nvme2"])
        self.assertEqual(g["hdd28"]["model"]["part"], "ST28000NT000")
        self.assertEqual(g["hdd28"]["label"], "Seagate IronWolf Pro 28 TB")
        self.assertEqual(g["hdd28"]["model"]["warranty_years"], 5)
        self.assertEqual(g["hdd4"]["disks"], ["sdd", "sde"])            # zwei gleiche Platten → ein Ziel
        self.assertEqual((g["hdd4"]["model"]["series"], g["hdd4"]["model"]["warranty_years"]), ("redplus", 3))
        self.assertEqual(g["nvme2"]["label"], "Samsung 990 PRO 2 TB")
        self.assertEqual(g["hdd8"]["model"]["series"], "")
        self.assertEqual(g["hdd8"]["model"]["part"], "ACME HX8000Q")     # unbekannt: Suche nach dem Namen selbst
        # Seriennummer aus VPD 89h wird nie gelesen oder weitergegeben
        self.assertNotIn("GEHEIM", json.dumps(a.local_disk_groups()))
        # gleiche Erkennung mit /sys (ohne /host)
        self.assertEqual(sorted(x["key"] for x in a.local_disk_groups(a.HOST_SYS)), sorted(g))

    def test_teilenummern(self):
        cases = {"ST4000VN006-3CW104": ("ironwolf", 3), "ST16000NE000-2RW103": ("ironwolfpro", 5), "ST20000NM007D-3DJ103": ("exos", 5),
                 "ST3000DM001-9YN166": ("barracuda", 2), "ST8000VE001-3EM101": ("skyhawkai", None), "ST4000VX016-3CV104": ("skyhawk", None),
                 "WDC WD40EFAX-68JH4N1": ("red", None), "WDC WD40EFZX-68AWUN0": ("redplus", 3), "WDC WD201KFGX-68BKJN0": ("redpro", 5),
                 "WDC WD4003FFBX-68MU3N0": ("redpro", 5), "WDC WD43PURZ-74BWPY0": ("purple", None), "WDC WUH722020BLE6L4": ("ultrastar", 5),
                 "TOSHIBA HDWG480": ("n300", 3), "TOSHIBA HDWR480": ("x300", None), "TOSHIBA HDWT140": ("s300", None),
                 "TOSHIBA MG09ACA18TE": ("toshibamg", 5), "WD Red SN700 1000GB": ("sn700", None), "CT2000P3PSSD8": ("crucialp3plus", None)}
        for raw, (series, warranty) in cases.items():
            m = a.model_info(raw, 4)
            self.assertEqual((m["series"], m["warranty_years"]), (series, warranty), raw)
        self.assertEqual(a.model_info("WDC WD30EZRX-00MMMB0", 3)["part"], "WD30EZRX")
        self.assertIsNone(a.model_info("", 4))

    def test_eigenes_modell_statt_exos(self):
        calls, s = self.run_ch()
        iw = s["hdd28"]
        self.assertEqual(iw["model"], "Seagate IronWolf Pro 28 TB")
        self.assertEqual((iw["part"], iw["series"], iw["warranty_years"]), ("ST28000NT000", "ironwolfpro", 5))
        self.assertEqual(iw["best"]["chf"], 955.0)                      # Produktseite der genauen Teilenummer
        self.assertIn("ST28000NT000", iw["best"]["name"])
        self.assertNotIn("Exos", iw["best"]["name"])
        self.assertEqual((iw["match"], iw["match_label"]), ("exact", "exakt"))
        # Die Exos ist billiger, erscheint aber nur als Alternative mit eigenem Modellnamen
        self.assertEqual(iw["alternative"]["chf"], 889.0)
        self.assertEqual(iw["alternative"]["model"], "Seagate Exos M 28 TB")
        self.assertTrue(all("Exos" not in o["name"] for o in iw["offers"]))
        self.assertEqual([h["chf"] for h in iw["history"]], [955.0])
        # Red Plus: genaue Teilenummer, nicht der SMR-Red und nicht WD40EFZX
        rp = s["hdd4"]
        self.assertEqual((rp["best"]["chf"], rp["match"], rp["disks"]), (109.0, "exact", ["sdd", "sde"]))
        self.assertEqual(rp["alternative"]["model"], "Seagate IronWolf 4 TB")
        # 990 PRO: Serie + Grösse bestimmen das Produkt, Heatsink-Version zählt nicht
        nv = s["nvme2"]
        self.assertEqual((nv["best"]["chf"], nv["match"]), (179.0, "exact"))
        self.assertEqual(nv["alternative"]["chf"], 119.0)
        # unbekanntes Modell: Suche nach dem Namen, nichts gefunden → kein Preis, aber die Alternative
        un = s["hdd8"]
        self.assertIsNone(un["best"])
        self.assertIn("nirgends", un["error"])
        self.assertEqual(un["alternative"]["chf"], 179.0)
        self.assertIn("toppreise.ch/produktsuche?q=ACME+HX8000Q", un["browser_url"])
        # Höflich: jede Suche höchstens einmal pro Lauf
        self.assertEqual(len(calls), len(set(calls)))
        self.assertNotIn("GEHEIM", json.dumps(a.price_summary()))
        with open(a.own_prices_path("CH")) as f:
            self.assertNotIn("GEHEIM", f.read())

    def test_rueckfall_auf_serie_nie_exos(self):
        pages = dict(self.pages)
        pages["q=ST28000NT000"] = "toppreise_no_hits.html"
        calls, s = self.run_ch(pages)
        iw = s["hdd28"]
        self.assertIn("https://www.toppreise.ch/produktsuche?q=Seagate+IronWolf+Pro+28TB&o=preis_pro_einheit", calls)
        self.assertEqual((iw["best"]["chf"], iw["match"], iw["match_label"]), (969.0, "series", "gleiche Serie"))
        self.assertIn("ST28000NT001", iw["best"]["name"])
        self.assertEqual(iw["alternative"]["chf"], 889.0)
        self.assertIn("q=ST28000NT000", iw["search_url"])                # Browser-Link: genaue Teilenummer

    def test_eigenes_modell_gesperrt(self):
        pages = dict(self.pages)
        del pages["q=ST28000NT000"]
        calls, s = self.run_ch(dict(pages, **{"q=ST28000NT000": "toppreise_blocked.html"}))
        iw = s["hdd28"]
        self.assertIsNone(iw["best"]); self.assertTrue(iw["unreadable"])
        self.assertTrue(iw["error"].startswith("Preise gerade nicht lesbar"))
        self.assertEqual(iw["alternative"]["chf"], 889.0)
        self.assertNotIn("https://www.toppreise.ch/produktsuche?q=Seagate+IronWolf+Pro+28TB&o=preis_pro_einheit", calls)

    def test_alte_preisdatei(self):
        days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(9, 0, -1)]
        write(a.own_prices_path("CH"), json.dumps({"updated": 1, "source": "Toppreise.ch", "queries": {
            "hdd28": {"label": "28 TB Festplatte", "tb": 28, "kind": "hdd", "disks": ["sdc"],
                      "history": [{"d": x, "chf": 900.0, "per_tb": 32.1, "name": "SEAGATE Exos M, 28 TB"} for x in days],
                      "best": {"name": "SEAGATE Exos M, 28 TB (ST28000NM003K)", "chf": 899.0, "price": 899.0, "per_tb": 32.1, "link": ""}},
            "hdd3": {"label": "3 TB Festplatte", "tb": 3, "history": [], "best": {"name": "WD Blue 3TB", "chf": 60.0}}}}))
        a.save_settings({"price_country": "CH", "price_alerts": {"hdd28": 1000}})
        s = {x["key"]: x for x in a.price_summary()["sizes"]}
        self.assertNotIn("hdd3", s)                                       # Platte steckt nicht mehr im NAS
        iw = s["hdd28"]
        self.assertIsNone(iw["best"]); self.assertTrue(iw["pending"]); self.assertFalse(iw["cheap"])
        self.assertEqual(iw["alternative"]["model"], "Seagate Exos M 28 TB")
        self.assertEqual(iw["history"], [])
        calls, s = self.run_ch()
        with open(a.own_prices_path("CH")) as f:
            e = json.load(f)["queries"]["hdd28"]
        self.assertEqual(e["mode"], "model")
        self.assertEqual(len(e["alt_history"]), 10)                      # alter Verlauf + heute
        self.assertEqual([h["chf"] for h in e["history"]], [955.0])
        iw = s["hdd28"]
        self.assertTrue(iw["cheap"]); self.assertIn("Grenze", iw["reason"])   # Grenze gilt für den eigenen Preis
        self.assertEqual(iw["best"]["chf"], 955.0)

    def test_externe_liste_mit_eigenem_modell(self):
        write(a.PRICES_FILE, json.dumps({"updated": int(time.time()), "source": "Toppreise.ch, Geizhals, Amazon (diskprices.com)", "queries": {
            "hdd28": {"label": "28 TB Festplatte", "tb": 28, "history": [{"d": "2026-10-29", "chf": 1019.0}],
                      "best": {"name": "SEAGATE Exos M, 28 TB (ST28000NM003K)", "chf": 889.0, "per_tb": 31.75, "link": "x"},
                      "shops": [{"shop": "digitec", "chf": 889.0, "link": "x"}],
                      "models_now": [{"model": "ST28000NT000", "name": "SEAGATE IronWolf Pro NAS HDD + Rescue, 28 TB (ST28000NT000)",
                                      "chf": 954.9, "per_tb": 34.1, "link": "y"}],
                      "model_history": {"ST28000NT000": [{"d": "2026-10-29", "chf": 960.0}, {"d": "2026-10-30", "chf": 954.9}]}},
            "nvme2": {"label": "2 TB NVMe-SSD", "tb": 2, "history": [], "best": {"name": "KIOXIA Exceria, 2 TB", "chf": 218.25, "per_tb": 109.1},
                      "models_now": [{"model": "Samsung 990 PRO", "name": "SAMSUNG 990 PRO, 2 TB", "chf": 346.0, "per_tb": 173.0, "link": ""}]},
            "hdd4": {"label": "4 TB Festplatte", "tb": 4, "history": [], "best": {"name": "Seagate IronWolf 4TB", "chf": 95.0, "per_tb": 23.75},
                     "models_now": [{"model": "WD40EFPX", "chf": None, "name": "nicht mehr im Handel", "link": ""}]}}}))
        a.save_settings({"price_country": "CH"})
        s = {x["key"]: x for x in a.price_summary()["sizes"]}
        iw = s["hdd28"]
        self.assertEqual((iw["best"]["chf"], iw["match"], iw["model"]), (954.9, "exact", "Seagate IronWolf Pro 28 TB"))
        self.assertEqual(iw["alternative"]["model"], "Seagate Exos M 28 TB")
        self.assertEqual(iw["shops"], [])                                  # Shops gehörten zur Exos
        self.assertEqual([h["chf"] for h in iw["history"]], [960.0, 954.9])
        self.assertEqual((s["nvme2"]["best"]["chf"], s["nvme2"]["alternative"]["chf"]), (346.0, 218.25))
        self.assertIsNone(s["hdd4"]["best"]); self.assertIn("nicht mehr im Handel", s["hdd4"]["error"])
        self.assertEqual(s["hdd4"]["alternative"]["chf"], 95.0)


class InstallTest(unittest.TestCase):
    """Installation für andere (eigenes Image): Token beim ersten Start einmal im Log, Datei nur für den Besitzer,
    Wechsel auf PUID/PGID, Synology-Ordner auslassen."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = a.CONFIG, os.environ.pop("NZBLAB_AGENT_TOKEN", None), os.environ.pop("PUID", None), os.environ.pop("PGID", None)
        a.CONFIG = self.d

    def tearDown(self):
        a.CONFIG = self.old[0]
        for k, v in zip(("NZBLAB_AGENT_TOKEN", "PUID", "PGID"), self.old[1:]):
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_token_einmal_im_log(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            t1 = a.token()
        self.assertIn(f"NZBLab-Agent Token: {t1}", out.getvalue())
        self.assertGreaterEqual(len(t1), 32)
        self.assertEqual(os.stat(f"{self.d}/token.txt").st_mode & 0o777, 0o600)
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            t2 = a.token()
        self.assertEqual(t1, t2)
        self.assertNotIn(t1, out2.getvalue())       # zweiter Start: Token nicht nochmals im Log
        os.environ["NZBLAB_AGENT_TOKEN"] = "vorgegeben"
        self.assertEqual(a.token(), "vorgegeben")

    def test_ohne_puid_bleibt_alles(self):
        self.assertIsNone(a.drop_privileges())
        os.environ["PUID"] = "0"
        self.assertIsNone(a.drop_privileges())
        os.environ["PUID"] = "abc"
        self.assertIsNone(a.drop_privileges())

    @unittest.skipUnless(hasattr(os, "fork") and os.geteuid() == 0, "nur als root prüfbar")
    def test_wechsel_auf_puid(self):
        write(f"{self.d}/token.txt", "x")
        write(f"{self.d}/crashes/1.json", "{}")
        os.environ["PUID"], os.environ["PGID"] = "65534", "65534"
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                who = a.drop_privileges()
                ok = who == (65534, 65534) and os.getuid() == 65534 and os.getgid() == 65534
                with open(f"{self.d}/neu.txt", "w") as f:       # /config bleibt schreibbar
                    f.write("ok")
                a.Store(f"{self.d}/history.db")                 # Verlauf (SQLite) lässt sich als Benutzer anlegen
                os.write(w, b"1" if ok else b"0")
            except Exception as e:
                os.write(w, repr(e).encode())
            os._exit(0)
        os.close(w)
        os.waitpid(pid, 0)
        self.assertEqual(os.read(r, 200), b"1")
        for f in ("token.txt", "crashes", "crashes/1.json", "neu.txt", "history.db"):
            self.assertEqual(os.stat(f"{self.d}/{f}").st_uid, 65534, f)

    def test_synology_ordner_ausgelassen(self):
        root = f"{self.d}/share"
        write(f"{root}/Filme/A (2020)/a.mkv", "x")
        write(f"{root}/Filme/A (2020)/@eaDir/a.mkv/SYNOVIDEO_THUMB.jpg", "x")
        write(f"{root}/#recycle/alt.mkv", "x")
        idx = a.FileIndex(f"{self.d}/files.db", root=root, exclude=set())
        idx.build()
        names = {r["name"] for r in idx.search("a", 50)} | {r["name"] for r in idx.search("alt", 50)}
        self.assertIn("a.mkv", names)
        self.assertNotIn("alt.mkv", names)
        self.assertNotIn("SYNOVIDEO_THUMB.jpg", {r["name"] for r in idx.search("SYNO", 50)})
        # Muster für Systemordner auf der obersten Ebene (Synology: @appstore, @docker …)
        self.assertTrue(a.top_excluded("@appstore", {"@*", "docker"}))
        self.assertTrue(a.top_excluded("docker", {"@*", "docker"}))
        self.assertFalse(a.top_excluded("Filme", {"@*", "docker"}))
        self.assertFalse(a.top_excluded("x@y", {"@*"}))


class Chaos28Test(unittest.TestCase):
    """Regressionen aus dem Chaos-Test vom 28.10.2026."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        root = os.path.join(self.dir, "root")
        for p in ("Downloads/a_b%c.txt", "Downloads/abxc.txt", "Media/Filme/X/x.mkv"):
            os.makedirs(os.path.dirname(os.path.join(root, p)), exist_ok=True)
            with open(os.path.join(root, p), "wb") as f:
                f.write(b"x" * 10)
        self.ix = a.FileIndex(os.path.join(self.dir, "i.db"), root=root, exclude=set())
        self.ix.build()
        self.crashes = a.CrashStore(os.path.join(self.dir, "crashes"))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, "geheim",
                                                                          index=self.ix, crashes=self.crashes))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None):
        r = urllib.request.Request(self.base + path, data=body, method=method,
                                   headers={"Authorization": "Bearer geheim", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_handler_hat_lese_timeout(self):
        import socket
        self.assertEqual(a.make_handler(None, None, "x").timeout, 10)
        # Content-Length grösser als der Körper: der Agent schliesst nach dem Timeout, statt ewig zu warten
        H = a.make_handler(a.Store(":memory:"), lambda: {}, "geheim", crashes=self.crashes)
        H.timeout = 1
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
            s.sendall(b"POST /api/crash HTTP/1.1\r\nAuthorization: Bearer geheim\r\nContent-Length: 5000\r\n\r\n{\"trace\":")
            t0 = time.time()
            self.assertEqual(s.recv(100), b"")          # Verbindung zu, keine Antwort, kein Hänger
            self.assertLess(time.time() - t0, 4)
            s.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_absturz_mit_ersatzzeichen_und_tiefer_verschachtelung(self):
        st, _ = self.call("POST", "/api/crash", '{"trace": "x\\ud800y", "version": "\\udfff"}'.encode())
        self.assertEqual(st, 200)
        st, got = self.call("GET", "/api/crashes")
        self.assertEqual(st, 200)
        self.assertTrue(got["crashes"][0]["trace"].startswith("x"))
        st, _ = self.call("POST", "/api/crash", b'{"a":' * 5000 + b"1" + b"}" * 5000)
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/config", b"[" * 3000 + b"]" * 3000)
        self.assertEqual(st, 400)
        self.assertEqual([f for f in os.listdir(self.crashes.dir) if f.endswith(".tmp")], [])
        # Schreibfehler: keine halbe .tmp-Datei zurücklassen
        with patch.object(a.json, "dump", side_effect=OSError("voll")):
            with self.assertRaises(OSError):
                self.crashes.add({"trace": "x"})
        self.assertEqual([f for f in os.listdir(self.crashes.dir) if f.endswith(".tmp")], [])

    def test_config_ohne_bekannten_schluessel_loescht_ntfy_nicht(self):
        with patch.object(a, "save_settings") as save:
            for body in (b"{}", b'{"foo": 1}', b'{"price_country_if_unset": true}'):
                st, _ = self.call("POST", "/api/config", body)
                self.assertEqual(st, 400, body)
            save.assert_not_called()
            st, _ = self.call("POST", "/api/config", b'{"ntfy_url": ""}')   # bewusst leeren geht weiter
            self.assertEqual(st, 200)
            save.assert_called_once_with({"ntfy_url": ""})
            st, _ = self.call("POST", "/api/config", b'{"price_alerts": {"8": true}}')   # true ist kein Betrag
            self.assertEqual(st, 400)

    def test_cleanup_riesige_tage_und_suche_mit_platzhaltern(self):
        for d in ("99999999999999999999999", "-99999999999999999999", "0"):
            st, got = self.call("GET", "/api/files/cleanup?days=" + d)
            self.assertEqual(st, 200, d)
            self.assertTrue(all(0 <= f["old_days"] <= 36500 for f in got["folders"]))
        self.assertEqual(self.ix.cleanup(10 ** 30)[0]["old_days"], 36500)
        names = lambda q: sorted(r["name"] for r in self.ix.search(q))
        self.assertEqual(names("%"), ["a_b%c.txt"])
        self.assertEqual(names("_"), ["a_b%c.txt"])
        self.assertEqual(names("a_b"), ["a_b%c.txt"])                      # «_» nicht als beliebiges Zeichen
        self.assertEqual(names("abxc"), ["abxc.txt"])
        self.assertEqual(names("\\"), [])


# Auszüge aus echten /proc/self/mountinfo (Unraid 7, docker.img als btrfs auf /dev/loop2)
MI_HEAD = ("133 77 0:59 /btrfs/subvolumes/a3caea23f7a5f43192b94003e4e9c53482ab3d60eb2fd9a6fbb2e525c29729aa / rw,noatime - btrfs /dev/loop2 rw,ssd,subvolid=1564\n"
           "135 133 0:74 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw\n"
           "138 133 0:22 / /host/sys ro,nosuid,nodev,noexec,relatime - sysfs sysfs rw\n"
           "145 133 0:59 /containers/5c56b599ffab222718d98790bf5b915fd34657849ae858161a7658820576843a/hostname /etc/hostname rw,noatime - btrfs /dev/loop2 rw\n")
MI_BIND = MI_HEAD + "143 133 0:58 /appdata/nzblab-agent /config rw,nosuid,nodev,noatime - fuse.shfs shfs rw,user_id=0,group_id=0\n"
MI_ANON = MI_HEAD + ("150 133 0:59 /volumes/0b6f3c2a8d1e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a/_data /config rw,noatime - "
                     "btrfs /dev/loop2 rw\n")
MI_NAMED = MI_HEAD + "150 133 0:59 /volumes/nzblab-config/_data /config rw,noatime - btrfs /dev/loop2 rw\n"
MI_TMPFS = MI_HEAD + "151 133 0:120 / /config rw,relatime - tmpfs tmpfs rw,size=65536k\n"
MI_RO = MI_HEAD + "143 133 0:58 /appdata/nzblab-agent /config ro,nosuid,nodev,noatime - fuse.shfs shfs rw\n"
MI_PARENT = MI_HEAD + "160 133 0:58 /appdata /data rw,noatime - fuse.shfs shfs rw\n"
MI_SPACE = MI_HEAD + "161 133 0:58 /appdata/mein\\040agent /mein\\040ordner rw,noatime - fuse.shfs shfs rw\n"


class ConfigPersistTest(unittest.TestCase):
    """Ab 1.13.0: Token ändert sich nie mehr von selbst; Warnung, wenn /config nicht auf dem NAS liegt."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = a.CONFIG, os.environ.pop("NZBLAB_AGENT_TOKEN", None), dict(a.CONFIG_STATE)
        a.CONFIG = self.d

    def tearDown(self):
        a.CONFIG = self.old[0]
        if self.old[1] is not None:
            os.environ["NZBLAB_AGENT_TOKEN"] = self.old[1]
        else:
            os.environ.pop("NZBLAB_AGENT_TOKEN", None)
        a.CONFIG_STATE.clear(); a.CONFIG_STATE.update(self.old[2])
        a.TOKEN_STATE.update(error="", source="")

    def mount(self, text, path="/config"):
        f = os.path.join(self.d, "mountinfo")
        write(f, text)
        return a.config_mount(path, f)

    def quiet(self, fn, *args):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = fn(*args)
        return r, out.getvalue()

    def test_mountinfo_bind_mount_bleibt(self):
        m = self.mount(MI_BIND)
        self.assertEqual((m["persistent"], m["mount"], m["reason"]), (True, "/config", "none"))
        self.assertEqual(a.config_warnings(m), [])
        # Elternordner als eigener Mount (CONFIG_DIR=/data/nzblab-agent) zählt auch, «/» nicht
        self.assertTrue(self.mount(MI_PARENT, "/data/nzblab-agent")["persistent"])
        self.assertTrue(self.mount(MI_NAMED)["persistent"])                      # benannter Volume überlebt Neuerstellen
        self.assertTrue(self.mount(MI_SPACE, "/mein ordner/x")["persistent"])    # \040 = Leerzeichen

    def test_mountinfo_nicht_bleibend(self):
        for text, reason in ((MI_HEAD, "root"), (MI_ANON, "anonymous"), (MI_TMPFS, "tmpfs"), (MI_RO, "ro")):
            m = self.mount(text)
            self.assertEqual((m["persistent"], m["reason"]), (False, reason), reason)
        # /data allein ist kein Mount von /database (nur ganze Ordnernamen)
        self.assertFalse(self.mount(MI_PARENT, "/database")["persistent"])
        # Kein Linux / nicht lesbar: unbekannt, keine Warnung
        m = a.config_mount("/config", os.path.join(self.d, "fehlt"))
        self.assertIsNone(m["persistent"])
        self.assertEqual(a.config_warnings(m), [])

    def test_warnung_mit_loesung(self):
        w = a.config_warnings(self.mount(MI_HEAD), "new")
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["code"], "config_not_persistent")
        for k in ("de", "en"):
            self.assertIn("/mnt/user/appdata/nzblab-agent", w[0][k])
            self.assertIn("nzblab-agent", w[0][k])
        self.assertIn("Lesen/Schreiben", w[0]["de"])
        self.assertIn("Read/Write", w[0]["en"])
        self.assertIn("neuen Token", w[0]["de"])
        self.assertIn("Variable NZBLAB_AGENT_TOKEN", a.config_warnings(self.mount(MI_HEAD), "env")[0]["de"])
        a.CONFIG_STATE.update(self.mount(MI_HEAD))
        shown, log = self.quiet(a.announce_config)
        self.assertTrue(shown)
        self.assertIn("ACHTUNG: /config bleibt nicht erhalten", log)
        self.assertIn("WARNING: /config is not persistent", log)
        self.assertGreater(log.count("\n"), 5)
        a.CONFIG_STATE.update(self.mount(MI_BIND))
        self.assertEqual(self.quiet(a.announce_config), (False, ""))

    def test_token_bleibt_ueber_zwei_starts(self):
        t1, log1 = self.quiet(a.token)
        self.assertIn(t1, log1)
        self.assertEqual(a.TOKEN_STATE["source"], "new")
        t2, log2 = self.quiet(a.token)
        self.assertEqual(t1, t2)
        self.assertEqual(a.TOKEN_STATE["source"], "file")
        self.assertNotIn(t1, log2)
        with open(f"{self.d}/token.txt") as f:
            self.assertEqual(f.read(), t1)

    def test_leere_oder_kaputte_datei_nie_ueberschrieben(self):
        for content in (b"", b"  \n", b"\x00\x01\xff\xfe", b"zeile1\nzeile2", b"x" * 5000):
            with open(f"{self.d}/token.txt", "wb") as f:
                f.write(content)
            t, log = self.quiet(a.token)
            self.assertEqual(t, "", content)
            self.assertIn("FEHLER: Token nicht nutzbar", log)
            self.assertIn("keinen neuen Token", a.TOKEN_STATE["error"])
            with open(f"{self.d}/token.txt", "rb") as f:
                self.assertEqual(f.read(), content)                 # unangetastet

    @unittest.skipUnless(hasattr(os, "fork") and os.geteuid() == 0, "nur als root prüfbar")
    def test_unlesbare_datei_nie_ueberschrieben(self):
        write(f"{self.d}/token.txt", "alter-token")
        os.chmod(f"{self.d}/token.txt", 0o000)
        os.chmod(self.d, 0o777)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.setgid(65534); os.setuid(65534)
                import contextlib
                import io
                with contextlib.redirect_stdout(io.StringIO()):
                    t = a.token()
                os.write(w, (repr(t) + "|" + a.TOKEN_STATE["error"]).encode())
            except Exception as e:
                os.write(w, repr(e).encode())
            os._exit(0)
        os.close(w)
        os.waitpid(pid, 0)
        res = os.read(r, 2000).decode()
        self.assertTrue(res.startswith("''|"), res)
        self.assertIn("nicht lesbar", res)
        os.chmod(f"{self.d}/token.txt", 0o600)
        with open(f"{self.d}/token.txt") as f:
            self.assertEqual(f.read(), "alter-token")

    def test_verzeichnis_statt_datei(self):
        os.makedirs(f"{self.d}/token.txt")
        t, _ = self.quiet(a.token)
        self.assertEqual(t, "")
        self.assertTrue(os.path.isdir(f"{self.d}/token.txt"))

    def test_variable_gewinnt(self):
        write(f"{self.d}/token.txt", "aus-datei")
        os.environ["NZBLAB_AGENT_TOKEN"] = "  aus-variable "
        self.assertEqual(a.token(), "aus-variable")
        self.assertEqual(a.TOKEN_STATE["source"], "env")
        # auch bei leerer Datei: Variable gilt, Datei bleibt, kein Fehler
        write(f"{self.d}/token.txt", "")
        self.assertEqual(a.token(), "aus-variable")
        self.assertEqual(a.TOKEN_STATE["error"], "")
        self.assertFalse(os.path.getsize(f"{self.d}/token.txt"))

    def test_http_ping_info_und_503(self):
        a.CONFIG_STATE.update(self.mount(MI_HEAD))
        a.TOKEN_STATE.update(source="file", error="")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, "geheim"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            ping = json.load(urllib.request.urlopen(base + "/api/ping"))
            self.assertEqual(set(ping), {"ok", "version", "config_persistent", "api_level", "sampling_ok"})   # ohne Token nur der Wahrheitswert
            self.assertIs(ping["config_persistent"], False)
            info = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/info", headers={"Authorization": "Bearer geheim"})))
            self.assertIs(info["config_persistent"], False)
            self.assertEqual(info["warnings"][0]["code"], "config_not_persistent")
            self.assertEqual(info["token_source"], "file")
            a.CONFIG_STATE.update(self.mount(MI_BIND))
            self.assertIs(json.load(urllib.request.urlopen(base + "/api/ping"))["config_persistent"], True)
            info = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/info", headers={"Authorization": "Bearer geheim"})))
            self.assertEqual(info["warnings"], [])
        finally:
            srv.shutdown()
            srv.server_close()
        # Kein Token geladen: nie «Bearer » mit leerem Token annehmen, sondern 503 mit Grund
        a.TOKEN_STATE["error"] = "/config/token.txt ist leer."
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, ""))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            self.assertTrue(json.load(urllib.request.urlopen(base + "/api/ping"))["ok"])
            for path, hdr in (("/api/info", "Bearer "), ("/api/now", ""), ("/api/hello?nonce=" + "0" * 32, "")):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(urllib.request.Request(base + path, headers={"Authorization": hdr}))
                self.assertEqual(e.exception.code, 503, path)
                self.assertIn("token.txt ist leer", json.load(e.exception)["token_error"])
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + "/api/config", data=b"{}", headers={"Authorization": "Bearer "}))
            self.assertEqual(e.exception.code, 503)
        finally:
            srv.shutdown()
            srv.server_close()


class Precompute113Test(unittest.TestCase):
    """Ab 1.13.0: Bestand und Duplikate rechnet der Agent nach dem Index-Lauf im Hintergrund; eine Anfrage wartet nie lange."""

    def put(self, root, rel, data=b"x"):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def index(self, d):
        root = os.path.join(d, "root")
        self.put(root, "Media/Filme/X (2000)/x.mkv", b"x" * 10)
        self.put(root, "Media/Serien/S (2001)/S - S01E01.mkv", b"x" * 5)
        return a.FileIndex(os.path.join(d, "i.db"), root=root, exclude=set())

    def test_nach_dem_index_fertig_ohne_neu_rechnen(self):
        with tempfile.TemporaryDirectory() as d:
            ix = self.index(d)
            ix.build()                                       # rechnet Bestand und Kandidaten gleich mit
            with patch.object(ix, "_inventory_compute", side_effect=AssertionError("Anfrage darf nicht rechnen")), \
                    patch.object(ix, "_dup_compute", side_effect=AssertionError("Anfrage darf nicht rechnen")):
                inv = ix.inventory()
                dup = ix.duplicates(min_mb=100)
            self.assertFalse(inv["building"])
            self.assertGreater(inv["computed_at"], 0)
            self.assertEqual([m["title"] for m in inv["movies"]], ["X"])
            self.assertEqual(set(inv) - {"building", "computed_at"}, {"last", "previous", "movies", "series", "new"})
            self.assertFalse(dup["building"])
            self.assertEqual(dup["groups"], [])
            for k in ("last", "running", "groups", "wasted", "checked_groups", "candidate_groups", "pending", "min_mb", "error"):
                self.assertIn(k, dup)

    def test_ohne_ergebnis_sofort_building_dann_fertig(self):
        with tempfile.TemporaryDirectory() as d:
            ix = self.index(d)
            with patch.object(ix, "precompute"):
                ix.build()                                   # Neustart-Fall: Index da, aber noch nichts gerechnet
            go = threading.Event()
            real = ix._inventory_compute

            def slow():
                go.wait(10)
                return real()
            with patch.object(a, "CALC_WAIT", 0.2), patch.object(ix, "_inventory_compute", side_effect=slow):
                t0 = time.time()
                r = ix.inventory()
                self.assertLess(time.time() - t0, 2)
                self.assertTrue(r["building"])
                self.assertEqual((r["movies"], r["series"]), ([], []))
                self.assertGreater(r["last"], 0)
                self.assertEqual(r["status"]["files"], 2)
                self.assertTrue(ix.calculating("inventory"))
                # Rechnet der Hintergrund, bleibt der Index frei: die Suche antwortet sofort
                self.assertTrue(ix.search("x (2000)"))
                ix.inventory()                               # zweite Anfrage startet keinen zweiten Rechen-Thread
                go.set()
                ix._calc["inventory"].join(5)
            r = ix.inventory()
            self.assertFalse(r["building"])
            self.assertEqual([m["title"] for m in r["movies"]], ["X"])

    def test_duplikate_building_und_kleinere_stufe(self):
        with tempfile.TemporaryDirectory() as d:
            ix = self.index(d)
            with patch.object(ix, "precompute"):
                ix.build()
            go = threading.Event()
            real = ix._dup_compute
            with patch.object(a, "CALC_WAIT", 0.1), patch.object(ix, "_dup_compute", side_effect=lambda m: (go.wait(10), real(m))[1]):
                r = ix.duplicates(min_mb=100)
                self.assertTrue(r["building"])
                self.assertTrue(r["running"])                # ältere Apps fragen dann alle 5 Sekunden nach
                self.assertEqual((r["groups"], r["min_mb"]), ([], 100))
                go.set()
                ix._calc["dup"].join(5)
            self.assertFalse(ix.duplicates(min_mb=500)["building"])     # 100-MB-Kandidaten gelten auch für 500 MB
            self.assertEqual(ix._cached("dup")["value"]["min_size"], 100 * 1024 * 1024)

    def test_rechenfehler_wird_gemeldet_und_nicht_bei_jeder_abfrage_neu_gerechnet(self):
        with tempfile.TemporaryDirectory() as d:
            ix = self.index(d)
            with patch.object(ix, "precompute"):
                ix.build()
            calls = []

            def boom(*_):
                calls.append(1)
                raise RuntimeError("kaputt")
            with patch.object(ix, "_inventory_compute", side_effect=boom), patch.object(ix, "_dup_compute", side_effect=boom):
                with self.assertRaises(a.CalcError) as e:
                    ix.inventory()
                self.assertIn("kaputt", str(e.exception))
                self.assertIn("Bestandsliste", str(e.exception))
                for _ in range(3):                            # weitere Abfragen: Fehler aus dem Speicher, kein neuer Rechen-Thread
                    with self.assertRaises(a.CalcError):
                        ix.inventory()
                self.assertEqual(len(calls), 1)
                with self.assertRaises(a.CalcError) as e2:
                    ix.duplicates(min_mb=100)
                self.assertIn("Duplikat", str(e2.exception))
                with self.assertRaises(a.CalcError):
                    ix.duplicates(min_mb=100)
                self.assertEqual(len(calls), 2)
                # Nach CALC_RETRY wird neu versucht
                with patch.object(a, "CALC_RETRY", 0.3):
                    time.sleep(0.4)
                    with self.assertRaises(a.CalcError):
                        ix.inventory()
                self.assertEqual(len(calls), 3)
            # Nächster Index-Lauf rechnet wieder (diesmal erfolgreich)
            ix.build()
            r = ix.inventory()
            self.assertFalse(r["building"])
            self.assertEqual([m["title"] for m in r["movies"]], ["X"])
            self.assertFalse(ix.duplicates(min_mb=100)["building"])

    def test_rechenfehler_als_http_500(self):
        with tempfile.TemporaryDirectory() as d:
            ix = self.index(d)
            with patch.object(ix, "precompute"):
                ix.build()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, "geheim", index=ix))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                with patch.object(ix, "_inventory_compute", side_effect=RuntimeError("Speicher voll")), \
                        patch.object(ix, "_dup_compute", side_effect=RuntimeError("Speicher voll")):
                    for path in ("/api/inventory", "/api/files/duplicates"):
                        req = urllib.request.Request(base + path, headers={"Authorization": "Bearer geheim"})
                        with self.assertRaises(urllib.error.HTTPError) as e:
                            urllib.request.urlopen(req, timeout=10)
                        self.assertEqual(e.exception.code, 500)
                        body = json.load(e.exception)
                        self.assertTrue(body["calc_failed"])
                        self.assertIn("Speicher voll", body["error"])
                        self.assertNotIn("CalcError", body["error"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_kaputte_einstellungen_werden_aufgehoben(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d, patch.object(a, "CONFIG", d):
            for broken in ('{"ntfy_url": "https://ntfy.example/x", ', "[1, 2]"):
                write(f"{d}/settings.json", broken)
                self.assertEqual(a.settings(), {})
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    a.save_settings({"price_country": "CH"})
                kept = [f for f in os.listdir(d) if f.startswith("settings.json.broken-")]
                self.assertEqual(len(kept), 1, kept)
                with open(os.path.join(d, kept[0])) as f:
                    self.assertEqual(f.read(), broken)
                self.assertIn("beschädigt", out.getvalue())
                self.assertEqual(a.settings(), {"price_country": "CH"})
                os.remove(os.path.join(d, kept[0]))
            # Gültige Datei: nichts aufheben, Werte bleiben
            a.save_settings({"agent_id": "a" * 32})
            self.assertEqual(a.settings(), {"price_country": "CH", "agent_id": "a" * 32})
            self.assertFalse([f for f in os.listdir(d) if ".broken-" in f])



class LinuxHostTest(unittest.TestCase):
    """1.14.0: Linux-Server (Debian, Ubuntu) mit nachgebautem /host/proc, /host/sys, os-release und /host/fs."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.proc, self.sys = f"{self.dir}/proc", f"{self.dir}/sys"
        self.osr = f"{self.dir}/os-release"
        self.fs = f"{self.dir}/fs"
        p, s = self.proc, self.sys
        write(self.osr, 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nVERSION_ID="13"\n'
                        'VERSION="13 (trixie)"\nID=debian\n# Kommentar\nkaputt\n')
        write(f"{p}/sys/kernel/hostname", "debianvm")
        write(f"{p}/sys/kernel/osrelease", "6.12.38+deb13-amd64")
        write(f"{p}/uptime", "93784.51 180000.00")
        write(f"{p}/loadavg", "0.52 0.41 0.30 1/321 4242")
        write(f"{p}/meminfo", "MemTotal: 8000000 kB\nMemFree: 1000000 kB\nMemAvailable: 6000000 kB\nCached: 2000000 kB\n"
                              "SwapTotal: 1000000 kB\nSwapFree: 750000 kB\n")
        write(f"{p}/cpuinfo", "".join(f"processor\t: {i}\nmodel name\t: Intel(R) Core(TM) i5-8500T CPU @ 2.10GHz\n"
                                      f"physical id\t: 0\ncore id\t\t: {i // 2}\n\n" for i in range(4)))
        # Routing und eigene Adressen: 192.168.1.50 auf eth0, Docker-Brücke wird ausgeblendet
        write(f"{p}/1/net/route", "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                                  "eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\n"
                                  "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
                                  "docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n")
        write(f"{p}/1/net/fib_trie", "Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 192.168.1.50\n        /32 host LOCAL\n"
                                     "     |-- 172.17.0.1\n        /32 host LOCAL\n     |-- 127.0.0.1\n        /32 host LOCAL\n")
        for n, mac, st, sp in (("eth0", "52:54:00:12:34:56", "up", "1000"), ("lo", "00:00:00:00:00:00", "unknown", ""),
                               ("docker0", "02:42:ac:11:00:01", "up", ""), ("veth1a2b", "aa:bb:cc:dd:ee:ff", "up", "10000")):
            write(f"{s}/class/net/{n}/address", mac)
            write(f"{s}/class/net/{n}/operstate", st)
            if sp:
                write(f"{s}/class/net/{n}/speed", sp)
        # Platten: SATA-HDD mit Temperatur (drivetemp), NVMe, loop wird ausgelassen
        write(f"{s}/block/sda/size", str(4_000_000_000_000 // 512))
        write(f"{s}/block/sda/queue/rotational", "1")
        write(f"{s}/block/sda/device/model", "WDC WD40EFPX-68C")
        write(f"{s}/block/sda/device/vendor", "ATA")
        write(f"{s}/block/sda/device/hwmon/hwmon3/temp1_input", "38000")
        write(f"{s}/block/nvme0n1/size", str(500_000_000_000 // 512))
        write(f"{s}/block/nvme0n1/queue/rotational", "0")
        write(f"{s}/block/nvme0n1/device/model", "Samsung SSD 980 500GB")
        write(f"{s}/block/loop0/size", "1000")
        write(f"{s}/class/block/sda1/size", str(3_999_000_000_000 // 512))
        write(f"{s}/class/block/nvme0n1p2/size", str(480_000_000_000 // 512))
        write(f"{s}/class/block/dm-0/size", str(100_000_000_000 // 512))
        write(f"{s}/class/block/dm-0/dm/name", "vg-home")
        os.makedirs(f"{self.fs}/data")
        self.data_dev = a._dev_of(f"{self.fs}/data")
        write(f"{p}/1/mountinfo",
              "22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
              "23 22 0:21 / /proc rw - proc proc rw\n"
              "24 22 0:22 / /sys rw - sysfs sysfs rw\n"
              "25 22 0:23 / /run rw - tmpfs tmpfs rw\n"
              f"30 22 {self.data_dev} / /srv rw,noatime shared:5 - ext4 /dev/sda1 rw\n"
              "31 22 253:0 / /home\\040daten rw - xfs /dev/mapper/vg-home rw\n"
              "32 22 259:1 / /boot/efi rw - vfat /dev/nvme0n1p1 rw\n"
              "33 22 0:50 / /var/lib/docker/overlay2/abc/merged rw - overlay overlay rw\n"
              f"34 30 {self.data_dev} /backup /mnt/backup rw - ext4 /dev/sda1 rw\n")

    def test_host_info(self):
        h = a.host_info(self.proc, self.sys, self.osr, now=100000)
        self.assertEqual(h["hostname"], "debianvm")
        self.assertEqual(h["os"]["pretty"], "Debian GNU/Linux 13 (trixie)")
        self.assertEqual(h["os"]["version"], "13 (trixie)")
        self.assertEqual(h["os"]["id"], "debian")
        self.assertEqual(h["kernel"], "6.12.38+deb13-amd64")
        self.assertEqual(h["uptime_s"], 93784)
        self.assertEqual(h["boot_time"], 100000 - 93784)
        self.assertEqual(h["cpu"], {"model": "Intel(R) Core(TM) i5-8500T CPU @ 2.10GHz", "threads": 4, "cores": 2})
        self.assertEqual(h["load"], [0.52, 0.41, 0.30])
        self.assertEqual(h["mem"]["total"], 8000000 * 1024)
        self.assertEqual(h["mem"]["used"], 2000000 * 1024)
        self.assertEqual(h["mem"]["swap_used"], 250000 * 1024)
        self.assertEqual([n["name"] for n in h["net"]], ["eth0"])
        self.assertEqual(h["net"][0]["ips"], ["192.168.1.50"])
        self.assertEqual(h["net"][0]["speed_mbit"], 1000)
        self.assertEqual(h["net"][0]["mac"], "52:54:00:12:34:56")

    def test_ohne_os_release_und_ohne_host_ordner(self):
        """Fehlt alles (kein /host-Ordner eingebunden), kommt trotzdem eine Antwort, nie das os-release des Containers."""
        h = a.host_info(f"{self.dir}/leer", f"{self.dir}/leer", f"{self.dir}/fehlt")
        self.assertFalse(h["os"]["available"])
        self.assertEqual(h["os"]["name"], "")
        self.assertIsNone(h["uptime_s"])
        self.assertEqual(h["net"], [])
        self.assertEqual(a.parse_os_release('A="x\\"y"\nb=klein\nLANG=' + "z" * 500)["A"], 'x"y')

    def test_dateisysteme(self):
        r = a.filesystems(self.proc, self.sys, self.fs, f"{self.dir}/kein-docker")
        mounts = {f["mount"]: f for f in r["filesystems"]}
        # Pseudo-Dateisysteme, /proc, /sys, /run, Docker-Overlays und /boot/efi nicht; Bind-Mount von /dev/sda1 nur einmal
        self.assertEqual(sorted(mounts), ["/", "/home daten", "/srv"])
        self.assertEqual(r["mounts_from"], "host")
        # Nur der ausdrücklich eingebundene Ordner (/host/fs/data → /srv) hat eine Belegung
        srv = mounts["/srv"]
        self.assertTrue(srv["measured"])
        self.assertEqual(srv["label"], "data")
        self.assertGreater(srv["size"], 0)
        self.assertIsNotNone(srv["free"])
        root = mounts["/"]
        self.assertFalse(root["measured"])
        self.assertIsNone(root["used"])
        self.assertEqual(root["size"], 480_000_000_000 // 512 * 512)
        self.assertEqual(mounts["/home daten"]["size"], 100_000_000_000 // 512 * 512)   # /dev/mapper → dm-0
        self.assertEqual(mounts["/home daten"]["fstype"], "xfs")
        disks = {d["dev"]: d for d in r["disks"]}
        self.assertEqual(sorted(disks), ["nvme0n1", "sda"])
        self.assertEqual(disks["sda"]["model"], "WDC WD40EFPX-68C")
        self.assertTrue(disks["sda"]["rotational"])
        self.assertEqual(disks["sda"]["temp"], 38)
        self.assertEqual(disks["nvme0n1"]["kind"], "nvme")
        self.assertIsNone(disks["nvme0n1"]["temp"])
        self.assertEqual(r["measured"], ["data"])

    def test_ohne_host_mountinfo(self):
        """Ohne /host/proc: nur die eingebundenen Ordner, nie die Einhängepunkte des Containers (/config, /host/sys …)."""
        r = a.filesystems(f"{self.dir}/leer", self.sys, self.fs, f"{self.dir}/kein-docker")
        self.assertEqual(r["mounts_from"], "container")
        self.assertEqual([f["label"] for f in r["filesystems"]], ["data"])
        self.assertTrue(all(f["measured"] for f in r["filesystems"]))
        self.assertFalse(any(f["mount"] in ("/config", "/proc", "/") for f in r["filesystems"]))

    def test_http_endpunkte_mit_token(self):
        store = a.Store(":memory:")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(store, lambda: {"cpu": 1.0}, "geheim"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            for path in ("/api/host", "/api/filesystems"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + path)
                self.assertEqual(e.exception.code, 401, path)
            with patch.object(a, "HOST_PROC", self.proc), patch.object(a, "HOST_SYS", self.sys), \
                    patch.object(a, "HOST_OS_RELEASE", self.osr), patch.object(a, "HOST_FS", self.fs):
                auth = {"Authorization": "Bearer geheim"}
                h = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/host", headers=auth)))
                self.assertEqual(h["hostname"], "debianvm")
                f = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/filesystems", headers=auth)))
                self.assertIn("/srv", [x["mount"] for x in f["filesystems"]])
                info = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/info", headers=auth)))
                self.assertTrue(info["host_os"])
                self.assertEqual(info["host_fs"], ["data"])
            # Fehler beim Lesen: 500 mit Text, der Agent läuft weiter
            a._HOST_CACHE.clear()
            with patch.object(a, "host_info", side_effect=RuntimeError("kaputt")):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(urllib.request.Request(base + "/api/host", headers={"Authorization": "Bearer geheim"}))
                self.assertEqual(e.exception.code, 500)
            self.assertTrue(json.load(urllib.request.urlopen(base + "/api/ping"))["ok"])
        finally:
            srv.shutdown()


class LinuxLastTest(unittest.TestCase):
    """Review 04.11.2026: /api/host und /api/filesystems zwischengespeichert, hängende Mounts blockieren nicht."""

    def test_zwischenspeicher(self):
        a._HOST_CACHE.clear()
        calls = []
        fn = lambda: calls.append(1) or {"n": len(calls)}
        self.assertEqual(a.cached("x", fn, ttl=30), {"n": 1})
        self.assertEqual(a.cached("x", fn, ttl=30), {"n": 1})
        self.assertEqual(len(calls), 1)
        self.assertEqual(a.cached("x", fn, ttl=0), {"n": 2})
        # Andere Host-Pfade (z. B. Test mit anderem Baum): eigener Eintrag
        with patch.object(a, "HOST_FS", "/anders"):
            self.assertEqual(a.cached("x", fn, ttl=30), {"n": 3})
        # Gleichzeitige Anfragen rechnen nur einmal
        a._HOST_CACHE.clear()
        slow_calls = []

        def slow():
            slow_calls.append(1)
            time.sleep(0.3)
            return {"ok": True}
        ts = [threading.Thread(target=lambda: a.cached("y", slow, ttl=30)) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(slow_calls), 1)

    def test_haengender_mount(self):
        d = tempfile.mkdtemp()
        os.makedirs(f"{d}/fs/nfs")
        os.makedirs(f"{d}/fs/ok")
        real = os.statvfs
        gate = threading.Event()

        def fake(path):
            if path.endswith("/nfs"):
                gate.wait(5)            # hängt wie ein weggefallener NFS-Mount
            return real(path)
        try:
            with patch.object(a.os, "statvfs", side_effect=fake), patch.object(a, "STATVFS_TIMEOUT", 0.3):
                t0 = time.time()
                r = a.filesystems(f"{d}/leer", f"{d}/leer", f"{d}/fs", f"{d}/kein-docker")
                self.assertLess(time.time() - t0, 2.5)
                by = {f["label"]: f for f in r["filesystems"]}
                self.assertTrue(by["nfs"].get("timeout"))
                self.assertFalse(by["nfs"]["measured"])
                self.assertTrue(by["ok"]["measured"])
                # Zweiter Aufruf, solange der alte Faden hängt: sofort, ohne neuen Faden
                t0 = time.time()
                self.assertEqual(a._statvfs(f"{d}/fs/nfs"), {"timeout": True})
                self.assertLess(time.time() - t0, 0.2)
        finally:
            gate.set()


FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fixtures")


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


class Server115:
    """Agent-Server auf einem freien Port für die Tests von 1.15.0."""

    def __init__(self, store=None, **kw):
        self.store = store or a.Store(":memory:")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(self.store, lambda: {}, "geheim", **kw))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_port

    def get(self, path, tok="geheim", headers=None):
        h = dict(headers or {})
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Agent115Test(unittest.TestCase):
    """1.15.0: Neustarts, Plattentemperaturen, Netz, SMART, Backups, RAID, Logs, Token-Bremse, Grössenlimit, Gesundheit."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    # --- Neustarts
    def test_neustarts_erkennen(self):
        st = a.Store(":memory:")
        t0 = 1_760_000_000
        self.assertTrue(st.note_boot(t0, 100, now=t0 + 100))            # erster Start: neu, Vorlaufzeit unbekannt
        self.assertFalse(st.note_boot(t0 + 1, 3700, now=t0 + 3700))     # NTP stellt die Uhr um 1 s: derselbe Start
        self.assertFalse(st.note_boot(t0, 7300, now=t0 + 7300))         # nur der Container neu gestartet
        self.assertTrue(st.note_boot(t0 + 8000, 60, now=t0 + 8060))     # Betriebszeit kleiner → Neustart
        # Agent war während des Neustarts gestoppt und die neue Betriebszeit ist schon grösser: trotzdem erkannt
        self.assertTrue(st.note_boot(t0 + 90000, 9000, now=t0 + 99000))
        b = st.boots(0)
        self.assertEqual([x["boot_time"] for x in b], [t0 + 90000, t0 + 8000, t0])
        self.assertEqual(b[1]["previous_uptime_s"], 7300)
        self.assertEqual(b[0]["previous_uptime_s"], 60)
        self.assertIsNone(b[2]["previous_uptime_s"])
        self.assertEqual({x["planned"] for x in b}, {"unknown"})

    def test_boot_aus_proc_und_endpunkt(self):
        write(f"{self.d}/proc/uptime", "3600.55 7000.00\n")
        write(f"{self.d}/proc/stat", "cpu  1 2 3 4\nbtime 1760000000\n")
        self.assertEqual(a.boot_now(f"{self.d}/proc"), (1760000000, 3600))
        st = a.Store(":memory:")
        self.assertTrue(a.check_boot(st, f"{self.d}/proc", now=1760003600))
        self.assertFalse(a.check_boot(st, f"{self.d}/proc", now=1760003660))
        srv = Server115(st)
        try:
            code, body, _ = srv.get("/api/boots?days=36500")
            self.assertEqual(code, 200)
            self.assertEqual(body["boots"][0]["boot_time"], 1760000000)
            code, info, _ = srv.get("/api/info")
            for k in ("api_level", "features", "last_boot", "boots_7d", "sampling_ok"):
                self.assertIn(k, info)
            self.assertEqual(info["api_level"], 15)
            self.assertIn("boots", info["features"])
            self.assertNotIn("smart", info["features"])
            self.assertNotIn("logs", info["features"])
            self.assertEqual(srv.get("/api/boots?days=x")[0], 400)
        finally:
            srv.close()

    # --- Plattentemperaturen: Aufbewahrung und Verdichtung
    def test_plattentemperaturen_pro_geraet(self):
        sysd = f"{self.d}/sys"
        write(f"{sysd}/block/sda/device/hwmon/hwmon3/temp1_input", "35000")
        write(f"{sysd}/block/sda/device/model", "ST16000NT001")
        write(f"{sysd}/block/nvme0n1/device/hwmon2/temp1_input", "41850")
        write(f"{sysd}/block/sdb/size", "1")           # ohne Temperatur (schläft): kommt aus SMART, falls bekannt
        os.makedirs(f"{sysd}/block/loop0", exist_ok=True)
        smart = {"/dev/sdb": {"device_name": "sdb", "temp": 31, "standby": False}}
        self.assertEqual(a.disk_temps(sysd, smart), {"sda": 35, "nvme0n1": 42, "sdb": 31})
        st = a.Store(":memory:")
        a.record_disk_temps(st, sysd, now=1_760_000_000)
        self.assertEqual(st.latest_series("disk:")["sda"]["v"], 35)

    def test_reihen_verdichtet_nach_zwei_tagen(self):
        st = a.Store(":memory:")
        h0 = 1_760_000_000 // 86400 * 86400
        for i in range(40 * 24 * 12):                  # 40 Tage alle 5 Minuten
            t = h0 + i * 300
            st.add_series(t, {"disk:sda": 30 + (i % 12), "disk:sdb": None})
        now = h0 + 40 * 86400
        raw = st.series("disk:", 0, "raw")["sda"]
        self.assertGreaterEqual(raw[0][0], now - 2 * 86400 - 300)      # Rohwerte nur 2 Tage
        self.assertLessEqual(len(raw), 2 * 24 * 12 + 1)
        hourly = st.series("disk:", now - 35 * 86400, "hour")["sda"]
        self.assertGreaterEqual(len(hourly), 35 * 24 - 1)              # ≥ 35 Tage Stundenwerte
        self.assertEqual(hourly[0][1:], [35.5, 41.0])                 # Mittel, Höchstwert
        daily = st.series("disk:", now - 35 * 86400, "day")["sda"]
        self.assertTrue(34 <= len(daily) <= 36, len(daily))
        self.assertNotIn("sdb", st.series("disk:", 0, "raw"))
        r = a.disktemps_reply(st, 35, None, sys_root=self.d, now=now)
        self.assertEqual(r["res"], "day")
        self.assertEqual(r["fields"], ["t", "avg", "max"])
        self.assertLess(len(json.dumps(r)), 5000)                      # Antwort bleibt klein
        self.assertEqual(a.series_res(7, None), "hour")
        self.assertEqual(a.series_res(30, "raw"), "hour")
        self.assertEqual(a.series_res(1, None), "raw")

    # --- Netz
    def test_netz_gateway_und_verlauf(self):
        # Default-Route über 192.168.1.1 (hex little endian 0101A8C0)
        write(f"{self.d}/proc/net/route", "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
                                          "eth0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
                                          "eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\n")
        self.assertEqual(a.default_gateway(f"{self.d}/proc"), "192.168.1.1")
        self.assertEqual(a.parse_target("1.1.1.1:443"), ("1.1.1.1", 443))
        self.assertEqual(a.parse_target("[2606:4700::1111]:53"), ("2606:4700::1111", 53))
        self.assertIsNone(a.parse_target("x:99999"))
        self.assertIsNone(a.parse_target("http://evil"))
        answers = iter([5.0, None, 7.0, None, None, None])
        vals = a.net_round({"gateway": ("192.168.1.1", 53), "internet": ("1.1.1.1", 443)}, probe=lambda h, p: next(answers))
        self.assertEqual(vals["net:gateway:rtt"], 7.0)
        self.assertAlmostEqual(vals["net:gateway:loss"], 33.3)
        self.assertIsNone(vals["net:internet:rtt"])
        self.assertEqual(vals["net:internet:loss"], 100.0)
        st = a.Store(":memory:")
        st.add_series(int(time.time()), vals)
        r = a.net_reply(st, 1, proc_dir=f"{self.d}/proc")
        self.assertEqual(r["series"]["gateway"]["rtt"][0][1], 7.0)
        self.assertEqual(r["latest"]["internet"]["loss"], 100.0)
        self.assertEqual(r["targets"]["gateway"], "192.168.1.1:53")

    def test_tcp_probe_echt(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            self.assertIsNotNone(a.tcp_probe("127.0.0.1", s.getsockname()[1], 1))
        finally:
            s.close()
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        self.assertIsNotNone(a.tcp_probe("127.0.0.1", port, 1))      # abgelehnt (RST) = Antwort

    # --- SMART
    def test_smart_befehle_nur_lesen(self):
        self.assertEqual(a.smartctl_args("scan"), ["smartctl", "--scan", "--json=c"])
        self.assertEqual(a.smartctl_args("read", "/dev/sda", "sat"), ["smartctl", "--json=c", "-a", "-n", "standby", "-d", "sat", "/dev/sda"])
        for bad in [("read", "/dev/sda; reboot", None), ("read", "-t long", None), ("read", "/dev/sda", "sat -t long"),
                    ("read", "/etc/shadow", None), ("test", "/dev/sda", None), ("read", "/dev/../etc", None), ("read", 5, None)]:
            self.assertIsNone(a.smartctl_args(*bad), bad)
        for args in (a.smartctl_args("scan"), a.smartctl_args("read", "/dev/nvme0", "nvme")):
            self.assertFalse({"-t", "--test", "-s", "--smart", "-o", "-S", "--set", "-X"} & set(args))

    def test_smart_json_ata_nvme_standby(self):
        devs = a.parse_smart_scan(fixture("smartctl_scan.json"))
        self.assertEqual([d["name"] for d in devs], ["/dev/sda", "/dev/nvme0", "/dev/sdc"])
        self.assertIsNone(devs[2]["type"])
        p = a.parse_smart(fixture("smartctl_ata.json"))
        self.assertEqual((p["model_name"], p["serial_number"], p["power_on_hours"], p["temp"]), ("ST16000NT001-3LV101", "ZRS0TEST", 10712, 36))
        self.assertEqual((p["reallocated"], p["pending"], p["uncorrectable"], p["crc"]), (8, 2, 0, 3))
        self.assertEqual(p["attrs"]["5"]["raw_value"], 8)
        self.assertEqual(p["rotational_speed"], 7200)
        self.assertTrue(p["smart_passed"])
        self.assertFalse(p["standby"])
        n = a.parse_smart(fixture("smartctl_nvme.json"))
        self.assertEqual((n["media_errors"], n["percentage_used"], n["power_on_hours"], n["temp"], n["unsafe_shutdowns"]), (0, 3, 4321, 41, 9))
        self.assertEqual(n["attrs"]["percentage_used"]["raw_value"], 3)
        self.assertEqual(n["device_protocol"], "NVMe")
        self.assertTrue(a.parse_smart(fixture("smartctl_standby.json"))["standby"])
        self.assertIsNone(a.parse_smart("kein json"))
        row = a.smart_day_row(p)
        self.assertEqual({k: row[k] for k in ("realloc", "pending", "uncorr", "crc", "temp_max", "hours")},
                         {"realloc": 8, "pending": 2, "uncorr": 0, "crc": 3, "temp_max": 36, "hours": 10712})

    def test_smart_monitor_tageswerte_und_endpunkte(self):
        class FakeHelper:
            def __init__(self):
                self.calls, self.standby = [], False

            def call(self, op, dev=None, dtype=None):
                self.calls.append((op, dev, dtype))
                if op == "scan":
                    return 0, json.dumps({"devices": [{"name": "/dev/sda", "type": "sat"}]})
                return 0, fixture("smartctl_standby.json" if self.standby else "smartctl_ata.json")
        st = a.Store(":memory:")
        h = FakeHelper()
        mon = a.SmartMonitor(st, h, enabled=True)
        t = int(time.time())
        mon.poll(t)
        h.standby = True
        mon.poll(t + 60)                                  # schlafend: letzter Stand bleibt, kein neuer Tageswert
        cur = mon.current()
        self.assertEqual(cur["disks"][0]["serial_number"], "ZRS0TEST")
        self.assertTrue(cur["disks"][0]["standby"])
        self.assertEqual(h.calls[1], ("read", "/dev/sda", "sat"))
        hist = mon.history(30, now=t + 60)
        self.assertEqual(hist["disks"][0]["serial"], "ZRS0TEST")
        self.assertEqual(len(hist["disks"][0]["rows"]), 1)
        self.assertEqual(hist["disks"][0]["rows"][0]["realloc"], 8)
        # gleicher Tag, höhere Temperatur früher: Höchstwert bleibt
        st.add_smart_day("ZRS0TEST", dict(a.smart_day_row(a.parse_smart(fixture("smartctl_ata.json"))), temp_max=30), t + 120)
        self.assertEqual(st.smart_days(0)["ZRS0TEST"][0]["temp_max"], 36)
        srv = Server115(st, smart=mon)
        try:
            code, body, _ = srv.get("/api/smart")
            self.assertEqual(code, 200)
            self.assertTrue(body["enabled"])
            code, body, _ = srv.get("/api/smart/history?days=7")
            self.assertEqual(body["disks"][0]["rows"][0]["hours"], 10712)
        finally:
            srv.close()
        off = a.SmartMonitor(st, FakeHelper(), enabled=False).current()
        self.assertFalse(off["enabled"])
        self.assertIn("NZBLAB_SMART", off["error"])

    @unittest.skipUnless(hasattr(os, "fork") and os.geteuid() == 0, "nur als root prüfbar")
    def test_smart_helfer_prozess(self):
        h = a.SmartHelper()
        self.assertTrue(h.start())
        try:
            with patch.object(a, "run_smartctl", side_effect=AssertionError("Hauptprozess darf nicht selbst starten")):
                rc, out = h.call("scan")          # läuft im Helfer (smartctl fehlt hier vermutlich: 127)
            self.assertIn(rc, (0, 1, 2, 4, 127))
            self.assertEqual(h.call("read", "/dev/sda; reboot"), (2, ""))
        finally:
            h.sock.close()
            os.waitpid(h.pid, 0)

    # --- Backups
    def _restic(self, path, t):
        for sub in ("data", "keys", "index", "snapshots"):
            os.makedirs(f"{path}/{sub}", exist_ok=True)
        write(f"{path}/config", "x")
        for i, ts in enumerate((t - 86400, t)):
            write(f"{path}/snapshots/{i:064x}", "enc")
            os.utime(f"{path}/snapshots/{i:064x}", (ts, ts))

    def test_backup_repos_erkennen(self):
        t = 1_760_000_000
        share = f"{self.d}/mnt/user"
        self._restic(f"{share}/backups/restic-repo", t)
        borg = f"{share}/backups/borg"
        write(f"{borg}/README", "This is a Borg Backup repository.\nSee https://borgbackup.readthedocs.io/\n")
        write(f"{borg}/config", "[repository]\nversion = 1\n")
        write(f"{borg}/index.42", "x")
        write(f"{borg}/hints.42", "x")
        os.utime(f"{borg}/index.42", (t - 3600, t - 3600))
        os.utime(f"{borg}/hints.42", (t - 7200, t - 7200))
        kopia = f"{share}/data/kopia"
        write(f"{kopia}/kopia.repository.f", "{}")
        write(f"{kopia}/xn0/_abc.f", "x")
        write(f"{kopia}/q12/q12345.f", "x")
        write(f"{kopia}/p00/p00123.f", "x")
        os.utime(f"{kopia}/xn0/_abc.f", (t - 60, t - 60))
        os.utime(f"{kopia}/q12/q12345.f", (t - 600, t - 600))
        os.utime(f"{kopia}/p00/p00123.f", (t + 999, t + 999))   # Datenblöcke zählen nicht
        write(f"{share}/Filme/Film (2020)/film.mkv", "x")
        self.assertEqual(a.detect_repo(f"{share}/backups/restic-repo"), "restic")
        self.assertEqual(a.detect_repo(borg), "borg")
        self.assertEqual(a.detect_repo(kopia), "kopia")
        self.assertIsNone(a.detect_repo(f"{share}/Filme"))
        found = {b["kind"]: b for b in a.scan_backups(auto_roots=[share])}
        self.assertEqual(found["restic"]["last_success"], t)
        self.assertEqual(found["restic"]["snapshots"], 2)
        self.assertEqual(found["borg"]["last_success"], t - 3600)
        self.assertEqual(found["kopia"]["last_success"], t - 60)
        self.assertIsNone(found["restic"]["last_error"])              # ehrlich: unbekannt
        self.assertEqual(found["restic"]["found_by"], "auto")

    def test_backup_log_und_endpunkt(self):
        log = f"{self.d}/logs/rsync-backup.log"
        write(log, fixture("backup_rsync.log"))
        info = a.backup_log_info(log)
        self.assertEqual(info["kind"], "rsync")
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(info["last_success"])), "2026-11-06 03:15")
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(info["last_error"])), "2026-11-05 03:00")
        self.assertIn("rsync error", info["error_text"])
        self.assertNotIn("/mnt/user/data/", info["error_text"])
        bs = a.BackupScanner(explicit=[log, f"{self.d}/fehlt"], auto_roots=[], auto=False)
        bs.refresh()
        srv = Server115(backups=bs)
        try:
            code, body, _ = srv.get("/api/backups")
            self.assertEqual(code, 200)
            kinds = {b["kind"] for b in body["backups"]}
            self.assertEqual(kinds, {"rsync", "unknown"})
            for b in body["backups"]:
                self.assertEqual(set(b) >= {"kind", "name", "last_success", "last_error", "source"}, True)
        finally:
            srv.close()

    # --- RAID
    def test_mdstat_zfs_btrfs(self):
        md = {p["name"]: p for p in a.parse_mdstat(fixture("mdstat_raid.txt"))}
        self.assertTrue(md["md1"]["degraded"])
        self.assertEqual(md["md1"]["level"], "raid5")
        self.assertEqual(md["md1"]["sync"], {"action": "recovery", "percent": 12.6, "finish_min": 284.3})
        self.assertTrue([d for d in md["md1"]["devices"] if d["name"] == "sdd1"][0]["failed"])
        self.assertFalse(md["md0"]["degraded"])
        self.assertEqual((md["md0"]["total"], md["md0"]["active"]), (2, 2))
        self.assertEqual(md["md2"]["sync"]["waiting"], "pending")
        self.assertIn("read-only", md["md2"]["state"])
        self.assertTrue(md["md127"]["degraded"])                     # inaktiv
        self.assertEqual(a.parse_mdstat("mdState=STARTED\nmdResync=0\n")[0]["kind"], "unraid")
        proc = f"{self.d}/proc"
        write(f"{proc}/spl/kstat/zfs/tank/state", "DEGRADED\n")
        write(f"{proc}/spl/kstat/zfs/backup/state", "ONLINE\n")
        pools, src = a.zfs_pools(proc)
        self.assertEqual(src, "kstat")
        self.assertEqual({p["name"]: p["degraded"] for p in pools}, {"tank": True, "backup": False})
        pools, src = a.zfs_pools(f"{self.d}/leer", run=lambda: "rpool\tONLINE\n")
        self.assertEqual((src, pools[0]["name"]), ("zpool", "rpool"))
        sysd = f"{self.d}/sys"
        u = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
        write(f"{sysd}/fs/btrfs/{u}/label", "pool")
        os.makedirs(f"{sysd}/fs/btrfs/{u}/allocation/data/raid1", exist_ok=True)
        os.makedirs(f"{sysd}/fs/btrfs/{u}/allocation/metadata/raid1c3", exist_ok=True)
        os.makedirs(f"{sysd}/fs/btrfs/{u}/devices/sdb", exist_ok=True)
        write(f"{sysd}/fs/btrfs/{u}/devinfo/1/error_stats", "write_errs 0\nread_errs 2\nflush_errs 0\ncorruption_errs 1\ngeneration_errs 0\n")
        write(f"{sysd}/fs/btrfs/{u}/devinfo/1/missing", "0")
        write(f"{sysd}/fs/btrfs/{u}/devinfo/2/missing", "1")
        os.makedirs(f"{sysd}/fs/btrfs/features", exist_ok=True)
        bt = a.btrfs_pools(sysd)
        self.assertEqual(len(bt), 1)
        self.assertEqual(bt[0]["profiles"], {"data": "raid1", "metadata": "raid1c3"})
        self.assertEqual(bt[0]["errors"], 3)
        self.assertTrue(bt[0]["degraded"])
        write(f"{proc}/mdstat", fixture("mdstat_raid.txt"))
        r = a.raid_status(proc, sysd)
        self.assertTrue(r["degraded"])
        self.assertEqual(r["sources"], {"mdadm": True, "zfs": "kstat", "btrfs": True})
        self.assertEqual(len(r["pools"]), 4 + 2 + 1)

    # --- Logs
    def test_logs_gefiltert(self):
        now = int(time.time())
        lines = fixture("syslog_sample.txt").splitlines()
        out = [time.strftime("%b %e %H:%M:%S", time.localtime(now - 3600 + i * 10)) + l[15:] for i, l in enumerate(lines)]
        out.insert(0, time.strftime("%b %e %H:%M:%S", time.localtime(now - 3 * 86400)) + " tower kernel: error: alt")
        write(f"{self.d}/log/syslog", "\n".join(out) + "\n")
        r = a.collect_logs(f"{self.d}/log", hours=24, limit=200, now=now)
        text = json.dumps(r, ensure_ascii=False)
        for secret in ("203.0.113.77", "192.168.1.20", "2001:db8::42", "00:11:22:33:44:55", "hans.muster@example.com", "hunter2",
                       "abcdef1234567890", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg", "eyJhbGciOiJIUzI1NiJ9", "geheim123",
                       "maria", "Steuern", "admin", "tower", "bob"):
            self.assertNotIn(secret, text, secret)
        self.assertNotIn("alt", [l["text"] for l in r["lines"]])          # älter als 24 h
        self.assertNotIn("CMD", text)                                    # Routine (keine Fehlerzeile)
        io_err = [l for l in r["lines"] if "I/O error" in l["text"]]
        self.assertEqual(len(io_err), 1)
        self.assertEqual(io_err[0]["n"], 2)                               # gleiche Meldung zusammengefasst
        self.assertEqual(io_err[0]["src"], "kernel")
        self.assertIn("timed out", text)                                 # Warnung mit Zeit «12:34:56» bleibt lesbar
        self.assertEqual(a.collect_logs(f"{self.d}/log", 24, 2, now)["lines"].__len__(), 2)

    def test_redaktion(self):
        r = a.redact_log
        self.assertEqual(r("um 12:34:56 fertig"), "um 12:34:56 fertig")
        self.assertEqual(r("from 10.0.0.5 port 22"), "from <ip> port 22")
        self.assertEqual(r("fe80::1%eth0 up"), "<ip> up")
        self.assertEqual(r("mac aa:bb:cc:dd:ee:ff"), "mac <mac>")
        self.assertEqual(r("MAC=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:08:00"), "MAC=<mac>")
        self.assertEqual(r("a@b.ch"), "<email>")
        self.assertEqual(r("password: hallo welt"), "password: <redacted> welt")
        self.assertEqual(r("x api_key=\"a b\" y"), "x api_key=<redacted> y")
        self.assertIn("<redacted>", r("Authorization: Bearer abcdefghijkl"))
        self.assertEqual(r("/home/maria/x"), "/home/<user>/x")
        self.assertNotIn("Ferien", r("/mnt/user/Fotos/Ferien/1.jpg"))

    def test_logs_endpunkt_nur_freiwillig(self):
        write(f"{self.d}/log/syslog", "")
        srv = Server115(log_dir=f"{self.d}/log")
        try:
            with patch.object(a, "LOGS_ON", False):
                self.assertEqual(srv.get("/api/logs")[0], 403)
            with patch.object(a, "LOGS_ON", True):
                code, body, _ = srv.get("/api/logs?hours=2&max=5")
                self.assertEqual(code, 200)
                self.assertTrue(body["redacted"])
                code, info, _ = srv.get("/api/info")
                self.assertIn("logs", info["features"])
        finally:
            srv.close()

    # --- Sicherheit
    def test_token_bremse(self):
        lim = a.AuthLimiter(max_fail=10, window=60, block=300)
        for i in range(9):
            self.assertFalse(lim.fail("1.2.3.4", now=100 + i))
        self.assertEqual(lim.blocked_for("1.2.3.4", now=110), 0)
        self.assertTrue(lim.fail("1.2.3.4", now=110))
        self.assertGreater(lim.blocked_for("1.2.3.4", now=111), 0)
        self.assertEqual(lim.blocked_for("5.6.7.8", now=111), 0)           # andere Adresse nicht betroffen
        self.assertEqual(lim.blocked_for("1.2.3.4", now=411), 0)           # nach 5 Minuten wieder frei
        lim2 = a.AuthLimiter(max_fail=10, window=60, block=300)
        for i in range(20):                                                 # langsam raten (1 pro 10 s) sperrt nicht
            self.assertFalse(lim2.fail("1.2.3.4", now=i * 10))
        srv = Server115()
        try:
            codes = [srv.get("/api/info", tok="falsch")[0] for _ in range(11)]
            self.assertEqual(codes[:10], [401] * 10)                       # 10 Fehlversuche, danach gesperrt
            self.assertEqual(codes[10], 429)
            code, body, headers = srv.get("/api/info")                     # auch mit richtigem Token gesperrt
            self.assertEqual(code, 429)
            self.assertTrue(int(headers["Retry-After"]) > 0)
            self.assertEqual(srv.get("/api/ping", tok=None)[0], 200)       # Ping bleibt
            self.assertEqual(srv.get("/api/info", tok=None)[0], 429)
        finally:
            srv.close()

    def test_grosse_anfragen_413(self):
        from http.client import HTTPConnection
        srv = Server115(crashes=a.CrashStore(f"{self.d}/crashes"))       # Absturzberichte behalten ihr Limit (128 000)
        try:
            for method, path, n, status in [("POST", "/api/config", 70000, 413), ("POST", "/api/files/refresh", 65537, 413),
                                            ("GET", "/api/info", 100000, 413), ("POST", "/api/crash", 100000, 400)]:
                conn = HTTPConnection("127.0.0.1", srv.port, timeout=3)
                conn.putrequest(method, path)
                conn.putheader("Authorization", "Bearer geheim")
                conn.putheader("Content-Length", str(n))
                conn.endheaders()
                if status == 400:
                    conn.send(b"x" * n)
                r = conn.getresponse()
                self.assertEqual(r.status, status, (method, path, n))
                conn.close()
            conn = HTTPConnection("127.0.0.1", srv.port, timeout=3)
            conn.putrequest("POST", "/api/config")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 411)
            conn.close()
        finally:
            srv.close()

    def test_gesundheit_und_bind(self):
        old = a.SAMPLER_STATE["last"]
        srv = Server115()
        try:
            a.SAMPLER_STATE["last"] = time.time()
            self.assertEqual(srv.get("/api/health", tok=None)[0], 200)
            a.SAMPLER_STATE["last"] = time.time() - 3 * a.INTERVAL - 30
            code, body, _ = srv.get("/api/health", tok=None)
            self.assertEqual(code, 503)
            self.assertFalse(body["ok"])
            self.assertFalse(srv.get("/api/ping", tok=None)[1]["sampling_ok"])
        finally:
            a.SAMPLER_STATE["last"] = old
            srv.close()
        s = a.make_server("127.0.0.1", 0, a.make_handler(a.Store(":memory:"), lambda: {}, "geheim"))
        self.assertEqual(s.server_address[0], "127.0.0.1")
        s.server_close()
        s = a.make_server("kein-ip", 0, a.make_handler(a.Store(":memory:"), lambda: {}, "geheim"))
        self.assertEqual(s.server_address[0], "0.0.0.0")
        s.server_close()

    def test_raid_und_netz_endpunkte(self):
        srv = Server115()
        try:
            code, body, _ = srv.get("/api/raid")
            self.assertEqual(code, 200)
            self.assertIn("pools", body)
            with patch.object(a, "NET_ON", False):
                code, body, _ = srv.get("/api/net?days=3")
                self.assertEqual((code, body["enabled"], body["res"]), (200, False, "hour"))
            code, body, _ = srv.get("/api/disktemps?days=1")
            self.assertEqual((code, body["res"]), (200, "raw"))
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
