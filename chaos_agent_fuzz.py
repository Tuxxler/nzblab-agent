"""Chaos-Test für den Agenten (QA 28.10.2026): schickt kaputte, übergrosse und bösartige Anfragen an einen lokal
gestarteten Agenten. Läuft nur mit NZBLAB_CHAOS=1:  NZBLAB_CHAOS=1 python3 -m unittest chaos_agent_fuzz -v
Erwartung: der Agent beantwortet jede Anfrage (kein Verbindungsabbruch ohne Antwort), gibt ohne Token keine Daten heraus
und bleibt danach erreichbar."""
import json
import os
import random
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import nzblab_agent as a

CHAOS = os.environ.get("NZBLAB_CHAOS") == "1"
TOK = "geheim-Token_123"


def raw(port, data, timeout=4):
    """Rohe Bytes schicken, Antwort (Status-Zeile + Body) oder Fehlertext zurück."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        buf = b""
        while True:
            try:
                c = s.recv(65536)
            except socket.timeout:
                return buf, "TIMEOUT"
            if not c:
                break
            buf += c
        return buf, None
    finally:
        s.close()


def status(buf):
    try:
        return int(buf.split(b" ", 2)[1])
    except Exception:
        return None


@unittest.skipUnless(CHAOS, "nur mit NZBLAB_CHAOS=1")
class ChaosAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        # settings.json (Kennung für /api/hello) im Test-Ordner statt in /config
        cls.cfg = patch.object(a, "CONFIG", cls.dir)
        cls.cfg.start()
        root = os.path.join(cls.dir, "root")
        for p in ("Media/Filme/X (2000)/x.mkv", "Media/Serien/Ä ö/S01E01.mkv", "Downloads/a_b%c.txt"):
            os.makedirs(os.path.dirname(os.path.join(root, p)), exist_ok=True)
            with open(os.path.join(root, p), "wb") as f:
                f.write(b"x" * 100)
        cls.ix = a.FileIndex(os.path.join(cls.dir, "i.db"), root=root, exclude=set())
        cls.ix.build()
        cls.crashes = a.CrashStore(os.path.join(cls.dir, "crashes"))
        store = a.Store(":memory:")
        store.add({"t": int(time.time()), "cpu": 5.0})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), a.make_handler(store, lambda: {"cpu": 1.0}, TOK, index=cls.ix, crashes=cls.crashes))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_port
        cls.findings = []

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.cfg.stop()
        out = os.path.join(cls.dir, "findings.txt")
        with open(out, "w") as f:
            f.write("\n".join(cls.findings))
        print("\n=== CHAOS-AGENT-BEFUNDE ===\n" + "\n".join(cls.findings) + f"\n({out})")

    def req(self, method, path, headers=None, body=b"", timeout=4):
        h = {"Host": "x", "Connection": "close"}
        h.update(headers or {})
        if body and "Content-Length" not in h:
            h["Content-Length"] = str(len(body))
        head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        return raw(self.port, head.encode("utf-8", "surrogateescape") + body, timeout)

    def alive(self):
        buf, err = self.req("GET", "/api/ping")
        return status(buf) == 200

    def note(self, what, buf, err):
        st = status(buf)
        if err or st is None:
            self.findings.append(f"KEINE ANTWORT ({err or 'Verbindung ohne Antwort zu'}): {what}")
        elif st >= 500:
            self.findings.append(f"HTTP {st}: {what} → {buf[-200:]!r}")
        return st

    # ------------------------------------------------------------------ ohne Token: keine Daten
    def test_ohne_token_keine_daten(self):
        paths = ["/api/info", "/api/now", "/api/history", "/api/files/search?q=x", "/api/files/tree", "/api/inventory",
                 "/api/appdata", "/api/cache/shares", "/api/wireguard", "/api/today", "/API/INFO", "/api//info", "/api/info/",
                 "/api/info;x", "/api/info%00", "/./api/info", "/../api/info", "//api/info", "/api/%69nfo"]
        bad = ["", "Bearer", "Bearer ", "bearer " + TOK, "Bearer " + TOK + " ", "Bearer " + TOK[:-1], "Basic " + TOK,
               "Bearer " + TOK + "\x00", "Bearer ÄÖÜ" , "Bearer " + "A" * 60000, TOK]
        for p in paths:
            for b in bad:
                h = {"Authorization": b} if b else {}
                buf, err = self.req("GET", p, h)
                st = self.note(f"GET {p} Auth={b[:30]!r}", buf, err)
                body = buf.split(b"\r\n\r\n", 1)[-1]
                if st == 200:
                    self.findings.append(f"DATEN OHNE GÜLTIGEN TOKEN: GET {p} Auth={b[:30]!r} → {body[:120]!r}")
        for p in ["/api/config", "/api/crash", "/api/speedtest", "/api/files/refresh", "/api/notify/test"]:
            buf, err = self.req("POST", p, {"Authorization": "Bearer falsch"}, b"{}")
            if status(buf) != 401:
                self.findings.append(f"POST {p} ohne Token → {status(buf)}")
        self.assertTrue(self.alive())

    # ------------------------------------------------------------------ Parameter-Fuzzing mit Token
    def test_parameter_fuzz(self):
        auth = {"Authorization": "Bearer " + TOK}
        vals = ["", "-1", "0", "1e3", "abc", "99999999999999999999999", "-99999999999999999999", "１２", "NaN", "inf",
                "%00", "%FF%FE", "..%2F..%2Fetc%2Fpasswd", "%25", "_", "%27%22%3B--", "😀" * 50, "A" * 5000]
        params = {"/api/history": ["hours", "res"], "/api/wakes": ["hours"], "/api/today": ["hours"],
                  "/api/files/cleanup": ["days"], "/api/files/largest": ["limit", "path"], "/api/files/search": ["q", "limit"],
                  "/api/files/tree": ["path"], "/api/files/duplicates": ["min_mb", "limit"], "/api/crashes": ["limit"],
                  "/api/hello": ["nonce"]}
        for p, keys in params.items():
            for k in keys:
                for v in vals:
                    path = f"{p}?{k}={v}" if v.isascii() else f"{p}?{k}=" + v.encode().hex()
                    buf, err = self.req("GET", path, auth)
                    self.note(f"GET {path[:120]}", buf, err)
        self.assertTrue(self.alive())

    def test_hello_ohne_token(self):
        """/api/hello (ab 1.11.0): ohne Token erreichbar, verrät aber weder Token noch Werte vom NAS."""
        for v in ["", "x", "0" * 32, "0" * 32 + "&nonce=" + "1" * 32, "%30" * 32, "A" * 5000, "0" * 32 + "%00"]:
            buf, err = self.req("GET", "/api/hello?nonce=" + v)
            self.note(f"GET /api/hello?nonce={v[:40]}", buf, err)
            body = buf.split(b"\r\n\r\n", 1)[-1]
            if TOK.encode() in body or b"hostname" in body:
                self.findings.append(f"HELLO VERRÄT ZU VIEL: {v[:40]!r} → {body[:160]!r}")
        self.assertTrue(self.alive())

    def test_pfad_ausbruch(self):
        auth = {"Authorization": "Bearer " + TOK}
        for v in ["../../etc", "/etc", "..", "Media/../..", "%2e%2e/%2e%2e", "Media/Filme/X (2000)/../../../../../"]:
            buf, err = self.req("GET", "/api/files/tree?path=" + v, auth)
            body = buf.split(b"\r\n\r\n", 1)[-1]
            if b"passwd" in body or b"\"found\": true" in body and b"etc" in body:
                self.findings.append(f"PFAD-AUSBRUCH tree {v}: {body[:200]!r}")
        # Suche nach LIKE-Platzhaltern: «%» und «_» sollten wörtlich gesucht werden
        buf, _ = self.req("GET", "/api/files/search?q=%25", auth)
        n = len(json.loads(buf.split(b"\r\n\r\n", 1)[-1])["results"])
        if n > 1:
            self.findings.append(f"Suche nach «%» liefert {n} Treffer (LIKE-Platzhalter nicht maskiert, findet alles)")
        buf, _ = self.req("GET", "/api/files/search?q=_", auth)
        n = len(json.loads(buf.split(b"\r\n\r\n", 1)[-1])["results"])
        if n > 1:
            self.findings.append(f"Suche nach «_» liefert {n} Treffer (LIKE-Platzhalter nicht maskiert)")

    # ------------------------------------------------------------------ kaputte Körper
    def test_post_koerper(self):
        auth = {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"}
        bodies = [b"", b"null", b"[]", b"1", b'"x"', b"{", b'{"trace": null}', b'{"trace": []}', b'{"trace": {"a":1}}',
                  b'{"trace": 1e999}', b'{"trace": "x", "version": {"a": [1,2]}}', b'{"ntfy_url": 5}', b'{"price_alerts": {"8": NaN}}',
                  b'{"price_alerts": {"8": Infinity}}', b'{"price_alerts": []}', b'{"price_watch": "x"}', b'{"price_watch": [1]}',
                  b'{"price_country": null}', b'{"price_country": "\\u00e4\\u00f6"}', b'{"price_alerts": {"8": true}}',
                  "{\"trace\": \"\ud800\"}".encode("utf-8", "surrogatepass"), b"\xef\xbb\xbf{}", b'{"a":' * 5000 + b"1" + b"}" * 5000]
        with patch.object(a, "save_settings") as save, patch.object(a, "ntfy", return_value=False):
            for p in ["/api/config", "/api/crash"]:
                for b in bodies:
                    buf, err = self.req("POST", p, auth, b)
                    st = self.note(f"POST {p} {b[:60]!r}", buf, err)
                    if p == "/api/config" and st == 200:
                        self.findings.append(f"INFO /api/config akzeptiert {b[:60]!r}; gespeichert: {save.call_args}")
            # Content-Length grösser als Körper: hängt der Arbeits-Thread ewig?
            t0 = time.time()
            buf, err = self.req("POST", "/api/crash", dict(auth, **{"Content-Length": "5000"}), b'{"trace":', timeout=13)
            if err == "TIMEOUT":
                self.findings.append(f"POST /api/crash mit zu grosser Content-Length: Agent wartet ewig auf den Rest (kein Lese-Timeout, {time.time() - t0:.0f}s ohne Antwort)")
            buf, err = self.req("POST", "/api/crash", dict(auth, **{"Content-Length": "abc"}), b"")
            self.note("POST /api/crash Content-Length=abc", buf, err)
        self.assertTrue(self.alive())

    def test_kaputte_anfragezeilen(self):
        junk = [b"\r\n\r\n", b"GET\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"FOO /api/ping HTTP/1.1\r\n\r\n",
                b"GET /api/ping HTTP/1.1\r\nX: " + b"A" * 70000 + b"\r\n\r\n",
                b"GET /api/ping HTTP/1.1\r\n" + b"".join(b"H%d: x\r\n" % i for i in range(150)) + b"\r\n",
                b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n", b"GET /api/ping\xff\xfe HTTP/1.1\r\n\r\n",
                b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + bytes(range(200)),  # TLS-Hallo
                b"PUT /api/config HTTP/1.1\r\n\r\n", b"DELETE /api/crashes HTTP/1.1\r\n\r\n", b"OPTIONS * HTTP/1.1\r\n\r\n",
                b"HEAD /api/info HTTP/1.1\r\n\r\n"]
        for j in junk:
            buf, err = raw(self.port, j)
            if err:
                self.findings.append(f"Rohanfrage {j[:40]!r}: {err}")
        self.assertTrue(self.alive())

    # ------------------------------------------------------------------ Zufall (Seed fest)
    def test_zufall(self):
        rnd = random.Random(4711)
        auth = {"Authorization": "Bearer " + TOK}
        paths = ["/api/history", "/api/files/search", "/api/files/tree", "/api/files/largest", "/api/files/cleanup",
                 "/api/today", "/api/wakes", "/api/files/duplicates", "/api/crashes"]
        keys = ["hours", "days", "limit", "q", "path", "min_mb", "res"]
        for i in range(400):
            q = "&".join(f"{rnd.choice(keys)}={rnd.choice(['', '0', '-5', str(rnd.randint(-10**30, 10**30)), 'x%20y', '%25%5F', '1.5'])}"
                         for _ in range(rnd.randint(0, 4)))
            p = rnd.choice(paths) + ("?" + q if q else "")
            buf, err = self.req("GET", p, auth)
            self.note(f"[{i}] GET {p[:150]}", buf, err)
        self.assertTrue(self.alive())

    def test_parallel_flut(self):
        """100 gleichzeitige Anfragen, dann noch erreichbar?"""
        auth = {"Authorization": "Bearer " + TOK}
        errs = []

        def one(i):
            buf, err = self.req("GET", "/api/files/search?q=x&limit=1000", auth, timeout=10)
            if status(buf) != 200:
                errs.append((i, status(buf), err))
        ts = [threading.Thread(target=one, args=(i,)) for i in range(100)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        if errs:
            self.findings.append(f"Parallel-Flut: {len(errs)} von 100 ohne 200, z. B. {errs[:3]}")
        self.assertTrue(self.alive())

    def test_halbe_anfragen_ohne_token(self):
        """Slowloris: 200 Verbindungen ohne Token schicken nur den Anfang der Kopfzeilen. Werden sie je geschlossen?"""
        before = threading.active_count()
        socks = []
        for _ in range(200):
            s = socket.create_connection(("127.0.0.1", self.port))
            s.sendall(b"GET /api/ping HTTP/1.1\r\nX: ")
            socks.append(s)
        time.sleep(12)   # Handler-Timeout ist 10 s (seit 28.10.2026)
        held = threading.active_count() - before
        if held >= 150:
            self.findings.append(f"Slowloris ohne Token: {held} Arbeits-Threads hängen nach 12 s noch (kein Socket-Timeout im Handler)")
        for s in socks:
            s.close()
        self.assertTrue(self.alive())


if __name__ == "__main__":
    unittest.main()
