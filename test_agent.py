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
import unittest.mock
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
        write(f"{self.sys}/class/hwmon/hwmon5/temp1_crit", "100000")
        write(f"{self.sys}/class/hwmon/hwmon5/temp1_max", "95000")
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
        # 2.63: Grenzwert laut hwmon (crit vor max), nur für Sensoren, die einen melden
        self.assertEqual(r["limits"]["CPU"], 100.0)
        self.assertNotIn("NVMe 0", r["limits"])
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
        self.assertIn("NASlab", html)
        self.assertNotIn("geheim", html)
        # 1.9.0: Token nur im Tab (sessionStorage), keine NAS-Werte in der Seite, Seite holt alles per Token
        self.assertIn("sessionStorage", html)
        self.assertNotIn("localStorage.setItem", html)
        self.assertNotIn(a.socket.gethostname(), html.replace("NASlab", "").replace("NZBLab", ""))
        self.assertIn("Authorization", html)
        self.assertIn("/api/cache/shares", html)
        page2 = urllib.request.urlopen(base + "/index.html")
        self.assertEqual(page2.read().decode(), html)
        for path in ("/api/info", "/api/now", "/api/history?hours=24", "/api/cache/shares", "/api/today"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + path, headers={"Authorization": "Bearer falsch"}))
            self.assertEqual(e.exception.code, 401, path)
        info = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/info", headers={"Authorization": "Bearer geheim"})))
        self.assertEqual(info["version"], "1.19.2")
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
            self.assertNotIn("version", r)                   # ab 1.19.0 ohne Version
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

    def test_netz_gestapelt_nur_einmal(self):
        """Unraid: eth0 → bond0 → br0 tragen denselben Verkehr; nur die oberste Schnittstelle (ohne «master») zählt (1.18.1)."""
        import tempfile, nzblab_agent as a
        with tempfile.TemporaryDirectory() as root:
            proc, sysd = os.path.join(root, "proc"), os.path.join(root, "sys")
            os.makedirs(os.path.join(proc, "1/net"))
            for i in ("eth0", "bond0", "br0", "eth1"):
                os.makedirs(os.path.join(sysd, "class/net", i))
            os.symlink(os.path.join(sysd, "class/net/bond0"), os.path.join(sysd, "class/net/eth0/master"))
            os.symlink(os.path.join(sysd, "class/net/br0"), os.path.join(sysd, "class/net/bond0/master"))
            def dev(rx, tx):
                open(os.path.join(proc, "1/net/dev"), "w").write("h1\nh2\n    lo: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\n"
                    f"  eth0: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n bond0: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n"
                    f"   br0: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n  eth1: {rx * 2} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n")
            smp = a.Sampler(sysd, proc)
            dev(1000, 500); smp.net(100.0)
            dev(3000, 1500); n = smp.net(102.0)
            # eth1 hängt an keiner Brücke und zählt eigenständig (Synology-Fall mit zwei Karten)
            self.assertEqual(n, {"br0": {"rx": 1000, "tx": 500}, "eth1": {"rx": 2000, "tx": 500}})



class Agent119RestTest(unittest.TestCase):
    def test_hosts_zu_breit_abgewiesen(self):
        import nzblab_agent as a
        self.assertEqual(a._extra_hosts(".com, *.example.org, nas.example.org, .vpn.example.net"), ("nas.example.org", ".vpn.example.net"))

    def test_hello_ohne_version(self):
        import inspect, nzblab_agent as a
        src = inspect.getsource(a)
        line = [l for l in src.splitlines() if '"proof": hello_proof(tok, nonce)' in l][0]
        self.assertNotIn("VERSION", line)

    def test_ntfy_zahlenname_nie_lokal(self):
        import nzblab_agent as a
        old = a.ntfy_url
        try:
            for u in ("https://2915221794/thema12345", "https://0xadc0ffee/thema12345"):
                a.ntfy_url = lambda u=u: u
                self.assertFalse(a.ntfy_names_allowed(), u)
            a.ntfy_url = lambda: "https://nas/thema12345"
            self.assertTrue(a.ntfy_names_allowed())
        finally:
            a.ntfy_url = old


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
        with patch.object(a, "LOGS_ON", True):
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
        # Lesen: ab 1.16.1 (Audit Lauf 2) immer mit Token, auch vom NAS selbst (Loopback) und hinter einem Proxy
        for hdr in ({}, {"Host": "127.0.0.1:8099"}, {"Host": "evil.example:8099"}, {"X-Forwarded-For": "203.0.113.9"}):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(base + "/api/crashes", headers=hdr))
            self.assertEqual(e.exception.code, 401, hdr)
            self.assertNotIn("IllegalStateException", e.exception.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/crashes", headers={"Authorization": "Bearer falsch"}))
        self.assertEqual(e.exception.code, 401)
        got = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/crashes", headers={"Authorization": "Bearer geheim"})))["crashes"]
        self.assertEqual(got[0]["version"], "2.18.14")
        self.assertIn("IllegalStateException", got[0]["trace"])
        # Preis-Beobachtungsliste bleibt ohne Token aus dem Heimnetz, aber nicht mit fremdem Host-Namen (DNS-Rebinding)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/api/prices/watch", headers={"Host": "evil.example:8099"}))
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

    # ---- Plattenpreise P1: Quelle und Zeit, abgeschaltete Quellen, Werbelinks
    def test_p1_quelle_und_zeit_pro_angebot(self):
        calls, s = self.run_country("CH", {"28TB": (200, "toppreise_search.html")})
        b = s["hdd28"]
        self.assertEqual((b["source"], b["source_id"]), ("Toppreise.ch", "toppreise"))
        self.assertAlmostEqual(b["fetched_at"], time.time(), delta=30)
        self.assertFalse(b["search_only"]); self.assertFalse(b["no_alert"])
        p = a.price_summary()
        self.assertEqual([x["id"] for x in p["sources"]], ["toppreise"])
        self.assertEqual(p["sources"][0]["url"], "https://www.toppreise.ch")
        self.assertTrue(p["sources"][0]["allowed"]); self.assertFalse(p["search_only"])

    def test_p1_quelle_aus_kein_abruf_nur_suchlink(self):
        a.save_settings({"price_country": "CH", "price_sources_off": ["toppreise"]})
        def boom(url, lang):
            raise AssertionError("abgeschaltete Quelle darf nicht abgefragt werden: " + url)
        self.assertIsNone(a.fetch_own_prices("CH", fetch=boom, pause=0))
        p = a.price_summary()
        self.assertTrue(p["search_only"]); self.assertFalse(p["sources"][0]["allowed"])
        for x in p["sizes"]:
            self.assertIsNone(x["best"]); self.assertTrue(x["search_only"]); self.assertFalse(x["unreadable"])
            self.assertIn("toppreise.ch/produktsuche", x["search_url"]); self.assertFalse(x["cheap"])
        # auch eine frische externe prices.json ändert daran nichts
        write(f"{a.CONFIG}/prices.json", json.dumps({"updated": int(time.time()), "queries": {
            "hdd28": {"label": "28 TB", "tb": 28, "history": [], "best": {"name": "X", "chf": 900.0}}}}))
        a.PRICES_FILE = f"{a.CONFIG}/prices.json"
        self.assertTrue(all(x["best"] is None for x in a.price_summary()["sizes"]))
        # Wieder an: Zahlen kommen zurück
        a.save_settings({"price_sources_off": []})
        self.assertFalse(a.price_summary()["search_only"])

    def test_p1_amazon_quelle_ohne_meldung_und_ohne_werbelink(self):
        calls, s = self.run_country("US", {"diskprices.com": (200, "diskprices_us.html")})
        b = s["hdd28"]
        self.assertEqual(b["source"], "Amazon.com (diskprices.com)")
        self.assertTrue(b["no_alert"])
        a.save_settings({"price_alerts": {"hdd28": 1000}})              # weit über dem Preis: bei Toppreise wäre das «günstig»
        self.assertFalse({x["key"]: x for x in a.price_summary()["sizes"]}["hdd28"]["cheap"])
        self.assertNotIn("ad_link", b["best"])

    def test_p1_alte_datei_amazon_ersatz_ohne_meldung(self):
        # Datei ohne source_id (vor P1): Amazon.de als Ersatz bei DE erkennt man am Shop
        self.assertTrue(a.AMAZON_HOST.search("www.amazon.de"))
        calls, s = self.run_country("DE", {"geizhals.de/robots.txt": (200, "robots_geizhals.txt"), "diskprices.com/?locale=de": (200, "diskprices_us.html")})
        path = a.own_prices_path("DE"); d = json.load(open(path))
        for q in d["queries"].values():
            if q.get("best"): q["best"].pop("source_id", None); q["best"].pop("source", None)
        json.dump(d, open(path, "w"))
        a.save_settings({"price_alerts": {"hdd28": 1000}})
        x = {y["key"]: y for y in a.price_summary()["sizes"]}["hdd28"]
        self.assertTrue(x["no_alert"]); self.assertFalse(x["cheap"])

    def test_p1_werbelink_aufbau(self):
        pub = lambda h: ["93.184.216.34"]                               # noqa: E731
        link = "https://www.galaxus.ch/de/s1/product/seagate-ironwolf-pro-28tb-123?x=1&y=2"
        # ohne Kennung: kein Werbelink
        self.assertEqual(a.affiliate_url(link, pub), "")
        self.assertFalse(a.ads_available())
        write(f"{a.CONFIG}/affiliate.json", json.dumps({"galaxus": {"id": "12345", "program": "98765"},
                                                          "awin": {"id": "777"}}))     # awin unvollständig (mid fehlt)
        self.assertTrue(a.ads_available())
        ad = a.affiliate_url(link, pub)
        self.assertTrue(ad.startswith("https://clk.tradedoubler.com/click?p=98765&a=12345&url=https%3A%2F%2Fwww.galaxus.ch"))
        self.assertIn("%3Fx%3D1%26y%3D2", ad)                          # Ziel vollständig kodiert, keine fremden Parameter
        self.assertEqual(a.affiliate_url("https://www.reichelt.de/x", pub), "")        # Awin ohne Händler-ID
        # Amazon nie, auch nicht mit Eintrag; andere Shops, http und private Ziele nie
        write(f"{a.CONFIG}/affiliate.json", json.dumps({"galaxus": {"id": "12345", "program": "98765"},
                                                          "amazon": {"id": "x-21", "program": "1"}}))
        for bad in ("https://www.amazon.com/dp/B0D1234567", "https://www.amazon.de/dp/B0D1", "https://amzn.to/abc",
                    "https://www.toppreise.ch/x", "http://www.galaxus.ch/x", "https://user:pw@" + "www.galaxus.ch/x"):
            self.assertEqual(a.affiliate_url(bad, pub), "", bad)
        self.assertEqual(a.affiliate_url(link, lambda h: ["192.168.1.5"]), "")        # Host zeigt ins Heimnetz
        # Ungültige Kennung (Sonderzeichen) wird nicht eingebaut
        write(f"{a.CONFIG}/affiliate.json", json.dumps({"galaxus": {"id": "1&evil=1", "program": "9"}}))
        self.assertEqual(a.affiliate_url(link, pub), "")
        # with_ad lässt den normalen Link unverändert
        write(f"{a.CONFIG}/affiliate.json", json.dumps({"galaxus": {"id": "12345", "program": "98765"}}))
        o = a.with_ad({"name": "X", "link": link}, pub)
        self.assertEqual(o["link"], link); self.assertIn("ad_link", o)
        self.assertNotIn("ad_link", a.with_ad({"name": "X", "link": "https://www.toppreise.ch/x"}, pub))

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
        os.environ["NZBLAB_AGENT_TOKEN"] = "vorgegeben-" + "x" * 24
        self.assertEqual(a.token(), "vorgegeben-" + "x" * 24)

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
        write(f"{self.d}/token.txt", "alter-token-" + "a" * 24)
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
            self.assertEqual(f.read(), "alter-token-" + "a" * 24)

    def test_verzeichnis_statt_datei(self):
        os.makedirs(f"{self.d}/token.txt")
        t, _ = self.quiet(a.token)
        self.assertEqual(t, "")
        self.assertTrue(os.path.isdir(f"{self.d}/token.txt"))

    def test_variable_gewinnt(self):
        write(f"{self.d}/token.txt", "aus-datei")
        os.environ["NZBLAB_AGENT_TOKEN"] = "  aus-variable-" + "v" * 20 + " "
        self.assertEqual(a.token(), "aus-variable-" + "v" * 20)
        self.assertEqual(a.TOKEN_STATE["source"], "env")
        # auch bei leerer Datei: Variable gilt, Datei bleibt, kein Fehler
        write(f"{self.d}/token.txt", "")
        self.assertEqual(a.token(), "aus-variable-" + "v" * 20)
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
            self.assertEqual(set(ping), {"ok", "config_persistent", "api_level", "sampling_ok"})   # ohne Token keine Version (ab 1.19.0)
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
                body = json.load(e.exception)                  # ab 1.19.0 auch von 127.0.0.1 nur der Code
                self.assertNotIn("token_error_detail", body)
                self.assertNotIn("token.txt", json.dumps(body))
                self.assertTrue(body["token_error"])
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
            self.assertEqual(info["api_level"], 16)
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

    # --- Synology DSM 7 (ab 1.17.0, Prüfung 10.11.2026): Platten heissen sata1…, sas1…, usb1… statt sda
    def test_synology_plattennamen(self):
        h = FakeHost()
        s = a.Sampler(h.sys, h.proc)
        write(f"{h.proc}/diskstats", "   8       0 sata1 0 0 0 0 0 0 0 0 0 0 0\n   8      16 sata1p1 0 0 0 0 0 0 0 0 0 0 0\n"
                                     "   8      32 sas1 0 0 0 0 0 0 0 0 0 0 0\n   8      48 usb1 0 0 0 0 0 0 0 0 0 0 0\n"
                                     "   8      64 sdb 0 0 0 0 0 0 0 0 0 0 0\n   7       0 loop0 0 0 0 0 0 0 0 0 0 0 0\n")
        s.sample()
        time.sleep(1.1)
        write(f"{h.proc}/diskstats", "   8       0 sata1 0 0 2048 0 0 0 0 0 0 0 0\n   8      16 sata1p1 0 0 2048 0 0 0 0 0 0 0 0\n"
                                     "   8      32 sas1 0 0 1024 0 0 0 0 0 0 0 0\n   8      48 usb1 0 0 512 0 0 0 0 0 0 0 0\n"
                                     "   8      64 sdb 0 0 1024 0 0 0 0 0 0 0 0\n   7       0 loop0 0 0 9999 0 0 0 0 0 0 0 0\n")
        r = s.sample()
        self.assertEqual(set(r["disks"]), {"sata1", "sas1", "usb1", "sdb"})      # Partition sata1p1 und loop0 nicht
        # Temperaturen: drivetemp unter /sys/block/sata1
        write(f"{h.sys}/block/sata1/device/hwmon/hwmon5/temp1_input", "41000")
        write(f"{h.sys}/block/sata1/size", "100")
        self.assertEqual(a.disk_temps(h.sys).get("sata1"), 41.0)
        # SMART: /dev/sata1 ist ein erlaubtes Gerät, Unsinn weiterhin nicht
        self.assertEqual(a.smartctl_args("read", "/dev/sata1", "sat")[-1], "/dev/sata1")
        self.assertIsNotNone(a.smartctl_args("read", "/dev/usb1", None))
        self.assertIsNone(a.smartctl_args("read", "/dev/sata1; reboot", None))
        self.assertIsNone(a.smartctl_args("read", "/dev/sata", None))

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

    def test_mover_log(self):
        now = int(time.time())
        def st(dt):
            return time.strftime("%b %e %H:%M:%S", time.localtime(now - dt))
        # Start ohne Ende: läuft laut Protokoll
        write(f"{self.d}/log/syslog", f"{st(1800)} nas move: mover: started\n{st(1700)} nas move: file: /mnt/cache/a/finished.mkv\n")
        r = a.mover_log(f"{self.d}/log", now)
        self.assertEqual(r["state"], "running")
        self.assertEqual(r["started"], now - 1800)
        self.assertIsNone(r["ended"])
        # Ende-Zeile danach: fertig
        write(f"{self.d}/log/syslog", f"{st(1800)} nas move: mover: started\n{st(600)} nas move: mover: finished\n")
        r = a.mover_log(f"{self.d}/log", now)
        self.assertEqual(r["state"], "finished")
        self.assertEqual(r["ended"], now - 600)
        # Neuer Start nach dem Ende: läuft wieder; «mover: stopped» zählt als Ende
        write(f"{self.d}/log/syslog", f"{st(1800)} nas move: mover: started\n{st(600)} nas move: mover: finished\n{st(300)} nas root: mover: started\n")
        self.assertEqual(a.mover_log(f"{self.d}/log", now)["state"], "running")
        write(f"{self.d}/log/syslog", f"{st(1800)} nas move: mover: started\n{st(60)} nas move: mover: stopped\n")
        self.assertEqual(a.mover_log(f"{self.d}/log", now)["state"], "finished")
        # Gross geschrieben («Move: Mover: started») zählt genauso (Review 2.64)
        write(f"{self.d}/log/syslog", f"{st(900)} nas Move: Mover: started\n")
        self.assertEqual(a.mover_log(f"{self.d}/log", now)["state"], "running")
        # Start älter als 12 Stunden ohne Ende: kein Urteil; keine Zeilen: alles None
        write(f"{self.d}/log/syslog", f"{st(13 * 3600)} nas move: mover: started\n")
        self.assertIsNone(a.mover_log(f"{self.d}/log", now)["state"])
        write(f"{self.d}/log/syslog", f"{st(100)} nas kernel: error: x\n")
        self.assertEqual(a.mover_log(f"{self.d}/log", now), {"state": None, "started": None, "ended": None})
        self.assertIn("mover", a.collect_logs(f"{self.d}/log", 24, 10, now))
        # Gleiche Zeilen in syslog und messages (gleiche Sekunde): das Ende aus syslog bleibt (Audit 1.17.0)
        write(f"{self.d}/log/syslog", f"{st(1800)} nas move: mover: started\n{st(600)} nas move: mover: finished\n")
        write(f"{self.d}/log/messages", f"{st(1800)} nas move: mover: started\n")
        self.assertEqual(a.mover_log(f"{self.d}/log", now)["state"], "finished")
        os.remove(f"{self.d}/log/messages")

    def test_grenzwert_crit_unbrauchbar_dann_max(self):
        h = FakeHost()
        write(f"{h.sys}/class/hwmon/hwmon5/temp1_crit", "0")            # Unsinn: crit 0 °C → max gilt
        self.assertEqual(a.Sampler(h.sys, h.proc).limits()["CPU"], 95.0)
        write(f"{h.sys}/class/hwmon/hwmon5/temp1_max", "255000")        # beide ausserhalb 40–150 °C → kein Wert
        self.assertNotIn("CPU", a.Sampler(h.sys, h.proc).limits())

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
        # Review 1.16.1: nach «_», JSON-Schlüssel und Basic-Anmeldung samt Wert
        s = a.redact_secrets
        self.assertEqual(s("access_token=abc123&x=1"), "access_token=<redacted>&x=1")
        self.assertEqual(s("client_secret: xyz"), "client_secret: <redacted>")
        self.assertEqual(s('{"apiKey":"abcdef1234"}'), '{"apiKey":<redacted>}')
        self.assertEqual(s("Authorization: Basic dXNlcjpwYXNz"), "Authorization: <redacted>")
        self.assertNotIn("abcdefghijkl", s("Authorization: Bearer abcdefghijkl"))
        self.assertEqual(s("monkey=banana at 10.0.0.5"), "monkey=banana at 10.0.0.5")    # IPs bleiben, kein Treffer in Wörtern
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
            self.assertEqual(srv.get("/api/info", tok="falsch")[0], 429)   # falsche Tokens bleiben gesperrt
            code, body, headers = srv.get("/api/info", tok="falsch")
            self.assertTrue(int(headers["Retry-After"]) > 0)
            self.assertEqual(srv.get("/api/info")[0], 200)                 # ab 1.18.0: richtiger Token wird nie gebremst (N5)
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


FIXTURE_APPDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fixtures", "discover", "appdata")
# Alle Geheimnisse in den Beispiel-Konfigs, die nie in einer Antwort oder im Log stehen dürfen (ausser den API-Keys selbst)
NEVER_LEAK = ("FIXTURE-POSTGRES-PASSWORT", "FIXTURE-NZB-KEY", "FIXTURE-SAB-PASSWORT", "FIXTURE-USENET-PASSWORT",
              "FIXTURE-PLEX-TOKEN-TAUTULLI", "FIXTURE-MAIL-PASSWORT", "FIXTURE-JELLYFIN-KEY-IN-SEERR",
              "FIXTURE-PLEX-ONLINE-TOKEN", "FIXTURE-NEXTCLOUD", "fixture-token-nicht-lesen", "192.168.1.2")
FIXTURE_KEYS = ("0000sonarrfixture000000000000aaa", "0000radarrfixture000000000000bbb", "0000prowlarrfixture0000000000ccc",
                "0000sonarr4kfixture0000000000ddd", "0000sabfixture000000000000000eee", "0000tautullifixture00000000ffff",
                "MDAwMG92ZXJzZWVycmZpeHR1cmUwMDA=", "MDAwMGplbGx5c2VlcnJmaXh0dXJlMDA=")


class DiscoverTest(unittest.TestCase):
    """1.16.0: /api/discover findet Dienste in appdata und liest ihre API-Keys (nur lesend, ohne Symlinks, max. Tiefe 3)."""

    def setUp(self):
        import shutil
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "appdata")
        shutil.copytree(FIXTURE_APPDATA, self.root, symlinks=True)
        # Symlinks: ganzer Ordner und einzelne Datei zeigen nach aussen → nie folgen
        outside = os.path.join(self.tmp, "aussen")
        write(f"{outside}/config.xml", "<Config><Port>7878</Port><ApiKey>0000symlinkfixture00000000000777</ApiKey>"
                                       "<InstanceName>Radarr</InstanceName></Config>")
        os.symlink(outside, os.path.join(self.root, "link-radarr"))
        os.makedirs(os.path.join(self.root, "sym-sonarr"))
        os.symlink(f"{outside}/config.xml", os.path.join(self.root, "sym-sonarr", "config.xml"))
        # zu gross (über 1 MB): nicht lesen
        write(f"{self.root}/riesen-radarr/config.xml",
              "<Config><Port>7878</Port><ApiKey>0000riesenfixture000000000000666</ApiKey>" + " " * (1024 * 1024 + 10) + "</Config>")
        # Entity-Trick (Billion Laughs / externe Datei): verweigern
        write(f"{self.root}/entity-sonarr/config.xml",
              '<?xml version="1.0"?><!DOCTYPE c [<!ENTITY x SYSTEM "file:///etc/passwd">]><Config><Port>8989</Port>'
              "<ApiKey>&x;</ApiKey></Config>")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, **kw):
        return a.Discover(roots=[self.root], **kw).scan()

    def by(self, res):
        return {(s["service"], s["folder"]): s for s in res["services"]}

    def test_findet_jede_dienstart(self):
        res = self.scan()
        d = self.by(res)
        self.assertEqual(d[("sonarr", "binhex-sonarr")]["apiKey"], "0000sonarrfixture000000000000aaa")
        self.assertEqual((d[("sonarr", "binhex-sonarr")]["port"], d[("sonarr", "binhex-sonarr")]["ssl"],
                          d[("sonarr", "binhex-sonarr")]["urlBase"]), (8989, False, ""))
        r = d[("radarr", "radarr")]                                    # Typ nur über den Ordnernamen, SSL an
        self.assertEqual((r["apiKey"], r["port"], r["ssl"], r["urlBase"], r["httpPort"]),
                         ("0000radarrfixture000000000000bbb", 9899, True, "/radarr", 7878))
        self.assertEqual(d[("prowlarr", "binhex-prowlarr")]["port"], 9696)
        s4 = d[("sonarr", "tv4k")]                                     # beliebiger Ordnername, Typ aus <InstanceName>
        self.assertEqual((s4["apiKey"], s4["port"], s4["urlBase"]), ("0000sonarr4kfixture0000000000ddd", 8990, "/4k"))
        sab = d[("sab", "sabnzbd")]                                    # Datei eine Ebene tiefer (config/)
        self.assertEqual((sab["apiKey"], sab["port"], sab["ssl"], sab["urlBase"]),
                         ("0000sabfixture000000000000000eee", 8080, False, "/sabnzbd"))
        t = d[("tautulli", "tautulli")]
        self.assertEqual((t["apiKey"], t["port"], t["ssl"], t["urlBase"]), ("0000tautullifixture00000000ffff", 8181, True, "/tautulli"))
        o = d[("seerr", "overseerr")]
        self.assertEqual((o["apiKey"], o["port"], o["variant"]), ("MDAwMG92ZXJzZWVycmZpeHR1cmUwMDA=", 5055, "overseerr"))
        self.assertEqual(d[("seerr", "jellyseerr")]["variant"], "jellyseerr")
        p = d[("plex", "plex")]                                        # Plex: nie ein Token, nur «gefunden»
        self.assertEqual((p["apiKey"], p["port"], p["code"]), (None, 32400, "plex_token"))
        j = d[("jellyfin", "jellyfin")]
        self.assertEqual((j["apiKey"], j["port"], j["urlBase"], j["code"]), (None, 8096, "/jf", "jellyfin_key"))
        e = d[("jellyfin", "emby")]
        self.assertEqual((e["apiKey"], e["port"], e["variant"]), (None, 8096, "emby"))
        for s in res["services"]:
            self.assertEqual(set(s) - {"variant", "httpPort"},
                             {"service", "folder", "port", "ssl", "urlBase", "apiKey", "code", "note", "portSource"}, s)
            self.assertNotIn("/", s["folder"])
        self.assertTrue(res["keys"])

    def test_kaputt_fehlend_tief_fremd(self):
        d = self.by(self.scan())
        k = d[("sonarr", "sonarr-kaputt")]                             # kaputte Datei: gefunden, aber ohne Schlüssel
        self.assertEqual((k["apiKey"], k["code"]), (None, "broken"))
        self.assertEqual(d[("sonarr", "entity-sonarr")]["apiKey"], None)   # DOCTYPE/ENTITY wird nicht ausgewertet
        self.assertEqual(d[("radarr", "riesen-radarr")]["code"], "unreadable")
        self.assertIsNone(d[("radarr", "riesen-radarr")]["apiKey"])
        folders = {f for _, f in d}
        for f in ("lidarr", "nextcloud", "nzblab-agent", "tiefer-sonarr"):  # fremde Dienste, eigener Ordner, zu tief
            self.assertNotIn(f, folders)
        self.assertEqual(a.Discover(roots=[os.path.join(self.tmp, "fehlt")]).scan(),
                         {"available": False, "keys": True, "services": [], "partial": False,
                          "at": unittest.mock.ANY, "ports_note": unittest.mock.ANY})

    def test_symlink_wird_nicht_verfolgt(self):
        res = self.scan()
        folders = {s["folder"] for s in res["services"]}
        self.assertNotIn("link-radarr", folders)
        self.assertNotIn("sym-sonarr", folders)
        self.assertNotIn("0000symlinkfixture00000000000777", json.dumps(res))

    def test_keine_anderen_inhalte_oder_pfade(self):
        txt = json.dumps(self.scan())
        for x in NEVER_LEAK:
            self.assertNotIn(x, txt)
        self.assertNotIn(self.tmp, txt)

    def test_auslesen_ausgeschaltet(self):
        with patch.object(a, "DISCOVER_KEYS", False):
            res = self.scan()
        self.assertFalse(res["keys"])
        self.assertGreater(len(res["services"]), 5)
        for s in res["services"]:
            self.assertIsNone(s["apiKey"])
        txt = json.dumps(res)
        for k in FIXTURE_KEYS:
            self.assertNotIn(k, txt)
        self.assertEqual(self.by(res)[("sonarr", "binhex-sonarr")]["code"], "keys_off")
        self.assertIn("ausgeschaltet", self.by(res)[("sonarr", "binhex-sonarr")]["note"])

    def server(self, **kw):
        return Server115(discover=a.Discover(roots=[self.root], **kw))

    def test_endpunkt_token_header_und_log(self):
        import contextlib
        import io
        srv = self.server()
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code, body, _ = srv.get("/api/discover", tok=None)
                self.assertEqual(code, 401)
                self.assertNotIn("services", body)
                code, body, _ = srv.get("/api/discover", tok="falsch")
                self.assertEqual(code, 401)
                code, body, hdr = srv.get("/api/discover")
                self.assertEqual(code, 200)
                self.assertIn("no-store", hdr.get("Cache-Control", ""))
                self.assertIn("0000sonarrfixture000000000000aaa", json.dumps(body))
                # Fehlerfall: Lesen bricht ab → kein Schlüssel und keine Ausnahme im Log oder in der Antwort
                with patch.object(a.Discover, "_folder", side_effect=RuntimeError("0000sonarrfixture000000000000aaa")):
                    code, body, _ = srv.get("/api/discover")
                self.assertEqual(code, 500)
                self.assertNotIn("0000sonarr", json.dumps(body))
                code, info, _ = srv.get("/api/info")
                self.assertIn("discover", info["features"])
                self.assertIn("discover_keys", info["features"])
                self.assertEqual(info["api_level"], 16)
                with patch.object(a, "DISCOVER_KEYS", False):
                    code, info, _ = srv.get("/api/info")
                self.assertIn("discover", info["features"])
                self.assertNotIn("discover_keys", info["features"])
        finally:
            srv.close()
        log = out.getvalue() + err.getvalue()
        for k in FIXTURE_KEYS + NEVER_LEAK:
            self.assertNotIn(k, log)

    def test_keys_null_ohne_schluessel(self):
        """?keys=0 (Zählen für die Startseite): gleiche Funde und Codes, aber nie ein Schlüssel in der Antwort."""
        srv = self.server()
        try:
            code, full, _ = srv.get("/api/discover")
            code2, lean, _ = srv.get("/api/discover?keys=0")
            self.assertEqual((code, code2), (200, 200))
            self.assertTrue(any(s["apiKey"] for s in full["services"]))
            self.assertEqual([(s["service"], s["folder"], s["code"], s["port"]) for s in full["services"]],
                             [(s["service"], s["folder"], s["code"], s["port"]) for s in lean["services"]])
            for s in lean["services"]:
                self.assertIsNone(s["apiKey"])
            txt = json.dumps(lean)
            for k in FIXTURE_KEYS:
                self.assertNotIn(k, txt)
            code, again, _ = srv.get("/api/discover?keys=1")
            self.assertTrue(any(s["apiKey"] for s in again["services"]))
        finally:
            srv.close()

    def test_rate_limit(self):
        srv = self.server(limit=3)
        try:
            codes = [srv.get("/api/discover")[0] for _ in range(3)]
            self.assertEqual(codes, [200, 200, 200])
            code, body, hdr = srv.get("/api/discover")
            self.assertEqual(code, 429)
            self.assertNotIn("services", body)
            self.assertTrue(int(hdr.get("Retry-After")) >= 1)
            self.assertEqual(srv.get("/api/info")[0], 200)              # andere Endpunkte bleiben frei
        finally:
            srv.close()
        lim = a.DiscoverLimiter(max_calls=10, window=60)
        waits = [lim.allow(now=100.0 + i) for i in range(11)]       # 0 = erlaubt, sonst Sekunden bis wieder frei
        self.assertEqual(waits.count(0), 10)
        self.assertGreaterEqual(waits[-1], 1)
        self.assertEqual(lim.allow(now=100.0 + 61), 0)                 # nach einer Minute wieder frei
        self.assertEqual(a.Discover().limiter.max_calls, 10)           # Standard: 10 pro Minute


class Agent1161Test(unittest.TestCase):
    """1.16.1: Korrekturen aus dem Sicherheitsaudit und den Code-Reviews zu 1.16.0 (alles mit erfundenen Testdaten)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = a.CONFIG, os.environ.pop("NZBLAB_AGENT_TOKEN", None)
        a.CONFIG = self.d

    def tearDown(self):
        import shutil
        a.CONFIG = self.old[0]
        os.environ.pop("NZBLAB_AGENT_TOKEN", None)
        if self.old[1] is not None:
            os.environ["NZBLAB_AGENT_TOKEN"] = self.old[1]
        shutil.rmtree(self.d, ignore_errors=True)

    def quiet(self, fn, *args):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = fn(*args)
        return r, out.getvalue()

    # --- Token
    def test_token_zu_kurz_oder_unbrauchbar(self):
        for bad in ("nas123", "x" * 31, "mit leerzeichen " + "y" * 30, "tab\there" + "z" * 30):
            os.environ["NZBLAB_AGENT_TOKEN"] = bad
            t, log = self.quiet(a.token)
            self.assertEqual(t, "", bad)
            self.assertIn("NZBLAB_AGENT_TOKEN", a.TOKEN_STATE["error"])
            self.assertEqual(a.TOKEN_STATE["source"], "")
            self.assertIn("FEHLER: Token nicht nutzbar", log)
            self.assertIn("mindestens 32", log)
            self.assertNotIn(bad, log)                                 # nie den Token ins Log
        os.environ["NZBLAB_AGENT_TOKEN"] = "abcdefgh" * 4             # genau 32 (8 verschiedene Zeichen): erlaubt
        self.assertEqual(self.quiet(a.token)[0], "abcdefgh" * 4)
        self.assertEqual(a.TOKEN_STATE["source"], "env")
        self.assertFalse(os.path.exists(f"{self.d}/token.txt"))        # nichts angelegt, auch bei Fehlern nicht

    def test_token_datei_zu_kurz_bleibt_unangetastet(self):
        write(f"{self.d}/token.txt", "kurz123")
        t, log = self.quiet(a.token)
        self.assertEqual(t, "")
        self.assertIn("zu kurz", a.TOKEN_STATE["error"])
        self.assertNotIn("kurz123", log)
        with open(f"{self.d}/token.txt") as f:
            self.assertEqual(f.read(), "kurz123")                      # nie überschrieben
        # Der Server antwortet dann mit 503 und dem Grund
        srv = Server115()
        srv.srv.shutdown()
        srv.srv.server_close()
        h = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, t))
        threading.Thread(target=h.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{h.server_port}/api/info", headers={"Authorization": "Bearer "})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 503)
            body = json.loads(e.exception.read())
            self.assertNotIn("token_error_detail", body)
            self.assertNotIn("zu kurz", json.dumps(body))
        finally:
            h.shutdown()
            h.server_close()

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "kein O_NOFOLLOW")
    def test_token_datei_symlink_wird_nicht_gelesen(self):
        write(f"{self.d}/echt.txt", "e" * 40)
        os.symlink(f"{self.d}/echt.txt", f"{self.d}/token.txt")
        t, log = self.quiet(a.token)
        self.assertEqual(t, "")
        self.assertIn("nicht lesbar", a.TOKEN_STATE["error"])
        self.assertTrue(os.path.islink(f"{self.d}/token.txt"))         # nicht ersetzt
        with patch.object(a.os, "open", wraps=os.open) as op:
            os.unlink(f"{self.d}/token.txt")
            write(f"{self.d}/token.txt", "ghijklmn" * 5)
            self.assertEqual(self.quiet(a.token)[0], "ghijklmn" * 5)
        flags = [c.args[1] for c in op.call_args_list if str(c.args[0]).endswith("token.txt")]
        self.assertTrue(flags and all(f & os.O_NOFOLLOW for f in flags))

    # --- Absturzberichte
    def test_absturzbericht_wird_serverseitig_geschwaerzt(self):
        cs = a.CrashStore(f"{self.d}/crashes")
        cs.add({"version": "2.54.0", "kind": "crash", "screen": "https://admin:geheim1@example.com/x?apikey=ABCDEF123456",
                "device": "Pixel password=hunter2",
                "trace": "java.io.IOException: GET https://u:pw@192.168.1.5:8989/api\nAuthorization: Bearer abcdef0123456789xyz\n"
                         "api_key=0000sonarrfixture000000000000aaa\nsecret=" + "Zz9" * 20 + "\n"
                         "at ch.nzblab.app.data.Foo.bar(Foo.kt:12)\nkey " + "A1" * 30})
        c = cs.list(1)[0]
        txt = json.dumps(c)
        for leak in ("geheim1", "ABCDEF123456", "hunter2", "pw@", "abcdef0123456789xyz", "0000sonarrfixture", "Zz9Zz9Zz9", "A1" * 30):
            self.assertNotIn(leak, txt)
        self.assertIn("<redacted>", c["trace"])
        self.assertIn("Foo.kt:12", c["trace"])                          # Fehlersuche bleibt möglich
        self.assertIn("192.168.1.5", c["trace"])                        # IPs und Pfade bleiben (nur Geheimnisse weg)
        self.assertEqual(c["version"], "2.54.0")
        # auch die Datei auf der Platte enthält nichts davon
        for n in os.listdir(cs.dir):
            with open(os.path.join(cs.dir, n)) as f:
                self.assertNotIn("hunter2", f.read())

    # --- Toppreise
    def test_toppreise_titel_linear(self):
        def page(title):
            return ('<meta property="og:url" content="https://www.toppreise.ch/preisvergleich/x-p1"><meta property="og:title" content="'
                    + title + '"><meta property="og:description" content="3 Angebote ab CHF 955.00 (Stand)">')
        good = a.parse_toppreise_product(page("SEAGATE IronWolf Pro, 28 TB (ST28000NT000)   ab CHF 955.00 bei Toppreise.ch"), 28)
        self.assertEqual((good["name"], good["price"]), ("SEAGATE IronWolf Pro, 28 TB (ST28000NT000)", 955.0))
        t0 = time.monotonic()
        self.assertIsNone(a.parse_toppreise_product(page("X" + " " * 1_000_000 + "Y"), 28))
        self.assertIsNone(a.parse_toppreise_product(page(" " * 1_000_000), 28))
        self.assertLess(time.monotonic() - t0, 2.0)
        # ein langer Titel wird auf 500 Zeichen gekappt
        got = a.parse_toppreise_product(page("28 TB " + "N" * 700 + " ab CHF 955.00"), 28)
        self.assertLessEqual(len(got["name"]), 120)

    # --- Discover
    def disc(self, files, **kw):
        import shutil
        root = os.path.join(self.d, "appdata")
        shutil.rmtree(root, ignore_errors=True)
        for rel, content in files.items():
            write(os.path.join(root, rel), content)
        return a.Discover(roots=[root], **kw)

    def test_tautulli_nur_mit_pms_oder_namen(self):
        gen = "[General]\nhttp_port = 8181\nhttp_root = /x\napi_key = 0000lookalike0000000000000aaaa\n"
        d = self.disc({"headphones/config.ini": gen, "lazylibrarian/config.ini": gen, "mylar3/config.ini": gen,
                       "irgendwas/config.ini": gen,
                       "tt/config.ini": gen + "[PMS]\npms_ip = x\n",
                       "tautulli-alt/config.ini": gen,
                       "headphones-mit-pms/config.ini": gen + "[PMS]\n"})
        got = {(s["service"], s["folder"]) for s in d.scan()["services"]}
        self.assertEqual(got, {("tautulli", "tt"), ("tautulli", "tautulli-alt")})
        # Ordnername «headphones» auch mit [PMS]: ausgeschlossen (kein Tautulli-Ordnername)
        self.assertNotIn("headphones-mit-pms", {f for _, f in got})

    def test_partial_nur_bei_abgeschnittenem_dienstordner(self):
        key = "<Config><Port>8989</Port><ApiKey>0000sonarrfixture000000000000aaa</ApiKey><InstanceName>Sonarr</InstanceName></Config>"
        files = {f"nextcloud/data/f{i}.txt": "x" for i in range(30)}
        files.update({f"gross-sonarr/zz{i}.txt": "x" for i in range(30)})
        with patch.object(a, "DISCOVER_MAX_ENTRIES", 10):
            res = self.disc(files).scan()
        self.assertEqual(res["services"], [])
        self.assertTrue(res["partial"])                                 # «gross-sonarr» sieht nach Dienst aus und wurde abgeschnitten
        # Nur ein fremder Ordner (nextcloud) abgeschnitten: kein partial
        with patch.object(a, "DISCOVER_MAX_ENTRIES", 10):
            res = self.disc({f"nextcloud/data/f{i}.txt": "x" for i in range(30)}, cache_s=0).scan()
        self.assertFalse(res["partial"])
        # Gefundener Dienst, Rest des Ordners gross: kein partial
        one = {"sonarr/config.xml": key}
        one.update({f"sonarr/gross/zz{i}": "x" for i in range(200)})
        with patch.object(a, "DISCOVER_MAX_ENTRIES", 50):
            res = self.disc(one).scan()
        self.assertFalse(res["partial"])
        self.assertEqual([s["code"] for s in res["services"]], ["ok"])

    def test_datenbank_ordner_werden_nicht_durchsucht(self):
        files = {"postgres14/PG_VERSION": "14", "postgres14/pgdata/config.xml": "<Config><Port>8989</Port></Config>",
                 "maria/mysql/x": "x", "maria/data/config.xml": "<Config/>",
                 "immich/model-cache/config.xml": "<Config/>"}
        files.update({f"postgres14/base/{i}/f": "x" for i in range(40)})
        with patch.object(a, "DISCOVER_MAX_ENTRIES", 10):
            res = self.disc(files).scan()
        self.assertEqual(res["services"], [])
        self.assertFalse(res["partial"])

    def test_ordner_nicht_lesbar(self):
        d = self.disc({"sonarr/config/config.xml": "<Config/>", "radarr/config.xml": "<Config/>", "tautulli/config.ini": "x",
                       "fremd/config.xml": "<Config/>", "seerr-x/settings.json": "{}", "plex-meta-manager/a": "x"})
        real = os.scandir

        def deny(path="."):
            if os.path.basename(str(path)) in ("sonarr", "tautulli", "seerr-x", "plex-meta-manager", "fremd"):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)
        with patch.object(a.os, "scandir", deny):
            res = d.scan()
        by = {(s["service"], s["folder"]): s for s in res["services"]}
        for k in (("sonarr", "sonarr"), ("tautulli", "tautulli"), ("seerr", "seerr-x")):
            self.assertEqual(by[k]["code"], "unreadable", k)
            self.assertIsNone(by[k]["apiKey"])
            self.assertIn("PUID=0", by[k]["note"])
        self.assertNotIn("fremd", {f for _, f in by})                   # unbekannter Name: nichts erfunden
        self.assertNotIn("plex-meta-manager", {f for _, f in by})       # Zusatzwerkzeug, kein Plex
        self.assertEqual(by[("radarr", "radarr")]["code"], "broken")    # lesbar, aber ohne Schlüssel
        self.assertNotIn(self.d, json.dumps(res))

    def test_zeitbudget(self):
        files = {f"sonarr{i:02d}/config.xml": "<Config><Port>8989</Port><ApiKey>0000sonarrfixture000000000000aaa</ApiKey></Config>"
                 for i in range(20)}
        tick = {"t": 0.0}

        def clock():
            tick["t"] += 1.0
            return tick["t"]
        res = self.disc(files, budget=8.0, clock=clock).scan()
        self.assertTrue(res["partial"])
        self.assertTrue(0 < len(res["services"]) < 20)
        self.assertEqual(a.DISCOVER_BUDGET_S, 8.0)
        full = self.disc(files, budget=100.0).scan()                    # genug Zeit: vollständig
        self.assertFalse(full["partial"])
        self.assertEqual(len(full["services"]), 20)

    def test_ergebnis_ohne_schluessel_wird_60_s_gemerkt(self):
        d = self.disc({"sonarr/config.xml": "<Config><Port>8989</Port><ApiKey>0000sonarrfixture000000000000aaa</ApiKey></Config>"})
        first = d.scan(with_keys=False)
        write(os.path.join(self.d, "appdata", "radarr", "config.xml"), "<Config><Port>7878</Port><ApiKey>0000radarrfixture000000000000bbb</ApiKey></Config>")
        self.assertEqual(d.scan(with_keys=False)["services"], first["services"])    # aus dem Zwischenspeicher
        self.assertEqual(len(d.scan(with_keys=True)["services"]), 2)               # mit Schlüsseln immer frisch
        self.assertEqual(len(a.Discover(roots=d.explicit).scan(with_keys=False)["services"]), 2)
        self.assertEqual(a.DISCOVER_CACHE_S, 60)

    def test_keys_parameter_und_wirksamer_zustand(self):
        d = self.disc({"sonarr/config.xml": "<Config><Port>8989</Port><ApiKey>0000sonarrfixture000000000000aaa</ApiKey></Config>"})
        srv = Server115(discover=d)
        try:
            for off in ("0", "false", "no", "off", "FALSE", "Nein"):
                code, body, _ = srv.get(f"/api/discover?keys={off}")
                self.assertEqual(code, 200)
                self.assertFalse(body["keys"], off)
                self.assertNotIn("0000sonarr", json.dumps(body))
            code, body, _ = srv.get("/api/discover?keys=1")
            self.assertTrue(body["keys"])
            self.assertIn("0000sonarrfixture000000000000aaa", json.dumps(body))
            code, body, _ = srv.get("/api/discover")
            self.assertTrue(body["keys"])
            with patch.object(a, "DISCOVER_KEYS", False):
                code, body, _ = srv.get("/api/discover")
            self.assertFalse(body["keys"])
        finally:
            srv.close()

    def test_emby_und_altes_jellyfin_port_aus_system_xml(self):
        d = self.disc({"emby/config/system.xml": "<ServerConfiguration><HttpServerPortNumber>8920</HttpServerPortNumber></ServerConfiguration>",
                       "emby2/config/system.xml": "<ServerConfiguration/>",
                       "jellyfin-alt/config/system.xml": "<ServerConfiguration><HttpServerPortNumber>8097</HttpServerPortNumber></ServerConfiguration>"})
        by = {s["folder"]: s for s in d.scan()["services"]}
        self.assertEqual((by["emby"]["port"], by["emby"]["variant"], by["emby"]["portSource"]), (8920, "emby", "config"))
        self.assertEqual((by["emby2"]["port"], by["emby2"]["portSource"]), (8096, "default"))
        self.assertEqual((by["jellyfin-alt"]["port"], by["jellyfin-alt"]["variant"], by["jellyfin-alt"]["code"]),
                         (8097, "jellyfin", "jellyfin_key"))
        self.assertIsNone(by["emby"]["apiKey"])


class Haertung1161Test(unittest.TestCase):
    """1.16.1 Härtung: Sicherheits-Kopfzeilen, CSP mit Skript-Hash, /api/hello-Bremse, einfache Tokens, Cookie-Schwärzung,
    Verbindungsgrenze je Absender (alles lokal mit erfundenen Daten)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = a.CONFIG, os.environ.pop("NZBLAB_AGENT_TOKEN", None)
        a.CONFIG = self.d
        a.AGENT_ID["v"] = None

    def tearDown(self):
        import shutil
        a.CONFIG = self.old[0]
        a.AGENT_ID["v"] = None
        os.environ.pop("NZBLAB_AGENT_TOKEN", None)
        if self.old[1] is not None:
            os.environ["NZBLAB_AGENT_TOKEN"] = self.old[1]
        shutil.rmtree(self.d, ignore_errors=True)
    def test_json_kopfzeilen(self):
        srv = Server115()
        try:
            for path, tok in (("/api/ping", None), ("/api/info", "geheim"), ("/api/now", "falsch")):
                code, _, h = srv.get(path, tok=tok)
                self.assertEqual(h["Cache-Control"], "no-store", path)
                self.assertEqual(h["X-Content-Type-Options"], "nosniff", path)
                self.assertEqual(h["X-Frame-Options"], "DENY", path)
                self.assertEqual(h["Referrer-Policy"], "no-referrer", path)
                self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"], path)
                self.assertNotIn("Python", h["Server"], path)
                self.assertEqual(h.get_all("Cache-Control"), ["no-store"], path)
            code, _, h = srv.get("/api/discover")                       # eigener no-store: nur einmal gesendet
            self.assertEqual(h.get_all("Cache-Control"), ["no-store"])
        finally:
            srv.close()

    def test_csp_mit_skript_hash_statt_unsafe_inline(self):
        import base64, hashlib, re
        srv = Server115()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/") as r:
                csp, html, xfo = r.headers["Content-Security-Policy"], r.read().decode(), r.headers["X-Frame-Options"]
        finally:
            srv.close()
        self.assertEqual(xfo, "DENY")
        script_src = re.search(r"script-src ([^;]+);", csp).group(1)
        self.assertNotIn("unsafe-inline", script_src)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertEqual(len(scripts), 1)
        want = "'sha256-" + base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode() + "'"
        self.assertEqual(script_src.strip(), want)
        self.assertNotRegex(html, r"<[^>]+\son[a-z]+\s*=")              # keine Inline-Handler (würde die CSP blockieren)
        self.assertNotIn("javascript:", html.replace('kein «javascript:»', ""))
        for part in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'none'"):
            self.assertIn(part, csp)

    def test_csp_hash_folgt_der_datei_und_crlf(self):
        p = os.path.join(self.d, "w.html")
        write(p, "<p>x</p><script>let a = 1;\r\nlet b = 2;</script>")
        body, csp = a.web_page(p)
        import base64, hashlib
        h = base64.b64encode(hashlib.sha256(b"let a = 1;\nlet b = 2;").digest()).decode()
        self.assertIn(f"'sha256-{h}'", csp)
        write(p, "<p>ohne Skript</p>")
        os.utime(p, ns=(1, 2))                                          # andere Zeit: neu lesen
        body, csp = a.web_page(p)
        self.assertIn("script-src 'none'", csp)
        self.assertIn(b"ohne Skript", body)

    def test_hello_bremse(self):
        srv = Server115(hello_limiter=a.HelloLimiter(max_calls=3))
        try:
            codes = [srv.get("/api/hello?nonce=" + "0" * 32, tok=None)[0] for _ in range(4)]
            self.assertEqual(codes, [200, 200, 200, 429])
            code, body, h = srv.get("/api/hello?nonce=" + "0" * 32, tok=None)
            self.assertEqual(code, 429)
            self.assertGreaterEqual(int(h["Retry-After"]), 1)
            self.assertNotIn("proof", body)
        finally:
            srv.close()
        lim = a.HelloLimiter(max_calls=2, window=60)
        self.assertEqual([lim.allow("x", now=0), lim.allow("x", now=1), lim.allow("y", now=1)], [0, 0, 0])
        self.assertGreater(lim.allow("x", now=2), 0)
        self.assertEqual(lim.allow("x", now=61), 0)                     # nach einer Minute wieder frei
        # Review 1.16.1: bei sehr vielen Absendern höchstens einmal pro Sekunde aufräumen (nicht bei jeder Anfrage)
        lim = a.HelloLimiter(max_calls=2, window=60)
        lim.calls = {f"ip{i}": [100.0] for i in range(5001)}
        lim.allow("neu", now=100.5)
        self.assertEqual(lim._pruned, 100.5)
        lim.calls["alt"] = [0.0]                                        # abgelaufen, aber erst nach einer Sekunde weg
        lim.allow("neu2", now=101.0)
        self.assertIn("alt", lim.calls)
        lim.allow("neu3", now=101.6)
        self.assertNotIn("alt", lim.calls)

    def test_sicherheits_kopfzeilen_auch_bei_send_error(self):
        srv = Server115()
        try:
            for method in ("HEAD", "PUT"):
                req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/ping", method=method)
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=5)
                h = cm.exception.headers
                self.assertEqual(cm.exception.code, 501)
                self.assertEqual(h["Cache-Control"], "no-store")
                self.assertEqual(h["X-Content-Type-Options"], "nosniff")
                self.assertEqual(h["X-Frame-Options"], "DENY")
                self.assertIn("default-src 'none'", h["Content-Security-Policy"])
                self.assertEqual(h["Server"], "NASlab-Agent")
                cm.exception.close()
            # Browser-Seite: eigene CSP und Cache-Control, keine doppelten Kopfzeilen
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/", timeout=5) as r:
                self.assertEqual(len(r.headers.get_all("Content-Security-Policy")), 1)
                self.assertEqual(r.headers.get_all("Cache-Control"), ["no-cache"])
                self.assertIn("script-src 'sha256-", r.headers["Content-Security-Policy"])
        finally:
            srv.close()

    def test_einfacher_token_abgelehnt(self):
        import contextlib, io
        for bad in ("ab" * 16, "1234" * 8, "a" * 40):
            os.environ["NZBLAB_AGENT_TOKEN"] = bad
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(a.token(), "", bad)
            self.assertIn("zu einfach", a.TOKEN_STATE["error"])
            self.assertNotIn(bad, out.getvalue())
        os.environ.pop("NZBLAB_AGENT_TOKEN")
        write(f"{self.d}/token.txt", "xy" * 20)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(a.token(), "")
        self.assertIn("zu einfach", a.TOKEN_STATE["error"])
        with open(f"{self.d}/token.txt") as f:
            self.assertEqual(f.read(), "xy" * 20)                       # nie überschrieben
        # Erzeugte Standard-Tokens bestehen die Prüfung immer
        for _ in range(200):
            self.assertIsNone(a._token_weak(a.secrets.token_urlsafe(24)))

    def test_verbindungsgrenze_je_absender(self):
        import socket
        with patch.object(a, "MAX_CONN_PER_IP", 3):
            s = a.make_server("127.0.0.1", 0, a.make_handler(a.Store(":memory:"), lambda: {}, "geheim"))
            threading.Thread(target=s.serve_forever, daemon=True).start()
            port = s.server_address[1]
            held = []
            try:
                for _ in range(3):                                      # drei halbe Anfragen halten je einen Faden
                    c = socket.create_connection(("127.0.0.1", port), timeout=5)
                    c.sendall(b"GET /api/ping HTTP/1.1\r\n")
                    held.append(c)
                time.sleep(0.3)
                extra = socket.create_connection(("127.0.0.1", port), timeout=5)
                extra.sendall(b"GET /api/ping HTTP/1.0\r\n\r\n")
                self.assertEqual(extra.recv(100), b"")                  # sofort geschlossen
                extra.close()
                held.pop().close()
                time.sleep(0.3)
                ok = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=5))
                self.assertTrue(ok["ok"])
            finally:
                for c in held:
                    c.close()
                s.shutdown()
                s.server_close()


class AuditLauf2Test(unittest.TestCase):
    """1.16.1 Befunde aus Audit Lauf 2 (needs_validation, vorsorglich behoben): Sammler und Absturzberichte."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = a.CONFIG, os.environ.pop("NZBLAB_AGENT_TOKEN", None)
        a.CONFIG = self.d
        a.AGENT_ID["v"] = None

    def tearDown(self):
        import shutil
        a.CONFIG = self.old[0]
        a.AGENT_ID["v"] = None
        os.environ.pop("NZBLAB_AGENT_TOKEN", None)
        if self.old[1] is not None:
            os.environ["NZBLAB_AGENT_TOKEN"] = self.old[1]
        shutil.rmtree(self.d, ignore_errors=True)
    def test_cookie_zeile_ganz_geschwaerzt(self):
        out = a.redact_secrets("Cookie: sid=AAAA; csrftoken=BBBB; theme=dark\nnaechste Zeile")
        self.assertNotIn("AAAA", out)
        self.assertNotIn("BBBB", out)
        self.assertIn("naechste Zeile", out)
        self.assertNotIn("CCCC", a.redact_secrets("set-cookie: x=CCCC; Path=/"))
        self.assertNotIn("DDDD", a.redact_log("Cookie: a=DDDD; b=EEEE"))

    def test_absturzbericht_alle_felder_geschwaerzt_und_kurz(self):
        st = a.CrashStore(os.path.join(self.d, "crashes"))
        st.add({"trace": "boom", "kind": "crash token=GEHEIM123", "version": "v" * 5000, "android": "Bearer abcdefghijkl",
                "time": "2026-11-08", "device": "Cookie: sid=XYZ; b=QQQ"})
        r = st.list(1)[0]
        dump = json.dumps(r)
        for secret in ("GEHEIM123", "abcdefghijkl", "XYZ", "QQQ"):
            self.assertNotIn(secret, dump)
        self.assertEqual(len(r["version"]), 200)
        self.assertEqual(r["trace"], "boom")

    def test_readme_in_freigabe_begrenzt_und_kaputtes_utf8(self):
        d = os.path.join(self.d, "share", "backup")
        os.makedirs(d)
        with open(os.path.join(d, "README"), "wb") as f:
            f.write(b"\xff\xfe" * 10 + b"x" * 100)
        self.assertIsNone(a.detect_repo(d))                              # kein UnicodeDecodeError mehr
        with open(os.path.join(d, "README"), "wb") as f:
            f.write(b"This is a Borg Backup repository.\n")
            f.truncate(256 * 1024 * 1024)                               # dünn besetzt, 256 MiB
        with patch.object(a, "read", side_effect=AssertionError("ganze Datei gelesen")):
            self.assertEqual(a.detect_repo(d), "borg")
        bad = os.path.join(self.d, "kaputt.txt")
        with open(bad, "wb") as f:
            f.write(b"\xff\xff")
        self.assertEqual(a.read(bad, "std"), "std")

    def test_email_regel_linear(self):
        t0 = time.monotonic()
        a.redact_log("error " + "a" * 200000)
        a.redact_log("error " + "a." * 100000)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(a.redact_log("mail an max.muster@example.com ging"), "mail an <email> ging")

    def test_lan_flut_neuer_macs_begrenzt(self):
        lan = a.LanScan(os.path.join(self.d, "dev.db"), lambda *x: None)
        msgs = []
        lan.notify = lambda t, x: msgs.append(t)
        rounds = iter([{"aa:00:00:00:00:01": "192.168.1.2"}] * 2 +
                      [{f"02:00:00:00:{i // 256:02x}:{i % 256:02x}": f"192.168.1.{i % 250 + 2}" for i in range(300)}] * 2)
        with patch.object(lan, "available", return_value=True), patch.object(lan, "arp", side_effect=lambda: next(rounds)), \
                patch.object(a.time, "sleep"), patch.object(lan, "lookup", return_value=""), patch.object(a.socket, "socket"):
            lan.scan()                                                  # erster Durchgang: ein Gerät, keine Meldung
            lan.scan()                                                  # 300 gefälschte MACs
        self.assertEqual(len(lan.devices()), 1 + lan.MAX_NEW)
        self.assertEqual(msgs.count("Neues Gerät im Heimnetz"), lan.MAX_ALERTS)
        self.assertEqual(msgs.count("Viele neue Geräte im Heimnetz"), 1)

    def test_lan_erster_durchgang_ohne_grenze_von_64(self):
        # Review 1.16.1: Netz mit 100 Geräten. Der erste Durchgang merkt sich alle, der zweite meldet nichts als «neu»
        lan = a.LanScan(os.path.join(self.d, "dev2.db"), lambda *x: None)
        msgs = []
        lan.notify = lambda t, x: msgs.append(t)
        net = {f"02:00:00:00:00:{i:02x}": f"192.168.1.{i + 2}" for i in range(100)}
        rounds = iter([net] * 4)
        with patch.object(lan, "available", return_value=True), patch.object(lan, "arp", side_effect=lambda: next(rounds)), \
                patch.object(a.time, "sleep"), patch.object(lan, "lookup", return_value=""), patch.object(a.socket, "socket"):
            lan.scan()
            lan.scan()
        self.assertEqual(len(lan.devices()), 100)
        self.assertEqual(msgs, [])



class AuditRestteileTest(unittest.TestCase):
    """1.16.1 Audit Restteile: WireGuard, Duplikate, Auslöser ohne Sperre, Preis-Abrufe (Weiterleitungen, Grösse, Links)."""
    PRIV = "cPrIvAtEkEyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    PSK = "pReShArEdAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    PUB1 = "HaNdYpUbLiCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def server(self, **kw):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(a.Store(":memory:"), lambda: {}, "geheim", **kw))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    @staticmethod
    def post(base, path, token="geheim"):
        req = urllib.request.Request(base + path, data=b"", method="POST",
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    # --- WireGuard

    def test_wg_kommentar_mit_schluessel_wird_kein_name(self):
        dump = "\t".join(["wg0", self.PUB1, self.PSK, "(none)", "10.8.0.2/32", "0", "0", "0", "off"]) + "\n"
        write(f"{self.d}/wg0.conf", f"[Interface]\nPrivateKey={self.PRIV}\n[Peer]\n#PresharedKey={self.PSK}\n"
                                    f"PublicKey={self.PUB1}\n")
        write(f"{self.d}/wg1.conf", f"#alt {self.PRIV}\n[Peer]\nPublicKey={self.PUB1}\n")
        write(f"{self.d}/wg0.cfg", f'PUBLIC:1="{self.PUB1}"\nNAME:1="{self.PRIV}"\nPRIVATE:1="{self.PRIV}"\n')
        r = a.wireguard_status(run=lambda: (0, dump), conf_dir=self.d, now=1)
        text = json.dumps(r)
        for secret in (self.PRIV, self.PSK, self.PRIV[:20], self.PSK[:20]):
            self.assertNotIn(secret, text)
        self.assertIsNone(r["interfaces"][0]["peers"][0]["name"])

    def test_wg_gewoehnliche_namen_bleiben(self):
        # Review 1.16.1: «privat…» oder «Upskill» sind keine Schlüssel; nur «Private Key», «Preshared», «PSK», Base64-Folgen
        for name in ("Mein privater Laptop", "Private Handy", "Upskill-PC", "Handy Anna"):
            self.assertEqual(a._wg_label(name), name, name)
        for name in ("PrivateKey = x", "private_key", "PSK Handy", f"alt {self.PRIV}"):
            self.assertIsNone(a._wg_label(name), name)

    def test_wg_konfiguration_begrenzt_gelesen(self):
        with open(f"{self.d}/gross.conf", "wb") as f:
            f.write(b"[Peer]\n#Handy\nPublicKey=" + self.PUB1.encode() + b"\n")
            f.truncate(64 * 1024 * 1024)                                   # dünn besetzt, 64 MiB
        self.assertEqual(len(a._wg_read(f"{self.d}/gross.conf")), a.WG_CONF_MAX)
        self.assertEqual(a.wg_names(self.d), {self.PUB1: "Handy"})

    def test_wg_fehler_ohne_ausgabe_im_text(self):
        class Boom(str):
            def splitlines(self):
                raise ValueError(f"kaputt {a_priv}")
        a_priv = self.PRIV
        r = a.wireguard_status(run=lambda: (0, Boom("x")), conf_dir=self.d)
        self.assertEqual((r["available"], r["reason"]), (False, "error"))
        self.assertNotIn(self.PRIV, json.dumps(r))

    # --- Duplikate

    def test_pruefsumme_folgt_keinem_symlink_und_keiner_pipe(self):
        root = os.path.join(self.d, "root")
        os.makedirs(root)
        secret = os.path.join(self.d, "token.txt")
        write(secret, "GEHEIM" * 10)
        write(os.path.join(root, "echt.bin"), "GEHEIM" * 10)
        os.symlink(secret, os.path.join(root, "link.bin"))
        with self.assertRaises(OSError):
            a.partial_hash(os.path.join(root, "link.bin"), 60, root)
        with self.assertRaises(OSError):
            a.partial_hash(secret, 60, root)                              # ausserhalb der Freigaben
        os.mkfifo(os.path.join(root, "pipe.bin"))
        t0 = time.monotonic()
        with self.assertRaises(OSError):
            a.partial_hash(os.path.join(root, "pipe.bin"), 60, root)      # vorher: open() hing für immer
        self.assertLess(time.monotonic() - t0, 2)
        self.assertTrue(a.partial_hash(os.path.join(root, "echt.bin"), 60, root))
        # Über den Index: Symlink und Pipe gelten als nicht lesbar («!»), keine Gruppe mit der echten Datei
        ix = a.FileIndex(os.path.join(self.d, "i.db"), root=root, exclude=set())
        ix.build()
        with ix.lock:
            rows = ix.db.execute("SELECT p, size, mtime FROM files WHERE dir=0").fetchall()
        self.assertEqual({r[0] for r in rows}, {"echt.bin", "link.bin", "pipe.bin"})
        ix._hash_run(rows)
        with ix.lock:
            hs = dict(ix.db.execute("SELECT p, h FROM hashes").fetchall())
        self.assertEqual((hs["link.bin"], hs["pipe.bin"]), ("!", "!"))
        self.assertNotEqual(hs["echt.bin"], "!")
        self.assertIsNone(ix.hash_error)

    # --- Auslöser

    def test_speedtest_nur_einmal_gleichzeitig(self):
        base = self.server()
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(5)
            return {"t": 1, "ping_ms": 1, "down_mbit": 1.0, "up_mbit": 1.0}
        res = {}
        with patch.object(a, "speedtest", side_effect=slow):
            t = threading.Thread(target=lambda: res.update(first=self.post(base, "/api/speedtest")))
            t.start()
            self.assertTrue(started.wait(5))
            code, body = self.post(base, "/api/speedtest")
            release.set()
            t.join(10)
        self.assertEqual(code, 409)
        self.assertIn("läuft schon", body["error"])
        self.assertEqual(res["first"][0], 200)
        self.assertEqual(self.post(base, "/api/speedtest", token=None)[0], 401)
        self.assertFalse(a.SPEED_LOCK.locked())

    def test_index_und_geraetesuche_nie_doppelt(self):
        root = os.path.join(self.d, "root")
        os.makedirs(root)
        ix = a.FileIndex(os.path.join(self.d, "i.db"), root=root, exclude=set())
        calls, gate = [], threading.Event()
        with patch.object(ix, "_build", side_effect=lambda: (calls.append(1), gate.wait(5))):
            ts = [threading.Thread(target=ix.build) for _ in range(5)]
            for t in ts:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in ts:
                t.join(5)
        self.assertEqual(len(calls), 1)
        lan = a.LanScan(os.path.join(self.d, "dev.db"), lambda *x: None)
        scans, gate2 = [], threading.Event()
        with patch.object(lan, "available", return_value=True), \
                patch.object(lan, "_scan", side_effect=lambda: (scans.append(1), gate2.wait(5))):
            ts = [threading.Thread(target=lan.scan) for _ in range(5)]
            for t in ts:
                t.start()
            time.sleep(0.2)
            gate2.set()
            for t in ts:
                t.join(5)
        self.assertEqual(len(scans), 1)
        # HTTP: läuft schon → 202 mit started=false (die App zeigt weiter «Suche gestartet»)
        lan.running = True
        ix.state["running"] = True
        base = self.server(index=ix, lan=lan)
        self.assertEqual(self.post(base, "/api/devices/scan"), (202, {"started": False, "running": True}))
        self.assertEqual(self.post(base, "/api/files/refresh"), (202, {"started": False, "running": True}))
        self.assertEqual(self.post(base, "/api/files/refresh", token="falsch")[0], 401)

    # --- Preise

    def test_nur_oeffentliche_https_adressen(self):
        pub = lambda h: ["93.184.216.34"]                                   # noqa: E731
        self.assertTrue(a.public_https_url("https://www.toppreise.ch/x", resolve=pub))
        self.assertFalse(a.public_https_url("http://www.toppreise.ch/x", resolve=pub))
        self.assertFalse(a.public_https_url("https://" + "user:pw" + "@" + "www.toppreise.ch/x", resolve=pub))
        for ip in ("192.168.1.1", "127.0.0.1", "10.0.0.5", "169.254.169.254", "100.64.0.1", "::1", "fe80::1", "::ffff:192.168.1.1", "0.0.0.0",
                   "64:ff9b::c0a8:101", "64:ff9b::7f00:1"):                     # NAT64 mit privater IPv4 (Review)
            self.assertFalse(a.public_host("x", resolve=lambda h, ip=ip: [ip]), ip)
        self.assertFalse(a.public_host("x", resolve=lambda h: ["93.184.216.34", "192.168.1.2"]))   # eine private reicht
        self.assertTrue(a.public_host("x", resolve=lambda h: ["64:ff9b::5db8:d822"]))           # NAT64 auf öffentliche IPv4
        self.assertFalse(a.public_host("x", resolve=lambda h: (_ for _ in ()).throw(OSError("dns"))))  # DNS-Fehler: zu
        self.assertFalse(a.public_host("localhost"))
        with self.assertRaises(urllib.error.URLError):
            a.http_get("http://127.0.0.1:1/robots.txt")
        with self.assertRaises(urllib.error.URLError):
            a._PublicRedirect().redirect_request(urllib.request.Request("https://www.toppreise.ch/"), None, 302, "Found", {},
                                                 "http://192.168.1.1/admin")

    def test_weiterleitung_ins_heimnetz_blockiert(self):
        from http.server import BaseHTTPRequestHandler
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *x):
                pass

            def do_GET(self):
                hits.append(self.path)
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/intern")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"intern")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/start"
            with patch.object(a, "public_https_url", side_effect=lambda u, resolve=None: u.endswith("/start")):
                with self.assertRaises(urllib.error.URLError):
                    a.http_get(url)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(hits, ["/start"])

    def test_seite_begrenzt_und_mit_frist(self):
        import io
        self.assertEqual(len(a._read_capped(io.BytesIO(b"x" * 5_000_000), 4_000_000, time.monotonic() + 10)), 4_000_000)

        class Slow:
            def read(self, n):
                return b"x"
        with self.assertRaises(TimeoutError):
            a._read_capped(Slow(), 4_000_000, time.monotonic() - 1)

    def test_links_aus_fremden_seiten_nur_https(self):
        self.assertEqual(a.safe_link("javascript:alert(1)", "https://f"), "https://f")
        self.assertEqual(a.safe_link("intent://x#Intent;end", "https://f"), "https://f")
        self.assertEqual(a.safe_link("https://geizhals.de/a.html", "https://f"), "https://geizhals.de/a.html")
        html = ('<article class="galleryview__item"><a class="galleryview__name-link" title="WD Red Plus 8TB" '
                'href="javascript:alert(1)">x</a><span class="galleryview__price-link">€ 200,00</span></article>')
        self.assertEqual(a.parse_geizhals(html, 8)[0]["link"], "https://geizhals.de")
        page = ('<meta property="og:url" content="javascript:/preisvergleich/x">'
                '<meta property="og:title" content="Seagate IronWolf 8 TB ab CHF 200.00 bei Toppreise.ch">')
        self.assertIsNone(a.parse_toppreise_product(page, 8))


class Agent118Test(unittest.TestCase):
    """1.18.0 (Audit 12.11.): M1 chown nur im eigenen /config, M2 Syslog in «Heute», M3 Heimnetz-Suche aus, N2, N5."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.old = a.CONFIG, os.environ.get("PUID"), os.environ.get("PGID")
        a.CONFIG = self.d
        os.environ["PUID"], os.environ["PGID"] = "65534", "65534"

    def tearDown(self):
        import shutil
        a.CONFIG = self.old[0]
        for k, v in zip(("PUID", "PGID"), self.old[1:]):
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        a.PRIV_STATE["root_reason"] = ""
        shutil.rmtree(self.d, ignore_errors=True)

    def _drop(self):
        """drop_privileges ohne echten Benutzerwechsel (Rückgabe, Besitzer der Dateien prüft der Test)."""
        with patch.object(a.os, "setgroups"), patch.object(a.os, "setgid"), patch.object(a.os, "setuid"):
            return a.drop_privileges()

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "nur als root prüfbar")
    def test_m1_fremde_dateien_bleiben_unberuehrt(self):
        write(f"{self.d}/postgres/data/PG_VERSION", "16")
        write(f"{self.d}/plex/Preferences.xml", "x")
        write(f"{self.d}/token.txt", "t")
        write(f"{self.d}/crashes/1.json", "{}")
        os.chmod(self.d, 0o777)
        for p in (f"{self.d}/postgres", f"{self.d}/postgres/data", f"{self.d}/postgres/data/PG_VERSION", f"{self.d}/plex",
                  f"{self.d}/plex/Preferences.xml"):
            os.lchown(p, 999, 999)
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            who = self._drop()
        self.assertEqual(who, (65534, 65534))
        for p in ("postgres", "postgres/data", "postgres/data/PG_VERSION", "plex", "plex/Preferences.xml"):
            self.assertEqual(os.lstat(f"{self.d}/{p}").st_uid, 999, p)          # fremde Dateien unverändert
        for p in ("token.txt", "crashes", "crashes/1.json"):
            self.assertEqual(os.lstat(f"{self.d}/{p}").st_uid, 65534, p)        # eigene gehören dem Agenten
        self.assertIn("WARNUNG", out.getvalue())
        self.assertFalse(os.path.exists(f"{self.d}/{a.CONFIG_MARKER}"))         # fremder Ordner wird nicht als «eigen» markiert

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "nur als root prüfbar")
    def test_m1_eigener_und_leerer_ordner_wird_uebernommen(self):
        write(f"{self.d}/token.txt", "t")
        write(f"{self.d}/history.db-wal", "x")
        write(f"{self.d}/prices-CH.json", "{}")
        self.assertEqual(a.config_is_own(self.d), (True, []))
        self.assertEqual(self._drop(), (65534, 65534))
        for p in ("token.txt", "history.db-wal", "prices-CH.json", a.CONFIG_MARKER):
            self.assertEqual(os.lstat(f"{self.d}/{p}").st_uid, 65534, p)
        # Nach-Audit: die Markierung allein macht den Ordner nicht «eigen»; liegen später fremde Namen darin, gilt er als fremd
        write(f"{self.d}/unterordner/x", "x")
        self.assertEqual(a.config_is_own(self.d), (False, ["unterordner"]))
        empty = tempfile.mkdtemp()
        self.assertEqual(a.config_is_own(empty), (True, []))
        os.rmdir(empty)

    def test_nachaudit_markierung_nicht_faelschbar(self):
        # /config war beim ersten Start leer (z. B. ganzes appdata) und bekam die Markierung; später fremde Container-Daten
        write(f"{self.d}/{a.CONFIG_MARKER}", "x")
        write(f"{self.d}/postgres/data/PG_VERSION", "16")
        self.assertEqual(a.config_is_own(self.d), (False, ["postgres"]))

    def test_nachaudit_sshd_benutzer_geschwaerzt(self):
        for line, name in (("Failed password for bob from 10.0.0.5 port 22 ssh2", "bob"),
                           ("Accepted publickey for alice from 10.0.0.5 port 22", "alice"),
                           ("Failed password for invalid user mallory from 10.0.0.5", "mallory")):
            out = a.redact_log(line)
            self.assertNotIn(name, out, out)
            self.assertNotIn("10.0.0.5", out)

    def test_m2_vm_befund_benutzer_in_pam_zeilen(self):
        r = a.redact_log
        for line, name in (("PAM: authentication failure for bob from 10.1.2.3", "bob"),
                           ("pam_unix(sshd:auth): authentication failure; logname=carl uid=0 euid=0 tty=ssh ruser=dora rhost=host.example.org", "carl"),
                           ("pam_unix(sshd:auth): authentication failure; logname= uid=0 tty=ssh ruser=dora rhost=10.1.2.3 user=bob", "dora"),
                           ("Failed password for bob from 10.1.2.3 port 22 ssh2", "bob"),
                           ("Accepted password for bob from 10.1.2.3 port 22 ssh2", "bob"),
                           ("pam_unix(sudo:session): session opened for user bob(uid=1000) by (uid=0)", "bob"),
                           ("Failed password for invalid user bob from 10.1.2.3", "bob"),
                           ("Invalid user bob from 10.1.2.3 port 5", "bob"),
                           ("error: maximum authentication attempts exceeded for bob from 10.1.2.3", "bob")):
            out = r(line)
            self.assertNotIn(name, out, out)
            self.assertNotIn("10.1.2.3", out)
            self.assertNotIn("example.org", out)
        # Überschwärzung: «failed» ist kein Benutzer
        self.assertEqual(r("login failed for user=bob"), "login failed for user=<user>")
        self.assertEqual(r("login failed"), "login failed")
        self.assertEqual(r("user not found"), "user not found")
        # Gegenbeispiele: gewöhnlicher Text bleibt
        self.assertEqual(r("Reading for the first time from disk"), "Reading for the first time from disk")
        self.assertEqual(r("Connection closed for nothing from the end"), "Connection closed for nothing from the end")
        self.assertEqual(r("authentication failure; logname= uid=0"), "authentication failure; logname= uid=<user>")

    def test_ntfy_nur_kritisch_standard(self):
        sent, events = [], []
        with patch.object(a, "ntfy", lambda t, x, tags="warning", priority="high": sent.append((t, priority)) or True), \
             patch.object(a, "EVENT_SINK", lambda k, l, t, x: events.append(t)):
            with patch.object(a, "NTFY_ALL", False):
                a.alerter("array", "err", critical=True)("Festplatte ausgefallen", "x")
                a.alerter("lan", "warn")("Neues Gerät im Heimnetz", "x")
                a.alerter("prices", "info")("Platte günstig: 4 TB", "x")
                a.alerter("array", "err", critical=True)("Array läuft wieder", "x", calm=True)
                self.assertEqual([t for t, _ in sent], ["Festplatte ausgefallen"])
            self.assertEqual(len(events), 4)            # «Heute» bekommt alles
            sent.clear()
            with patch.object(a, "NTFY_ALL", True):
                a.alerter("lan", "warn")("Neues Gerät im Heimnetz", "x")
                a.alerter("array", "err", critical=True)("Array läuft wieder", "x", calm=True)
                self.assertEqual(sent, [("Neues Gerät im Heimnetz", "high"), ("Array läuft wieder", "low")])

    def test_alertgate_24h_schlimmer_entwarnung(self):
        g = a.AlertGate()
        self.assertTrue(g.allow("k", 1, now=1000))
        self.assertFalse(g.allow("k", 1, now=1000 + 3600))
        self.assertTrue(g.allow("k", 2, now=1000 + 7200))          # schlimmer
        self.assertFalse(g.allow("k", 2, now=1000 + 86000))
        self.assertTrue(g.allow("k", 2, now=1000 + 7200 + 86401))    # nach 24 Std.
        self.assertTrue(g.clear("k"))
        self.assertFalse(g.clear("k"))
        self.assertTrue(g.allow("k", 1, now=1000 + 7200 + 86402))    # Entwarnung hob die Sperre auf

    def test_arraywatch_24h_und_entwarnung(self):
        import tempfile
        sent = []
        with tempfile.TemporaryDirectory() as proc:
            w = a.ArrayWatch(lambda t, x, calm=False: sent.append(("ruhig " if calm else "") + t), proc=proc)
            def md(state="STARTED", status="DISK_OK", errors=0):
                open(os.path.join(proc, "mdstat"), "w").write(
                    f"mdState={state}\nmdResync=0\ndiskName.1=md1p1\nrdevName.1=sdc\nrdevStatus.1={status}\nrdevNumErrors.1={errors}\n")
            md(); w.check()
            md(errors=2); w.check()
            md(errors=2); w.check()
            self.assertEqual(sent, ["Neue Plattenfehler"])
            md(errors=5); w.check()                                  # schlimmer: nochmals
            self.assertEqual(len(sent), 2)
            md(status="DISK_DSBL", errors=5); w.check()
            md(status="DISK_OK", errors=5); w.check()                # Entwarnung (ruhig)
            md(status="DISK_DSBL", errors=5); w.check()              # Sperre aufgehoben: wieder melden
            self.assertEqual(sent, ["Neue Plattenfehler", "Neue Plattenfehler", "Festplatte ausgefallen",
                                    "ruhig Festplatte wieder in Ordnung", "Festplatte ausgefallen"])

    def test_hitze_entwarnung_und_24h(self):
        store, got = a.Store(":memory:"), []
        w = a.Watcher(store, lambda t, x, calm=False: got.append(("ruhig " if calm else "") + t))
        t = 1_760_000_000
        def feed(temp, n):
            nonlocal t
            for _ in range(n):
                t += 30
                w.feed({"t": t, "cpu": 20.0, "temps": {"CPU": temp}, "disks": {}, "top": []})
        feed(97.0, 20)
        feed(60.0, 2)                                   # abgekühlt: ruhige Entwarnung, Sperre weg
        feed(97.0, 20)                                  # wieder heiss: neue Meldung
        self.assertEqual(got, ["NAS zu heiss", "ruhig NAS wieder normal", "NAS zu heiss"])

    def test_m1_erkennung_fremd(self):
        write(f"{self.d}/token.txt", "t")
        write(f"{self.d}/mariadb/db.opt", "x")
        own, foreign = a.config_is_own(self.d)
        self.assertFalse(own)
        self.assertEqual(foreign, ["mariadb"])

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "nur als root prüfbar")
    def test_n2_root_weiter_mit_warnung_in_info(self):
        write(f"{self.d}/token.txt", "t")
        os.chmod(self.d, 0o700)                                         # root-eigen, Benutzer 65534 kommt nicht hin
        write(f"{self.d}/andere/datei", "x")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            with patch.object(a.os, "lchown"):                          # chown schlägt fehl (z. B. NFS root_squash)
                self.assertIsNone(self._drop())
        w = a.privilege_warnings()
        self.assertEqual([x["code"] for x in w], ["running_as_root"])
        self.assertIn("root", w[0]["de"])
        a.PRIV_STATE["root_reason"] = ""
        self.assertEqual(a.privilege_warnings(), [])

    def test_m2_heute_ohne_logs_keine_syslog_zeilen(self):
        os.makedirs(f"{self.d}/log")
        stamp = time.strftime("%b %d %H:%M:%S", time.localtime(time.time() - 30))
        with open(f"{self.d}/log/syslog", "w") as f:
            f.write(f"{stamp} nas sshd[12]: error: Failed login for invalid user admin from 192.168.1.55 port 22 mac aa:bb:cc:dd:ee:ff "
                    "email bob@example.com\n")
            f.write(f"{stamp} nas kernel: error: cannot open /mnt/user/private/Steuern-2026.pdf\n")
        t = a.Today(a.Store(":memory:"), files_root=f"{self.d}/s", notify_dir=f"{self.d}/n", log_dir=f"{self.d}/log", docker_dir=f"{self.d}/d")
        with patch.object(a, "LOGS_ON", False):
            r = t.collect(24)
            self.assertEqual([e for e in r["items"] if e["kind"] == "syslog"], [])
            self.assertFalse(r["sources"]["syslog"])
        with patch.object(a, "LOGS_ON", True):
            items = [e for e in t.collect(24)["items"] if e["kind"] == "syslog"]
        self.assertEqual(len(items), 2)
        blob = json.dumps(items)
        for secret in ("192.168.1.55", "aa:bb:cc:dd:ee:ff", "bob@example.com", "admin", "Steuern-2026"):
            self.assertNotIn(secret, blob, secret)

    def test_m3_heimnetz_standard_aus(self):
        self.assertFalse(a.LAN_ON)                                      # Standard: aus
        lan = a.LanScan(os.path.join(self.d, "dev.db"), lambda *x: None)
        with patch.object(lan, "arp_path", return_value="/proc/net/arp"):
            self.assertFalse(lan.available())
            self.assertEqual(lan.reason(), "off")
            with patch.object(a.socket, "socket") as so:
                lan.scan()
                so.assert_not_called()                                  # kein einziges UDP-Paket
            with patch.object(a, "LAN_ON", True):
                self.assertTrue(lan.available())
                self.assertEqual(lan.reason(), "")
        srv = Server115(lan=lan)
        try:
            code, info, _ = srv.get("/api/info")
            self.assertEqual((code, info["lan"], info["lan_enabled"]), (200, False, False))
            self.assertIs(info["ntfy_all"], False)                      # ab 1.18.0, Standard aus
            self.assertNotIn("lan", info["features"])
            code, dev, _ = srv.get("/api/devices")
            self.assertEqual((code, dev["available"], dev["reason"], dev["enabled"], dev["devices"]), (200, False, "off", False, []))
            req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/devices/scan", data=b"", method="POST",
                                         headers={"Authorization": "Bearer geheim"})
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual((r.status, json.load(r)["started"]), (202, False))
        finally:
            srv.close()

    def test_n5_richtiger_token_trotz_bremse(self):
        srv = Server115()
        try:
            for _ in range(a.AUTH_FAILS + 1):
                srv.get("/api/info", tok="falsch")
            self.assertEqual(srv.get("/api/info", tok="falsch")[0], 429)
            self.assertEqual(srv.get("/api/info")[0], 200)
        finally:
            srv.close()



class Agent119Test(unittest.TestCase):
    """1.19.0: Host-Prüfung ping/health, knappe Token-Fehler, Bremse je /64, ntfy ohne Namen, stacked_member."""

    def test_g1_ping_health_host_header(self):
        srv = Server115()
        try:
            for path in ("/api/ping", "/api/health"):
                self.assertEqual(srv.get(path, tok=None, headers={"Host": "evil.example"})[0], 403, path)
                self.assertEqual(srv.get(path, tok=None, headers={"Host": "evil.example:8099"})[0], 403, path)
                self.assertEqual(srv.get(path, tok=None, headers={"Host": "192.168.1.5:8099"})[0], 200, path)
                self.assertEqual(srv.get(path, tok=None, headers={"Host": "tower:8099"})[0], 200, path)
            self.assertEqual(srv.get("/api/ping", tok="geheim", headers={"Host": "evil.example"})[0], 403)
            code, body, _ = srv.get("/api/ping", tok=None)
            self.assertEqual((code, body["ok"], body["api_level"], "version" in body), (200, True, a.API_LEVEL, False))
            self.assertIn("config_persistent", body)                      # die App braucht das ohne Token
            code, body, _ = srv.get("/api/ping", tok="geheim")
            self.assertEqual(body["version"], a.VERSION)
            code, body, _ = srv.get("/api/ping", tok="falsch")            # falscher Token: wie ohne Token
            self.assertEqual((code, "version" in body), (200, False))
        finally:
            srv.close()

    def test_g2_token_fehler_knapp(self):
        d = tempfile.mkdtemp()
        old = a.CONFIG
        a.CONFIG = d
        try:
            with open(f"{d}/token.txt", "w") as f:
                f.write("kurz")
            import contextlib
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.dict(os.environ, {"NZBLAB_AGENT_TOKEN": ""}):
                self.assertEqual(a.token(), "")
            self.assertEqual(a.TOKEN_STATE["code"], "short")
            self.assertIn(d, out.getvalue())                                # ausführlich nur im Log
            h = a.make_handler(a.Store(":memory:"), lambda: {}, "")
            # Anfrage von aussen (nicht Loopback): nur der Code
            fake = type("F", (), {"client_address": ("192.168.1.9", 1)})()
            sent = []
            h.refuse(type("S", (), {"client_address": ("192.168.1.9", 1), "_limited": 0,
                                    "reply": lambda self, c, o, hd=None: sent.append((c, o))})())
            h.refuse(type("S", (), {"client_address": ("127.0.0.1", 1), "_limited": 0,
                                    "reply": lambda self, c, o, hd=None: sent.append((c, o))})())
            (c1, o1), (c2, o2) = sent
            self.assertEqual((c1, o1["token_error"]), (503, "short"))
            txt = json.dumps(o1)
            self.assertNotIn(d, txt)
            self.assertNotIn("Zeichen", txt)
            self.assertNotIn("token.txt", txt)
            self.assertNotIn("token_error_detail", o2)                       # ab 1.19.0 auch von 127.0.0.1 nur der Code
            self.assertNotIn("Zeichen", json.dumps(o2))
        finally:
            a.CONFIG = old
            a.TOKEN_STATE.update(error="", source="", code="")
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_g3_ipv6_je_64(self):
        self.assertEqual(a.rate_key("2001:db8:1:2::1"), a.rate_key("2001:db8:1:2:ffff::9"))
        self.assertNotEqual(a.rate_key("2001:db8:1:2::1"), a.rate_key("2001:db8:1:3::1"))
        self.assertEqual(a.rate_key("::ffff:1.2.3.4"), "1.2.3.4")
        lim = a.AuthLimiter(max_fail=10, window=60, block=300)
        ips = [f"2001:db8:1:2::{i:x}" for i in range(1, 65)]
        for i in range(1000):
            lim.fail(ips[i % 64], now=100 + i * 0.01)
        self.assertGreater(lim.blocked_for(ips[0], now=111), 0)           # ein /64 = ein Absender
        self.assertGreater(lim.blocked_for("2001:db8:1:2::abcd", now=111), 0)
        self.assertEqual(lim.blocked_for("2001:db8:1:3::1", now=111), 0)  # anderes /64 frei

    def test_g3_richtiger_token_geht_immer_durch(self):
        lim = a.AuthLimiter(max_fail=10, window=60, block=300)
        srv = Server115(limiter=lim)
        try:
            for i in range(1000):
                lim.fail(f"2001:db8:1:2::{i % 64 + 1:x}", now=time.monotonic())
            self.assertGreater(lim.blocked_for("127.0.0.1"), -1)
            lim.fail("127.0.0.1"); [lim.fail("127.0.0.1") for _ in range(12)]
            self.assertGreater(lim.blocked_for("127.0.0.1"), 0)
            self.assertEqual(srv.get("/api/info", tok="falsch")[0], 429)
            self.assertEqual(srv.get("/api/info")[0], 200)                 # richtiger Token nie gebremst
        finally:
            srv.close()

    def test_eigener_token_warnt_im_log(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.dict(os.environ, {"NZBLAB_AGENT_TOKEN": "aB3dE5gH7jK9mN2pQ4sT6vW8yZ1cF0hL"}):
            self.assertEqual(a.token(), "aB3dE5gH7jK9mN2pQ4sT6vW8yZ1cF0hL")
        self.assertIn("zufälliger Token ist sicherer", out.getvalue())
        a.TOKEN_STATE.update(error="", source="", code="")

    def test_g1_vpn_namen_und_nzblab_hosts(self):
        self.assertTrue(a.local_host_header("nas.tail1234.ts.net:8099"))
        self.assertFalse(a.local_host_header("evil.example"))
        with patch.object(a, "EXTRA_HOSTS", ("nas.example.org", ".vpn.example.net")):
            self.assertTrue(a.local_host_header("nas.example.org:8099"))
            self.assertTrue(a.local_host_header("x.vpn.example.net"))
            self.assertFalse(a.local_host_header("evil.example"))
            self.assertFalse(a.local_host_header("evil.nas.example.org"))
            self.assertFalse(a.local_host_header("vpn.example.net.evil.example"))
        srv = Server115()
        try:
            self.assertEqual(srv.get("/api/ping", tok=None, headers={"Host": "nas.tail1234.ts.net"})[0], 200)
            self.assertEqual(srv.get("/api/ping", tok=None, headers={"Host": "evil.example"})[0], 403)
        finally:
            srv.close()

    def test_g3_hello_limiter_ipv6_64(self):
        lim = a.HelloLimiter(max_calls=2, window=60)
        self.assertEqual(lim.allow("2001:db8::1", now=1), 0)
        self.assertEqual(lim.allow("2001:db8::2", now=1), 0)
        self.assertGreater(lim.allow("2001:db8::3", now=1), 0)

    def test_g4_ntfy_ohne_namen_an_fremde_server(self):
        for url, ok in (("https://ntfy.sh/abcdefgh12", False), ("https://ntfy.example.com/abcdefgh12", False),
                        ("https://192.168.1.20/abcdefgh12", True), ("https://ntfy.lan/abcdefgh12", True), ("https://ntfy/abcdefgh12", True)):
            with patch.object(a, "ntfy_url", lambda url=url: url), patch.object(a, "NTFY_ALL", False), patch.object(a, "NTFY_NAMES", False):
                self.assertIs(a.ntfy_names_allowed(), ok, url)
        with patch.object(a, "ntfy_url", lambda: "https://ntfy.example.com/abcdefgh12"), patch.object(a, "NTFY_NAMES", True):
            self.assertTrue(a.ntfy_names_allowed())
        for url, names in (("https://ntfy.sh/abcdefgh12", False), ("https://192.168.1.20/abcdefgh12", True)):
            msgs = []
            w = a.Watcher(a.Store(":memory:"), lambda t, x, calm=False: msgs.append(x))
            with patch.object(a, "ntfy_url", lambda url=url: url), patch.object(a, "NTFY_ALL", False), patch.object(a, "NTFY_NAMES", False):
                for t in (0, 400):
                    w.feed({"t": 1000 + t, "cpu": 90.0, "temps": {"CPU": 99.0}, "disks": {}, "top": [("plex", 80.0), ("radarr", 5.0)]})
            self.assertEqual(len(msgs), 1)
            self.assertIn("99 °C", msgs[0])
            self.assertEqual("plex" in msgs[0], names, url)

    def test_g4_ntfy_env_wird_geprueft(self):
        import contextlib
        import io
        a._NTFY_WARNED.discard("env")
        with patch.object(a, "settings", lambda: {}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch.object(a, "NTFY_URL", "https://ntfy.sh/kurz"):
                self.assertEqual(a.ntfy_url(), "")                         # Thema zu kurz: ntfy aus
            self.assertIn("WARNUNG", out.getvalue())
            self.assertNotIn("kurz", out.getvalue().replace("zu kurz", ""))  # Adresse nicht im Log
            with patch.object(a, "NTFY_URL", "http://ntfy.sh/abcdefgh12"):
                self.assertEqual(a.ntfy_url(), "")                         # nur https
            with patch.object(a, "NTFY_URL", "https://ntfy.sh/abcdefgh12"):
                self.assertEqual(a.ntfy_url(), "https://ntfy.sh/abcdefgh12")

    def test_stacked_member_nur_exists(self):
        d = tempfile.mkdtemp()
        os.makedirs(f"{d}/class/net/eth0")
        os.symlink("/nonexistent-target", f"{d}/class/net/eth0/master")   # toter Link zählt nicht
        os.makedirs(f"{d}/class/net/eth1/master")
        stub = type("N", (), {"sys": d})()
        self.assertFalse(a.Sampler.stacked_member(stub, "eth0"))        # toter Link: kein Mitglied
        self.assertTrue(a.Sampler.stacked_member(stub, "eth1"))
        import shutil
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()