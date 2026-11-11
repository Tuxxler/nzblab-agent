# NASlab-Agent (früher NZBLab-Agent)

Container, Image (`ghcr.io/tuxxler/nzblab-agent`), Ordner und Pfade heissen weiter `nzblab-agent`; die Log-Zeile mit dem Token lautet weiterhin «NZBLab-Agent Token: …».

Kleiner Dienst auf dem NAS, der rund um die Uhr misst und die Werte der NASlab-App liefert:
Temperaturen aller Sensoren, CPU-Last (gesamt und pro Kern), Grafikchip (Grafik- und Medien-Einheit, die Medien-Einheit
rechnet Videos für Plex um), Arbeitsspeicher, Plattenzugriffe (wann eine Platte nach einer Pause anläuft) und welche
Programme die meiste CPU brauchen. 8 Tage volle Auflösung (alle 30 s), 2 Jahre Stundenwerte.

**Nur lesen.** Der Agent ändert nichts am NAS. Alle Host-Pfade sind nur lesend eingebunden, schreibbar ist nur `/config`.

## Installieren (für alle, ab Agent 1.10.0)

Fertiges Image: **`ghcr.io/tuxxler/nzblab-agent:latest`** (amd64 und arm64, gebaut von `.github/workflows/agent-image.yml`,
Grundlage `python:3.12-alpine3.22`, nur Python-Standardbibliothek). Kostenlos, Quellcode öffentlich einsehbar:
<https://github.com/Tuxxler/nzblab-agent> (Lizenz siehe unten).
Gleiche Anleitung in der App (Einstellungen › Server-Verbindungen › NASlab-Agent › «Agent installieren») und auf der
Website <https://tuxxler.github.io/nzblab/agent.html>.

| System | Datei | Kurz |
|---|---|---|
| Unraid | `unraid/nzblab-agent.xml` | Apps › «NASlab-Agent» › Install; bis zur Aufnahme in CA: Docker › Template repositories › `https://github.com/Tuxxler/nzblab-agent` |
| TrueNAS SCALE (24.10+) | `compose/truenas.yml` | Apps › Discover Apps › ⋮ › Install via YAML; `tank` = eigener Pool |
| Synology DSM 7.2+ | `compose/synology.yml` | Ordner `docker/nzblab-agent` anlegen; Container Manager › Projekt › Erstellen |
| Linux (Debian, Ubuntu …, ab 1.14.0) | `compose/linux.yml` | Ordner anlegen, Datei als `docker-compose.yml` speichern, `docker compose up -d`; siehe unten |
| Docker allgemein / alle in einer Datei (ab 1.15.0) | `docker-compose.yml` | Block A–D für deinen Server einkommentieren, freiwillige Blöcke S (SMART), L (Logs), B2 (Backups); `docker compose up -d` |

**Token:** Beim ersten Start erzeugt der Agent einen zufälligen Token (`/config/token.txt`, Rechte 600) und schreibt ihn
**genau einmal** ins Container-Log:

```
================================================================
NZBLab-Agent Token: …
In der App eintragen: Einstellungen › Server-Verbindungen › NASlab-Agent.
================================================================
```

Danach steht er nur noch in der Datei. Wer das Container-Log lesen kann, ist ohnehin Admin des NAS. Bewusst **keine**
Setup-Seite, die den Token im Browser zeigt: sonst könnte ihn jedes Gerät im Heimnetz abholen. Neuer Token: `token.txt`
löschen und Container neu starten. Eigener Token: Variable `NZBLAB_AGENT_TOKEN` (dann nichts im Log, sie hat Vorrang): **mindestens 32 zufällige Zeichen ohne
Leerzeichen, besser leer lassen** (ab 1.16.1; kürzere Werte oder Werte mit weniger als 8 verschiedenen Zeichen, z. B.
`aaaa…`, lehnt der Agent mit `503`/`token_error` ab).

**Pfad `/config` ist Pflicht:** Hier speichert der Agent Token und Verlauf. Ohne eigenen Pfad liegt beides nur im
Container und ist nach jedem Update weg: Dann gibt es jedes Mal einen neuen Token, die App verbindet sich nicht mehr. Unraid:
Docker › nzblab-agent › Bearbeiten › Pfad `/config` → `/mnt/user/appdata/nzblab-agent` (Lesen/Schreiben) › Übernehmen.
Compose: `- ./nzblab-agent:/config` (bzw. der Pfad in den Vorlagen).

Ab 1.13.0 sichert der Agent das ab:

- Ein neuer Token entsteht **nur**, wenn `token.txt` fehlt. Ist die Datei leer, beschädigt, zu kurz (unter 32 Zeichen, ab 1.16.1), ein Symlink (ab 1.16.1 mit `O_NOFOLLOW` nie gelesen) oder nicht lesbar (Rechte),
  überschreibt er sie nie: Fehlermeldung im Log, und alle Anfragen mit Token antworten `503` mit dem genauen Grund
  (`{"error": …, "token_error": …}`). Lösung: alten Token in die Datei schreiben, Datei löschen (dann neuer Token) oder
  `NZBLAB_AGENT_TOKEN` setzen.
- Beim Start prüft er in `/proc/self/mountinfo`, ob `/config` (oder ein Ordner darüber, ausser `/`) ein eigener Mount ist.
  Nicht bleibend sind: nur das Dateisystem des Containers, tmpfs, ein anonymer Docker-Volume (`VOLUME /config` im Image
  ohne eigenen Pfad, wird beim Neuerstellen ersetzt) und ein nur lesender Mount. Dann: laute Warnung mit Lösung im Log
  (Deutsch und Englisch), `config_persistent: false` in `/api/ping` und `/api/info` (dort dazu `warnings`) und ein Hinweis
  auf der Browser-Seite.
- Ist `settings.json` beschädigt (kein gültiges JSON), hebt er sie vor dem nächsten Speichern als
  `settings.json.broken-<Zeit>` auf und meldet das im Log, statt sie still zu überschreiben.

**Ohne root:** Das Image setzt `PUID=99`, `PGID=100` (nobody:users). Der Agent startet als root, legt den Token an, liest die
Container-Namen (`/host/docker/containers`, nur root lesbar), gibt `/config` diesem Benutzer und läuft dann ohne root weiter.
Kann er `/config` nicht übergeben (z. B. NFS mit root_squash), bleibt er mit Hinweis im Log root. `PUID=0` = immer root:
nötig nur für appdata-Grössen anderer Container mit engen Rechten. TrueNAS-Vorlage: 568:568 (Benutzer «apps»);
Synology-Vorlage: root, weil DSM-ACLs im Container keinem Benutzer entsprechen.

**Härtung in allen Vorlagen:** `--read-only` (Root-Dateisystem), `/tmp` als tmpfs, `no-new-privileges`, 512 MB RAM, 1 CPU,
kein `privileged`, keine Zusatz-Capabilities. HEALTHCHECK im Image auf `/api/ping`.

Weitere Variablen für andere Systeme: `FILES_EXCLUDE` (oberste Ordner, auch Muster wie `@*`), `FILES_SKIP` (in jeder Ebene,
Standard `@eaDir,#recycle,#snapshot`), `CACHE_ROOTS` (leer = kein Cache).

Optional: Push-Meldung bei Hitze auch unterwegs: Variable `NTFY_URL` = `https://ntfy.sh/<langes-geheimes-wort>` und in der
App «ntfy» dasselbe Thema abonnieren. Schwelle: `HEAT_ALERT_C` (Standard 95 °C, nach 5 Minuten).

## Linux (Debian, Ubuntu …) (ab 1.14.0)

Für Linux-Server ohne eigene NAS-Schnittstelle (Debian, Ubuntu, Raspberry Pi OS und ähnliche). In der App heisst der
Server-Typ **«Linux (Beta)»** (Einstellungen › Server-Verbindungen › NAS-Server). Übersicht, Speicher, Temperaturen und
Meldungen kommen vom Agenten; Docker-Container steuert die App optional über **Portainer** (eigener Dienst in der App,
Zugriffstoken aus Portainer › Mein Konto › Zugriffstoken). Der Agent selbst bekommt **kein** `docker.sock`.

**Beta:** nur mit Testdaten geprüft (nachgebautes `/proc`, `/sys` und `os-release` in `test_agent.py`, Demo-Agent in der App),
noch nie an einem echten Debian- oder Ubuntu-Server.

Vorlage `compose/linux.yml`:

1. Ordner anlegen und den Ordner für Token und Verlauf dem Agenten-Benutzer geben:
   `mkdir -p ~/nzblab-agent/nzblab-agent-config && sudo chown 65534:65534 ~/nzblab-agent/nzblab-agent-config`,
   dann in `~/nzblab-agent` die Datei als `docker-compose.yml` speichern.
2. Ordner mit Daten anpassen (siehe unten), dann `docker compose up -d`.
3. Token: `docker logs nzblab-agent 2>&1 | grep "NZBLab-Agent Token"`. In der App den Dienst «NASlab-Agent» eintragen und
   beim NAS-Server den Typ «Linux (Beta)» wählen.

**Sicherheit zuerst: nie das ganze System (`/`) einbinden.** Der Agent braucht es nicht. Er liest:

| Pfad im Container | vom Host | wofür |
|---|---|---|
| `/host/proc` | `/proc` (nur lesen) | CPU, RAM, Last, Betriebszeit, Einhängepunkte (`/host/proc/1/mountinfo`, sonst `mounts`, sonst die eigenen des Containers), Netz |
| `/host/sys` | `/sys` (nur lesen) | Platten (Modell, Grösse, drehend, Temperatur über `drivetemp`/NVMe), Netzwerkkarten, Sensoren |
| `/host/os-release` | `/etc/os-release` (nur lesen, **eine Datei**) | Name und Version des Systems |
| `/host/fs/<Name>` | ein Ordner deiner Wahl (nur lesen) | **Belegung** (belegt, frei, gesamt per `os.statvfs`) und Datei-Index |
| `/host/docker-root` | optional | Belegung des Docker-Datenordners. Vorsicht: `/var/lib/docker` enthält die Einstellungen aller Container (auch Passwörter); nur einbinden, wenn nötig |

Belegung gibt es nur für Ordner unter `/host/fs` (Beispiel `- /srv:/host/fs/data:ro`, weitere mit eigenem Namen). Alle anderen
Dateisysteme stehen ohne Belegung in der Liste, die Grösse kommt aus `/sys/class/block`. Pseudo-Dateisysteme (proc, tmpfs,
overlay, squashfs …), `/proc`, `/sys`, `/dev`, `/run`, `/boot/efi` und Docker-Overlays sind ausgeblendet. Mit
`FILES_ROOT=/host/fs` indexiert der Agent genau diese Ordner (Speicher-Analyse, Suche, Duplikate).

`network_mode: host`: So sieht der Agent die echten Netzwerkkarten und IP-Adressen des Servers und die Geräte im Heimnetz.
Port 8099 liegt dann direkt auf dem Server: nie ins Internet freigeben.

**Benutzer (wichtig):** Die Vorlage startet den Agenten direkt als `65534:65534` (nobody, kein Login) mit `cap_drop: ALL`
und `no-new-privileges`; er wechselt also nie von root weg, sondern hat root gar nicht erst. **Nicht** als root und
**nicht** als deinen eigenen Benutzer (z. B. 1000) starten: Mit `/proc` des Servers kann ein Prozess über
`/proc/<pid>/root` in das Dateisystem anderer Prozesse **desselben** Benutzers schauen, mit dessen Schreibrechten. Als
nobody betrifft das höchstens Programme, die selbst als nobody laufen. Noch strenger: eine eigene, sonst unbenutzte Nummer
(z. B. `user: "8099:8099"`, Ordner entsprechend `chown 8099:8099`). Ordner unter `/host/fs` müssen für diesen Benutzer
lesbar sein (meist «für alle lesbar»); die Belegung (`statvfs`) geht auch ohne Leserecht.

Optional härter: `/proc` auf dem Server mit `hidepid=invisible` (früher `hidepid=2`) einhängen
(`proc /proc proc defaults,hidepid=invisible 0 0` in `/etc/fstab`). Dann sieht der Agent keine fremden Prozesse mehr:
Die Liste «meiste Last» bleibt leer, und die Einhängepunkte kommen nur noch aus den eingebundenen Ordnern.

Hängt ein Netzwerk-Mount unter `/host/fs` (NFS/SMB weg), wartet der Agent höchstens `STATVFS_TIMEOUT` Sekunden (Standard 2)
und markiert ihn mit `timeout: true`. `/api/host` und `/api/filesystems` rechnet er höchstens alle `HOST_CACHE_S`
Sekunden neu (Standard 15).

Die Vorlagen für Unraid (99:100), TrueNAS (568:568) und Synology (root) sind davon unverändert.

Unraid, TrueNAS und Synology ändern sich dadurch nicht: Die neuen Pfade sind nur zusätzlich.

## Ab 1.15.0: Neustarts, Plattentemperaturen, Netz, SMART, Backups, RAID, Logs

**Versionen:** `/api/info` liefert `api_level` (15 für 1.15.0) und `features`. Die App vergleicht `api_level` mit ihrem
Minimum und zeigt «neuer Agent verfügbar». Der Agent aktualisiert sich **nie selbst**: neue Version = Image neu holen
(Unraid «update ready», Compose `docker compose pull && docker compose up -d`).

Immer an (nur lesen, nichts Neues einzubinden):

- **Neustarts:** beim Start und jede Minute `btime` aus `/proc/stat` und `/proc/uptime`. Neu gespeichert, wenn die
  Betriebszeit kleiner ist als zuletzt gesehen oder das System erst nach dem letzten Lebenszeichen des Agenten gestartet ist.
  `previous_uptime_s` = zuletzt gesehene Betriebszeit davor (war der Agent vorher gestoppt: Mindestwert). Ob ein Neustart
  geplant war, weiss der Agent nicht (`planned: "unknown"`). 2 Jahre.
- **Plattentemperatur pro Gerät:** alle 5 Minuten aus drivetemp (SATA) bzw. nvme-hwmon, mit SMART auch von Platten ohne
  hwmon. Rohwerte 2 Tage, danach Stundenwerte (Mittel, Tief, Hoch) für 400 Tage.
- **RAID/Pools:** mdadm aus `/proc/mdstat` (ausgefallen, Resync/Recovery mit Prozent), ZFS aus `/proc/spl/kstat/zfs/<pool>/state`
  (ohne `zpool`; gibt es `zpool` im Container, wird es gefragt, sonst `unavailable`), Btrfs aus `/sys/fs/btrfs` (Profile,
  fehlende Geräte, Fehlerzähler). Unraid-Array weiter über `/api/array`.
- **Netz/Latenz** (`NZBLAB_NET=0` schaltet aus): jede Minute (`NZBLAB_NET_INTERVAL`) je 3 TCP-Verbindungsaufbauten zum Router
  (Standard-Gateway des NAS, Port `NZBLAB_NET_GATEWAY_PORT` = 53; eine Ablehnung zählt auch als Antwort) und zu
  `NZBLAB_NET_TARGET` (Standard `1.1.1.1:443`). Gespeichert: Median in ms und Verlust in %. Kein ICMP, keine Rechte nötig,
  es werden keine Daten gesendet (nur Verbindungsaufbau und sofort schliessen).
- **Backups** (best effort): Ordner in `NZBLAB_BACKUP_PATHS` (Komma, Pfade im Container, nur lesend eingebunden) und
  Suche unter `/host/mnt/user` bzw. `/host/fs` (`NZBLAB_BACKUP_AUTO=0` schaltet sie aus): oberste Ebene jeder Freigabe, darunter
  nur Ordner mit Backup-Namen (backup, restic, borg, kopia, sicherung, rsync …), höchstens 3 Ebenen, alle 6 Stunden.
  restic (`config` + `snapshots/` + `data/` + `keys/`): neueste Datei in `snapshots/`; borg (`README`): Zeit von
  `index.N`/`hints.N`; Kopia (`kopia.repository.f`): neueste Index-/Metadaten-Blobs. Die Werkzeuge werden nie gestartet, verschlüsselte
  Inhalte nie gelesen. Log-Dateien (rsync, cron-Skripte): letzte Erfolgszeile (`sent … bytes received`, `total size is`,
  `backup completed` …) und letzte Fehlerzeile (gefiltert wie die Logs). Was sich nicht bestimmen lässt, ist `null`.

Freiwillig (eigener Compose-Block in `docker-compose.yml`):

| Block | Schalter | Liest | Braucht |
|---|---|---|---|
| S SMART | `NZBLAB_SMART=1` (`NZBLAB_SMART_MINUTES`, Standard 60) | `smartctl --scan` und `smartctl --json -a -n standby` pro Platte: Modell, Seriennummer, Betriebsstunden, neu zugewiesene, instabile und nicht korrigierbare Sektoren, CRC- und Medienfehler, Verschleiss, Temperatur. Nie Selbsttests, nie Schreiben, schlafende Platten bleiben schlafen | `devices:` jede Platte, `cap_add: SYS_RAWIO` (NVMe zusätzlich `SYS_ADMIN`), Start als root mit PUID (nicht `user:`) |
| L Logs | `NZBLAB_LOGS=1` | Fehler-/Warnzeilen der letzten Stunden aus `syslog`, `messages`, `kern.log`, `daemon.log` (Textdateien; journald-Binärdateien nicht) | `/var/log:/host/log:ro` |
| B2 Backups | `NZBLAB_BACKUP_PATHS` | Repository-Ordner (nur Dateinamen und -zeiten) und Log-Dateien | die Pfade `:ro` |

**SMART und Rechte:** smartctl braucht root und `CAP_SYS_RAWIO`. Damit der Agent selbst trotzdem ohne Rechte läuft, startet
er (als root gestartet, `NZBLAB_SMART=1`) vor dem Wechsel auf PUID einen kleinen Helfer-Prozess, der root bleibt und über ein
privates Socket-Paar nur zwei Befehle annimmt (`--scan`, `-a -n standby` für Gerätenamen wie `/dev/sda`, `/dev/nvme0`).
Der Agent selbst verliert beim Wechsel auf PUID alle Capabilities. Gruppen-IDs für `/dev` sind darum nicht nötig. Mit
`user:` im Compose (Linux-Einzeldatei) gibt es keinen root-Start: SMART meldet dann einen Rechtefehler (Docker gibt
Nicht-root-Prozessen kein `SYS_RAWIO`, auch nicht mit `group_add: [disk]` = GID 6). Image: `smartmontools` aus Alpine,
rund 3 MB; ohne `NZBLAB_SMART=1` wird es nie gestartet. Mit dem alten Setup (offizielles `python:3.12-alpine`, Code
eingebunden) fehlt smartctl: `/api/smart` meldet «smartctl fehlt im Image».

**Datenschutz der Logs:** Vor der Antwort entfernt der Agent IPv4/IPv6-, MAC- und E-Mail-Adressen, Tokens, Schlüssel und
Passwörter (`key=…`, `password: …`, `Bearer …`, `user:pass@`), Benutzernamen (`user …`, `for invalid user …`),
Heimordner (`/home/<user>`), Datei- und Ordnernamen unter den Freigaben (`/mnt/user/<Freigabe>/…`) und lange Zufallsketten.
Gleiche Meldungen werden zu einer Zeile mit Anzahl. Der Agent schickt Logs nie von selbst irgendwohin; nur die App holt
sie mit Token.

**Weitere Schalter:** `NZBLAB_BIND` (Standard `0.0.0.0`; z. B. die LAN-Adresse oder `127.0.0.1` hinter einem Proxy, IPv6 `::`),
`NZBLAB_AUTH_FAILS` (Token-Bremse, Standard 10 pro Minute).

**Gesundheitsprüfung:** Der `HEALTHCHECK` im Image fragt `/api/health`: Er schlägt auch fehl, wenn die Messschleife hängt
(letzte Messung älter als 2 Intervalle + 15 s). `/api/ping` bleibt billig und ändert sich nicht im Verhalten.

**Getestete Architekturen:** Das Image wird für amd64 und arm64 gebaut (GitHub Actions, Smoke-Test auf amd64). Im Dauereinsatz:
Unraid 7.x (amd64). 1.15.0 ist mit den Tests und einem Probelauf auf Unraid (amd64) geprüft, noch nicht als Image. arm64 (Raspberry Pi 4/5 mit 64-Bit-System) wird gebaut, aber nicht auf echter
Hardware geprüft; 32-Bit-ARM (armv7) gibt es nicht.

## Ab 1.16.0: Dienste finden («Alles in einer Minute verbunden»)

`GET /api/discover` (nur mit Token) sucht **nur lesend** in den appdata-Ordnern nach bekannten Diensten und liest ihre
API-Keys aus den Einstellungsdateien. Die App kann damit «7 Dienste gefunden, alle verbinden?» anbieten. Der Ordnername ist
egal (`binhex-sonarr`, `sonarr`, `tv4k` …), erkannt wird über den Inhalt und den Namen.

**Standard an.** `DISCOVER_KEYS=0` schaltet das Auslesen der Schlüssel aus: Der Endpunkt meldet dann nur, welche Dienste
es gibt (`apiKey: null`, `code: "keys_off"`), `features` enthält nur `discover`, nicht `discover_keys`.

| Dienst (`service`) | Datei | Gelesen | Port ohne Angabe |
|---|---|---|---|
| `sonarr`, `radarr`, `prowlarr` | `config.xml` | `ApiKey`, `Port`, `SslPort`/`EnableSsl`, `UrlBase`; Typ aus `<InstanceName>`, sonst Ordnername, sonst Standard-Port | 8989, 7878, 9696 |
| `sab` (SABnzbd) | `sabnzbd.ini` | `[misc]` `api_key`, `port`, `https_port`/`enable_https`, `url_base` | 8080 |
| `tautulli` | `config.ini` | `[General]` `api_key`, `http_port`, `http_root`, `enable_https` | 8181 |
| `seerr` (`variant` overseerr, jellyseerr, seerr) | `settings.json` | nur `main.apiKey` | 5055 |
| `plex` | `Preferences.xml` im Ordner «Plex Media Server» | **nichts**: die Datei wird nie geöffnet (Konto-Token) → «Token selbst eintragen» | 32400 |
| `jellyfin` (`variant` jellyfin, emby) | `network.xml` (oder Ordnername) | `InternalHttpPort`, `BaseUrl`, `EnableHttps`; ein API-Key steht in keiner Datei → «in der Weboberfläche erstellen» | 8096 |

Lidarr, Readarr, Whisparr und andere Dienste werden nicht gemeldet.

**Wo gesucht wird:** `DISCOVER_ROOTS` (Container-Pfade, Komma), sonst `<FILES_ROOT>/appdata` (Unraid: `/mnt/user` →
`/host/mnt/user`, schon in der Vorlage), wenn es den nicht gibt `<FILES_ROOT>/apps` (TrueNAS, eigene Datasets wie in
`compose/truenas.yml`) und `<FILES_ROOT>/docker` (Synology `/volume1/docker`). TrueNAS-Katalog-Apps liegen unter
`/mnt/.ix-apps/app_mounts`: bei Bedarf zusätzlich nur lesend einbinden und `DISCOVER_ROOTS` setzen. Der Agent hat kein
`docker.sock` und braucht keins.

**Grenzen und Sicherheit:**

- höchstens 3 Ordner-Ebenen unter jedem Container-Ordner, 3000 Einträge pro Container-Ordner (über alle Ebenen), 500 Ordner
  unter jeder Wurzel (gleiche Ordnernamen unter einer zweiten Wurzel zählen nur einmal); grosse Datenordner (Cache, Logs,
  Backups, MediaCover, Metadata, Datenbank-Ordner wie `pgdata`, `mysql`, `model-cache` …) und versteckte Ordner werden nicht
  betreten; Postgres-/MariaDB-Container werden ganz übersprungen
- höchstens rund 8 Sekunden Suchzeit (App-Zeitlimit 10 s), danach kommt, was bis dahin gefunden wurde, mit `partial: true`; die
  Antwort ohne Schlüssel (`?keys=0`) merkt sich der Agent 60 Sekunden
- `partial: true` heisst ab 1.16.1: Zeitbudget oder Ordnerzahl erreicht, oder ein Ordner, der nach einem bekannten Dienst
  aussieht, wurde abgebrochen, bevor seine Einstellungsdatei gefunden war. Ein abgebrochener fremder Ordner (z. B. Nextcloud
  `data`) zählt nicht mehr
- Symlinks werden nie verfolgt (Ordner nicht, Dateien nicht: `O_NOFOLLOW`), nur normale Dateien bis 1 MB
- XML mit `DOCTYPE`/`ENTITY` wird nicht ausgewertet; Schlüssel nur, wenn sie wie ein Schlüssel aussehen (16–128 Zeichen
  `A-Z a-z 0-9 _ - + / =`)
- Antwort nur mit diesen Feldern, keine anderen Dateiinhalte (Passwörter, Plex-Token, Usenet-Zugänge bleiben liegen), kein
  Pfad ausser dem Ordnernamen; `Cache-Control: no-store`
- höchstens 10 Abrufe pro Minute (für alle zusammen), danach `429` mit `Retry-After`; falsche Tokens bremst weiterhin die
  Token-Bremse
- Schlüssel stehen nie im Log, auch nicht im Fehlerfall (`500 {"error": "Suche fehlgeschlagen"}` ohne Einzelheiten), nicht
  in «Heute», im Verlauf oder in der Weboberfläche
- liest der Agent als PUID 99 einen Ordner oder eine Datei nicht: `code: "unreadable"` (ab 1.16.1 auch für nicht lesbare
  Ordner, wenn der Ordnername nach Sonarr, Radarr, Prowlarr, SABnzbd, Tautulli, Seerr, Plex, Jellyfin oder Emby aussieht), Abhilfe PUID=0
- Tautulli wird nur erkannt mit Abschnitt `[PMS]` in `config.ini` oder «tautulli» im Ordnernamen (ab 1.16.1; Headphones,
  LazyLibrarian, Mylar & Co. haben auch `[General] http_root`, sind aber keine); Emby und Jellyfin vor 10.9 lesen den Port aus
  `system.xml` (`HttpServerPortNumber`)
- `?keys=` akzeptiert `0`, `false`, `no`, `off`, `nein` (ab 1.16.1, wie die Umgebungsvariablen); `keys` in der Antwort zeigt, ob
  wirklich Schlüssel mitkommen

**Antwort:**

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

`code`: `ok`, `keys_off`, `plex_token`, `jellyfin_key`, `emby_key`, `unreadable`, `broken` (Datei kaputt oder ohne gültigen
Schlüssel). `port` ist bei SSL der HTTPS-Port, `httpPort` dann der unverschlüsselte. **Wichtig:** Die Ports gelten im
Container. Im Bridge-Netz kann Docker sie am NAS anders abbilden (z. B. 8989 → 18989); das muss die App über die
Containerliste des NAS (Portzuordnung) lösen. `partial: true` = eine Grenze wurde erreicht (siehe oben).

**Teststand:** nur mit erfundenen Testdaten geprüft (`test_fixtures/discover/`, Unit-Tests), **noch nicht gegen echte appdata**
und nicht auf echtem Unraid.

## Ab 1.17.0: Grenzwerte, Synology-Plattennamen, Mover-Stand aus dem Protokoll

Gleiche Schnittstelle (`api_level` 16), drei Ergänzungen:

- **`limits` in `/api/now`:** Grenzwert je Sensor laut hwmon (`temp*_crit`, sonst `temp*_max`, nur 40–150 °C). Die App
  nimmt daraus die Grenze der CPU (vor der Hersteller-Tabelle); fehlt der Wert, bleibt alles wie vorher.
- **Synology DSM 7:** Platten heissen dort `sata1…`, `sas1…` oder `usb1…` statt `sda`. Der Agent erkennt diese Namen jetzt bei
  Plattenzugriffen (`disks`, `disk_rw`, Platten-Wecker), Plattentemperaturen (`/api/disktemps`) und als SMART-Gerät
  (`/dev/sata1`). Nur mit erfundenen Testdaten geprüft, noch nicht auf einem echten Synology.
- **`mover` in `/api/logs`** (nur mit `NZBLAB_LOGS=1` und eingebundenem `/var/log`): letzter Mover-Start und letztes Ende aus
  syslog/messages (`{"state": "running"|"finished"|null, "started": t, "ended": t}`, Start ohne Ende höchstens 12 Stunden
  alt). Die App meldet «Mover hängt» nur, wenn das Protokoll den Lauf noch offen zeigt und 30 Minuten nichts verschoben wurde;
  ohne Protokoll bleibt es eine beschriftete Schätzung. Keine Dateinamen, keine Pfade.

**Teststand:** Unit-Tests mit Testdaten (`test_agent.py`); nicht auf echtem Unraid oder Synology geprüft. Sicherheits-Audit und
Code-Review des Diffs seit 1.16.2 am 11.11.2026: zwei kleine Fehler behoben (Grenzwert: `max`, wenn `crit` unbrauchbar ist;
Mover: doppelte Startzeile in syslog und messages setzt das Ende nicht zurück).

## Ab 1.16.1: Korrekturen aus Sicherheitsaudit und Code-Reviews

Gleiche Schnittstelle (`api_level` 16). Geändert:

- **Token:** `NZBLAB_AGENT_TOKEN` und `token.txt` unter 32 Zeichen (oder mit Leer-/Steuerzeichen) lehnt der Agent mit klarer
  Meldung im Log und `503`/`token_error` ab (nie mit dem Token im Log); `token.txt` wird mit `O_NOFOLLOW` geöffnet.
- **`/api/crashes`:** nur noch mit Token, auch vom NAS selbst (Audit Lauf 2: ein Reverse-Proxy auf dem NAS ohne
  Weiterleitungs-Kopf sah sonst wie Loopback aus); alle Felder werden beim Speichern geschwärzt, kurze Felder auf 200 Zeichen gekürzt.
- **Toppreise:** `og:title` wird auf 500 Zeichen gekappt und linear geteilt (keine quadratische Regex mehr).
- **Dienste finden:** Tautulli-Erkennung strenger, `partial` nur noch bei abgeschnittenem Dienst-Ordner, Datenbank-Ordner
  übersprungen, `unreadable` auch für nicht lesbare Ordner, Zeitbudget 8 s, 60-s-Zwischenspeicher ohne Schlüssel, `?keys=0|false|no|off`
  mit ehrlichem `keys`, Emby-/Jellyfin-Port aus `system.xml`.

### Ab 1.16.1: Härtung (Audit Lauf 2)

- **Kopfzeilen:** Alle Antworten (JSON und Fehlerseiten wie `501` für `HEAD`) mit `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` und
  `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` (die Browser-Seite behält ihre eigene CSP und
  `no-cache`). Server-Kopf ohne Python-Version.
- **Browser-Seite:** CSP ohne `'unsafe-inline'` für Skripte; erlaubt ist nur der SHA-256 des eigenen Skripts (beim Start aus
  `web.html` berechnet). Stile bleiben `'unsafe-inline'` (Balken und Farben über `style="…"`; alle Daten laufen durch `esc()`).
  `X-Frame-Options: DENY`.
- **Token:** zusätzlich mindestens 8 verschiedene Zeichen (`aaaa…`, `1234…` sind trotz 32 Zeichen leicht zu erraten).
- **`/api/hello`:** höchstens 30 Anfragen pro Minute und Absender (danach `429` mit `Retry-After`; die App wertet das als
  «nicht erkannt», nicht als «fremdes Gerät»). Der Beweis bleibt `HMAC-SHA256(Token, "nzblab-hello:" + Nonce)`: Eine langsame
  Ableitung (PBKDF2/scrypt) würde alle bisherigen App-Versionen brechen (sie rechnen genau diesen Wert) und hilft nur, wenn der
  alte Beweis ganz wegfällt. Schutz gegen Offline-Raten sind darum die Mindestlänge, die Zeichenvielfalt und der zufällige
  Standard-Token (192 Bit).
- **Verbindungen:** höchstens 32 gleichzeitig offene Verbindungen je Absender und 256 insgesamt (`NZBLAB_MAX_CONN_PER_IP`,
  `NZBLAB_MAX_CONN`); halbe Anfragen (Slowloris) belegen so nicht beliebig viele Fäden. Token-Bremse räumt höchstens einmal
  pro Sekunde auf.
- **Sammler:** `README` in Backup-Ordnern nur die ersten 300 Bytes, kaputtes UTF-8 bricht die Backup-Suche nicht mehr ab;
  E-Mail-Regel beim Schwärzen linear, Logzeilen vor dem Schwärzen auf 4000 Zeichen gekürzt; Geräteliste: höchstens 64 neue
  Geräte je Suchlauf (der erste Suchlauf übernimmt alle, ohne Meldung), 5 Einzelmeldungen plus eine Sammelmeldung, höchstens 2000 Geräte, Namen nur druckbar (253 Zeichen);
  Syslog-Zeilen für «Heute» werden geschwärzt (key=wert, Bearer, Cookie …); Cookie-Zeilen werden ganz geschwärzt.
- **/config:** neue Dateien nur noch für den Agenten lesbar (`umask 077`: Datei-Index, Verlauf, Absturzberichte).
  Bestehende Dateien behalten ihre Rechte.
- **Container:** Vorlagen (Compose, TrueNAS, Synology, Unraid) mit `cap_drop: ALL` und nur `CHOWN`, `DAC_OVERRIDE`, `FOWNER`,
  `SETUID`, `SETGID` (Start als root für `/config`, dann Wechsel auf PUID ohne Rechte), dazu wie bisher `read_only`,
  `no-new-privileges`. SMART braucht zusätzlich `SYS_RAWIO`. Kein `USER` im Dockerfile: bestehende Installationen besitzen
  `/config` noch als root, und der SMART-Helfer braucht root. Basis-Image per Digest festgenagelt.
- **Workflow:** Rechte nur im Job (`contents: read`, `packages: write`), Actions auf Commit-SHA, Checkout ohne gespeicherte
  Zugangsdaten, kein `pull_request_target`; das Image wird zuerst lokal gebaut und mit den Rechten der Vorlagen getestet,
  erst dann werden Versions-Tag und `latest` hochgeladen. Der Token wird im Log nie ausgegeben.

### Ab 1.16.1: Audit Restteile

- **WireGuard:** Konfiguration höchstens 256 KiB je Datei gelesen; Kommentare oder `NAME`-Felder, die nach Schlüssel aussehen
  («Private Key», «Preshared», «PSK», lange Base64-Folge; «Mein privater Laptop» bleibt ein Name), werden nie Peer-Namen; Fehler beim Auswerten melden nur `reason: "error"`
  ohne Text. Private und Preshared Keys verlassen den Agent wie bisher nie.
- **Doppelte Dateien:** Teil-Prüfsummen nur für echte Dateien innerhalb der Freigaben (`O_NOFOLLOW`, kein Symlink, keine Pipe,
  die `open()` sonst für immer blockierte); solche Einträge gelten als nicht lesbar.
- **Auslöser:** Speedtest und Test-Push höchstens einmal gleichzeitig (sonst `409` «läuft schon»); Gerätesuche und
  Index-Neuaufbau prüfen und setzen «läuft» in einem Schritt, ein zweiter Auslöser antwortet `202` mit `started: false`.
- **Preise:** nur `https` auf öffentliche Adressen, auch bei Weiterleitungen (höchstens 5; nie ins Heimnetz, Loopback,
  Link-local, CGNAT, auch nicht über NAT64 oder IPv4 in IPv6; DNS-Fehler = blockiert); Seite höchstens 4 MB in 60 Sekunden; Links aus fremden Seiten nur `https://`.

**Bestehende Installationen:** Nichts muss geändert werden. Wer einen eigenen Token mit weniger als 8 verschiedenen Zeichen
gesetzt hat, bekommt `503` mit Grund und muss ihn ersetzen (oder leer lassen). Wer `/api/crashes` von Hand ohne Token
abgefragt hat: `curl -H "Authorization: Bearer $(cat /config/token.txt)" http://127.0.0.1:8099/api/crashes`. Die neuen
Container-Rechte gelten nur für neue Vorlagen; eigene Compose-Dateien und gespeicherte Unraid-Vorlagen bleiben, wie sie sind
(freiwillig übernehmen: die Zeilen `cap_drop`/`cap_add` bzw. die «Extra Parameters» aus der neuen Vorlage).

## Übersicht im Browser (ab 1.4.0)

`http://<NAS-IP>:8099` (z. B. `http://192.168.1.10:8099`) im Browser öffnen (PC, Tablet, Handy im WLAN oder über
WireGuard). Beim ersten Mal den Token einfügen (siehe «Token» oben); er bleibt nur in diesem Browser gespeichert, «Abmelden» löscht ihn.

Zu sehen, live alle 5 Sekunden: CPU und CPU-Temperatur mit Verlauf (12 Std.), Arbeitsspeicher, Netzwerk, Array mit
Platten, Internet-Tempo (Knopf «Jetzt messen»), alle Temperaturen, meiste Last, Festplatten-Zugriffe, Grafikchip,
Geräte im Heimnetz und Datei-Index. **Nur Anzeige**: Steuern (Container, Downloads, Array) geht in der App.

Ganz oben **«Heute auf dem NAS»** (letzte 24 Stunden, Filter Probleme · Downloads · Unraid-Meldungen · Container · System):
Unraid-Meldungen, fertige und fehlgeschlagene Downloads (aus dem Verlauf von SABnzbd in appdata, nur lesend), abgestürzte
oder gestoppte Container, Fehler im Systemprotokoll (gleiche Meldungen zusammengefasst) und die Alarme des Agenten
(Hitze, Array, neue Geräte, angelaufene Platten, Speedtest). Ohne Passwörter oder API-Keys. Dafür zwei zusätzliche
Pfade, beide **nur lesen**: `/tmp/notifications` → `/host/notifications` und `/var/log` → `/host/log`.
Fehlt eine Quelle, sagt die Seite unten in der Karte, welcher Pfad fehlt.

Die Seite selbst braucht keinen Token, alle Werte schon. Sie lädt nichts aus dem Internet (strenge Content-Security-Policy).
Nicht ins Internet freigeben; von unterwegs über WireGuard.

Ab 1.9.0: Der Token gilt nur noch für den offenen Browser-Tab (`sessionStorage`, weg nach dem Schliessen). Die Seite
aktualisiert sich alle 30 Sekunden, der Verlauf von CPU und Temperatur zeigt 24 Stunden. Neu dazu die Karten
**«Cache & Mover»** (läuft der Mover, Belegung der Freigaben auf dem Cache) und **«Agent»** (Version, misst seit,
Anzahl Absturzberichte der App, letzte Aktualisierung). Die HTML-Seite selbst enthält keine Werte vom NAS.

## Schnittstelle

Alle Anfragen ausser `/api/ping`, `/api/health` und `/api/hello` (ab 1.16.1 höchstens 30 pro Minute und Absender) mit `Authorization: Bearer <Token>`. Falscher Token: `401`;
ab 1.15.0 nach 10 falschen Tokens in einer Minute von derselben Adresse `429` mit `Retry-After` für 5 Minuten
(`NZBLAB_AUTH_FAILS`). Anfragen über 64 KiB: `413` (Absturzberichte bis 128 000 Bytes), `Transfer-Encoding`: `411`. Konnte der
Agent selbst keinen Token laden (ab 1.13.0, `token.txt` leer/kaputt/nicht lesbar): `503` mit `token_error`.

Ausnahme ohne Token: `GET /api/prices/watch` (nur Plattenserien), nur aus privaten Netzen **und** nur mit IP-Adresse oder
lokalem Namen im `Host`-Header (`nas`, `*.local`, `*.lan`, `*.home.arpa`, `*.internal`), damit fremde Websites sie nicht über
DNS-Rebinding lesen können. `GET /api/crashes` (Absturzberichte der App) braucht ab 1.16.1 immer den Token, auch vom NAS
selbst. Die App sendet den Token ohnehin. Neue Berichte schwärzt der Agent beim Speichern (Zugangsdaten in Adressen,
Bearer-Tokens, Cookie-Zeilen, Schlüssel und Passwörter, lange Zufallsketten). Der Token wird in konstanter Zeit verglichen (`secrets.compare_digest`); `token.txt` und `settings.json` haben Rechte 600.

| Pfad | Inhalt |
|---|---|
| `GET /api/ping` | lebt der Agent? Ab 1.13.0 dazu `config_persistent` (`false` = `/config` geht beim Update verloren), ab 1.15.0 `api_level` und `sampling_ok`, sonst nichts |
| `GET /api/hello?nonce=<32 Hex-Zeichen>` | ohne Token, ab 1.11.0: Heimnetz-Prüfung der App, siehe unten |
| `GET /api/info` | Version, Hostname, Messintervall, seit wann gemessen wird; ab 1.13.0 `config_persistent`, `token_source` (`env`, `file`, `new`) und `warnings` (`[{"code": "config_not_persistent", "de": …, "en": …}]`, mit Lösung); ab 1.14.0 `host_os` (os-release eingebunden) und `host_fs` (Ordner unter `/host/fs`); ab 1.15.0 `api_level` (Zahl, 15), `features` (eingeschaltete Zusätze: `boots`, `disktemps`, `health`, `smart`, `backups`, `raid`, `net`, `logs`; ab 1.16.0 `discover` und `discover_keys`, `api_level` 16), `last_boot`, `boots_7d`, `sampling_ok` |
| `GET /api/now` | aktuelle Werte (höchstens alle 2 s neu gemessen) |
| `GET /api/history?hours=24` | bis 192 h alle Messungen, darüber Stundenwerte (`res=hour` erzwingt Stundenwerte) |
| `GET /api/today?hours=24` | «Heute»: Ereignisse aller Quellen, neueste zuerst, mit Zusammenfassung und welche Quellen fehlen |
| `GET /api/prices` | Plattenpreise (ab 1.5.0): pro Platte im NAS der Preis des eigenen Modells (ab 1.12.0, mit günstigster Alternative), sonst pro Grösse bester Preis mit Shop, Schweizer Shops, Ausland, Verlauf, Tief/Hoch 90 Tage, «günstig» |
| `GET /api/prices/watch` | ohne Token, nur aus privaten Netzen: welche Plattenserien die App zusätzlich beobachtet (für eine externe Preisliste, siehe unten) |
| `POST /api/config` | `ntfy_url`, `price_alerts` (`{"hdd28": 900}`), `price_watch` (`[{"series": "ironwolfpro", "tb": 16}]`), `price_country` (`"DE"`, ab 1.10.0; mit `price_country_if_unset: true` nur, wenn noch keins gewählt ist). `price_alerts` und `price_watch` ersetzen jeweils die ganze Liste; ein leerer Wert (`{}`, `[]`, `null`) löscht alle Preisgrenzen bzw. beobachteten Serien |
| `GET /api/wakes?hours=168` | Platten, die nach `DISK_IDLE_MINUTES` (30) Ruhe angelaufen sind, mit den Programmen in diesem Moment |
| `GET /api/files/tree?path=` · `GET /api/files/largest?path=&limit=` | Speicher-Analyse (ab 1.7.0) |
| `GET /api/files/duplicates?min_mb=100&limit=100` | doppelte Dateien (ab 1.8.0), siehe unten |
| `GET /api/inventory` | Medien-Bestand: Filme, Serien, Lücken, neu seit dem letzten Index (ab 1.8.0) |
| `GET /api/cache/shares` | Belegung jeder Freigabe auf dem Cache (ab 1.8.1; Pfad `/mnt/cache` → `/host/mnt/cache` nur lesen, mehrere Pools mit `CACHE_ROOTS`) |
| `GET /api/discover` | ab 1.16.0: gefundene Dienste mit Port, SSL, URL-Basis und API-Key (siehe «Ab 1.16.0»); `DISCOVER_KEYS=0` oder Abfrage `?keys=0` ohne Schlüssel (`apiKey: null`, Codes unverändert; die App zählt so nur), `Cache-Control: no-store`, höchstens 10 Abrufe pro Minute (`429`) |
| `GET /api/appdata` | Grösse jedes Container-Ordners unter appdata (ab 1.8.2; misst im Hintergrund höchstens alle 6 Std., braucht `/mnt/user` → `/host/mnt/user` nur lesen) |
| `GET /api/wireguard` | WireGuard-Tunnel und Geräte, ohne Schlüssel (ab 1.8.0) |
| `GET /api/host` | Linux-Server (ab 1.14.0): `hostname`, `os` (aus `/host/os-release`: `name`, `version`, `id`, `pretty`, `available`), `kernel`, `uptime_s`, `boot_time`, `cpu` (`model`, `cores`, `threads`), `load` (1/5/15 Min.), `mem` (Bytes), `net` (Schnittstellen mit MAC, Zustand, Tempo, IPv4) |
| `GET /api/filesystems` | Linux-Server (ab 1.14.0): `filesystems` (`device`, `mount`, `fstype`, `size`, `used`/`free` nur bei `measured: true`, `label`), `disks` (`dev`, `model`, `size`, `rotational`, `kind`, `temp`), `mounts_from` (`host` oder `container`), `measured` (Namen unter `/host/fs`) |
| `GET /api/health` | ohne Token, ab 1.15.0: `200 {"ok": true, "sample_age_s", "interval"}`, `503`, wenn die Messschleife länger als 2 Intervalle (+15 s) steht (Docker-Gesundheitsprüfung) |
| `GET /api/boots?days=30` | ab 1.15.0: `boots: [{boot_time, previous_uptime_s (oder null), planned: "unknown"}]`, neueste zuerst |
| `GET /api/disktemps?days=7&res=` | ab 1.15.0: `devices: {sda: {model, latest, points}}`, `res` = `raw` (bis 2 Tage, Punkte `[t, °C]` alle 5 Min.), `hour` (bis 14 Tage) oder `day` (Punkte `[t, Mittel, Höchstwert]`), `fields`, `interval_s` |
| `GET /api/net?days=1&res=` | ab 1.15.0: `enabled`, `targets` (`gateway`, `internet`), `latest` (`{gateway: {t, rtt, loss}}`), `series` (`{gateway: {rtt: [...], loss: [...]}}`, ms bzw. %), `res`/`fields` wie oben |
| `GET /api/smart` | ab 1.15.0, nur mit `NZBLAB_SMART=1`: `enabled`, `checked`, `error`, `disks: [{device, device_name, model_name, serial_number, firmware, capacity, rotational_speed, device_protocol, smart_passed, power_on_hours, power_cycle_count, temp, reallocated, pending, uncorrectable, crc, media_errors, percentage_used, unsafe_shutdowns, standby, attrs}]` (`attrs` wie Scrutiny: nach ID `"5"` bzw. bei NVMe nach Namen `media_errors`, `percentage_used` …, je `raw_value`) |
| `GET /api/smart/history?days=90` | ab 1.15.0: ein Tageswert pro Platte (letzter Stand, Temperatur = Höchstwert): `disks: [{serial, model, device, rows: [{day, realloc, pending, uncorr, crc, media, temp_max, hours, wear, passed}]}]`, 2 Jahre |
| `GET /api/backups` | ab 1.15.0: `backups: [{kind (restic, borg, kopia, rsync, log, unknown), name, path, last_success, last_error, error_text, snapshots, source, found_by (path, auto)}]`, `checked`, `paths`; unbekannt = `null` |
| `GET /api/raid` | ab 1.15.0: `pools: [...]` (mdadm: `level`, `devices`, `total`, `active`, `degraded`, `sync {action, percent, finish_min}`; ZFS: `state`; Btrfs: `profiles`, `devices` mit `missing` und `errors`; Unraid: Verweis auf `/api/array`), `degraded`, `sources {mdadm, zfs: kstat/zpool/none/unavailable, btrfs}` |
| `GET /api/logs?hours=24&max=200` | ab 1.15.0, nur mit `NZBLAB_LOGS=1`: `lines: [{t, level (err, warn), src, text, n}]`, `files`, `redacted: true`; sonst `403` |

## Heimnetz-Prüfung (ab 1.11.0)

Bevor die App Schlüssel über `http://` an eine Adresse im Heimnetz schickt, prüft sie, ob dort wirklich das eigene NAS
antwortet (ein fremdes WLAN kann denselben Adressbereich haben, z. B. 192.168.1.x). Dazu fragt sie ohne Token
`GET /api/hello?nonce=<32 Zeichen 0-9a-f>`. Der Agent antwortet mit:

- `id`: feste Zufalls-Kennung, beim ersten Start in `settings.json` angelegt (keine Namen, keine Adressen)
- `proof`: HMAC-SHA256 mit dem Token als Schlüssel über `nzblab-hello:<nonce>`, als Hex-Text
- `version`

Die App rechnet denselben Wert mit ihrem gespeicherten Token nach und vergleicht in konstanter Zeit. Der Token selbst
verlässt weder App noch Agent. Andere Nonce als 32 Zeichen `0-9a-f`: 400. Fremder Name im `Host`-Header: 403 (wie bei den
anderen Endpunkten ohne Token). Den Agent deshalb weiterhin nicht ins Internet freigeben.

## Testen

```bash
python3 test_agent.py        # im Ordner mit nzblab_agent.py (im App-Repo: agent/)
```

## Datei-Index (ab 1.1.0)

Mit dem Pfad `/host/mnt/user` (nur lesen) baut der Agent alle 6 Stunden einen Index aller Ordner und Dateien der Freigaben
(ohne `appdata`, `system`, `domains`, `isos`; änderbar mit `FILES_EXCLUDE`, Intervall mit `FILES_EVERY_HOURS`).
Die App sucht dann direkt darin. Endpunkte: `GET /api/files/status`, `GET /api/files/search?q=…`, `POST /api/files/refresh`.


## Plattenpreise selbst holen (ab 1.10.0)

Ab 1.10.0 holt der Agent die Preise **selbst** (nur Python-Standardbibliothek), einmal am Tag ab 06:00, für die
Plattengrössen in diesem NAS (aus `/sys/block`, ab 0.5 TB, HDD/SSD/NVMe getrennt) und die in der App beobachteten Serien.
Das Land wählt man in der App (Plattenpreise › Land); die App schickt beim Einrichten das Land ihrer Spracheinstellung,
ändert aber nie ein schon gewähltes. Ohne App: Variable `PRICE_COUNTRY` (z. B. `DE`). Ohne Land holt der Agent nichts.

| Land | Quelle | Anfragen pro Tag |
|---|---|---|
| CH | Toppreise.ch (Suche nach Preis pro TB) | eine pro Grösse, dazu eine (selten zwei) pro Plattenmodell |
| DE, AT | Geizhals.de / .at, **nur wenn deren robots.txt es erlaubt**; sonst Amazon.de über diskprices.com | eine pro Grösse bzw. eine für alle |
| US, GB, FR, IT, ES, CA, AU, SE, IE, IN | Amazon über diskprices.com (eine Tabelle für alle Grössen) | eine |
| andere | nächster Amazon-Markt (z. B. NL → Amazon.de), sonst Amazon USA | eine |

Höflich: vorher `robots.txt` der Seite lesen (einmal pro Lauf; verbietet sie die Suche, wird nicht abgefragt), ehrlicher
User-Agent `NZBLab-Agent/<Version>`, 20 s Timeout, 5 s Pause zwischen Anfragen (`PRICE_PAUSE_S`), keine Cookies, keine
Wiederholung. Stand 28.10.2026: Geizhals verbietet in seiner robots.txt die Suche (`/?fs=`, `*?*sort=`) für alle Bots,
darum fragt der Agent Geizhals in der Praxis nicht ab.

**Nie erfundene Zahlen:** Ist eine Seite gesperrt (HTTP 403/429), eine Prüfseite («Sichere Verbindung wird überprüft»),
von robots.txt verboten oder hat sie ein unbekanntes Layout, steht bei der Grösse «Preise gerade nicht lesbar» und
`browser_url`: Die App zeigt dann den Knopf «Im Browser suchen». Beträge stehen in der Währung des Landes (`currency`);
das Feld heisst aus Kompatibilität weiter `chf`.

Ergebnis pro Land in `/config/prices-<Land>.json` (Verlauf 400 Tage). Für die Schweiz hat eine **frische** externe `prices.json`
(höchstens 36 Stunden alt, siehe «Externe Preisliste») Vorrang; ist sie älter oder fehlt sie,
holt der Agent selbst. Die Tests (`test_agent.py`, Klasse `PriceFetchTest`) prüfen die Leser mit Beispielseiten in
`test_fixtures/` (Toppreise-, Geizhals- und diskprices-Seiten nachgebaut nach dem bekannten Aufbau; Sperr-/Prüfseiten und
robots.txt echt, gekürzt), ohne Netz.

`GET /api/prices` liefert dazu: `country`, `country_set`, `currency`, `countries` (Auswahl für die App) und pro Grösse
`unreadable`, `browser_url`.

### Preis des eigenen Modells (ab 1.12.0)

Bis 1.11.0 war der Preis «die günstigste Platte gleicher Grösse», also oft eine Exos statt der IronWolf Pro im NAS. Ab
1.12.0 liest der Agent das **Modell jeder Platte** aus sysfs (nur lesen, nie die Seriennummer):

- SATA: voller Modellname aus der ATA-Information `device/vpd_pg89` (nur die Modell-Bytes), sonst `device/model`
  (höchstens 16 Zeichen, z. B. `ST28000NT000-4AB`); NVMe: `device/model` bzw. `/sys/class/nvme/nvmeX/model`.
  Funktioniert mit `/host/sys` (Container) und `/sys`.
- Teilenummer und Serie per Tabelle: Seagate `ST…NT/NE` IronWolf Pro, `VN` IronWolf, `NM` Exos, `DM` BarraCuda,
  `VX/VE` SkyHawk; WD `EFPX/EFZX/EFRX` Red Plus, `EFAX` Red (SMR), `FFBX/KFBX/KFGX` Red Pro, `PURZ` Purple,
  `WUH…/HUH…` Ultrastar; Toshiba `HDWG` N300, `HDWR` X300, `HDWT` S300, `MG…` Enterprise; SSDs nach Name
  (Samsung 990 PRO, WD Red SN700 …). Garantie aus der Tabelle, nur wo belegt (IronWolf Pro, Exos, Red Pro, Ultrastar,
  MG, 990 PRO 5 Jahre; IronWolf, Red Plus, N300 3; BarraCuda 2). Quellen stehen im Code bei `MODEL_SERIES`.
- Mehrere gleiche Platten = ein Eintrag (`disks` listet alle). Mehrere Modelle gleicher Grösse: das häufigste behält
  den Schlüssel `hdd3`, die anderen heissen z. B. `hdd3-wd30ezrx` (Preisgrenzen aus der App gelten weiter).

Ablauf pro Modell: zuerst die **genaue Teilenummer** suchen (Toppreise `?q=ST28000NT000`, springt meist direkt auf die
Produktseite); zählt nur, was die Teilenummer im Namen trägt (`match: exact`). Nichts gefunden: Suche nach Serie und
Grösse (`Seagate IronWolf Pro 28TB`), nur Angebote derselben Serie (`match: series`, «gleiche Serie»). Unbekannte Modelle:
Suche nach dem Modellnamen selbst. Zusätzlich wie bisher die Suche nach der Grösse: deren günstigstes Angebot eines
**anderen** Modells erscheint als `alternative`, nur wenn es billiger ist als das eigene. Jede Suche höchstens einmal
pro Tag, gleiche Regeln (robots.txt, Pausen, «Preise gerade nicht lesbar»). Ohne lesbares Modell: wie bis 1.11.0.

Neue Felder pro Eintrag in `GET /api/prices`: `model` («Seagate IronWolf Pro 28 TB»), `part` («ST28000NT000»), `series`,
`warranty_years`, `match` (`exact`/`series`), `match_label` («exakt»/«gleiche Serie»), `alternative` (Angebot mit `model`),
`alt_search_url`. `best`, Verlauf, «günstig» und Preisgrenzen gelten für das eigene Modell. Alte `prices-<Land>.json`
laufen weiter: der alte Verlauf (günstigste Platte) wird beim ersten Lauf zu `alt_history`, bis dahin erscheint der
alte Preis als Alternative. Bei einer externen `prices.json` nimmt der Agent den Preis aus `models_now` (Verlauf aus
`model_history`, falls vorhanden) und das dortige `best` als Alternative.

## Externe Preisliste (optional)

Wer Preise lieber selbst sammelt (z. B. mit einem eigenen Skript), legt eine `prices.json` im gleichen Format wie
`/config/prices-CH.json` ab. Der Agent liest sie nur, schreibt nie hinein:

- Variable `PRICES_FILE` = Pfad im Container (z. B. `/host/mnt/user/daten/nzblab-prices/prices.json`), oder
- ohne Variable: die erste Freigabe unter `/host/mnt/user` (alphabetisch), die `nzblab-prices/prices.json` enthält
  (gesucht höchstens alle 6 Stunden).

Welche Serien die App zusätzlich beobachtet, liefert `GET /api/prices/watch` (ohne Token, nur aus privaten Netzen).
«Günstig» heisst: unter der eigenen Grenze, oder tiefster Preis seit 90 Tagen und mindestens 3 % unter dem 30-Tage-Mittel
(ab 7 Tagen Verlauf). Der Agent meldet das per ntfy, die App als Benachrichtigung.


## Doppelte Dateien (ab 1.8.0)

`GET /api/files/duplicates?min_mb=100&limit=100`: Kandidaten sind Dateien mit genau gleicher Grösse (ab `min_mb` MB, aus dem
Datei-Index). Ob sie wirklich gleich sind, prüft der Agent mit einer Teil-Prüfsumme (SHA-1 aus dem ersten und letzten MiB
plus Grösse): Er liest also höchstens 2 MiB pro Datei, auch bei einem 40-GB-Film. Das läuft in einem Hintergrund-Thread mit
tiefer Priorität, höchstens `DUP_MAX_FILES` (2000) Dateien pro Lauf, grösste zuerst. Solange er läuft, ist das Ergebnis
unvollständig (`running: true`, `pending` = noch ungeprüfte Dateien); die App fragt einfach später nochmals. Die Prüfsummen
bleiben in `files.db` gespeichert und gelten, solange Grösse und Änderungszeit gleich bleiben.
`wasted` = Platz, der frei würde, wenn von jeder Gruppe nur eine Datei bliebe (über alle Gruppen).

**Nur lesen:** Der Agent löscht und verschiebt nie etwas. Aufräumen macht man selbst.

**Vorgerechnet (ab 1.13.0):** Die Kandidaten (ab 100 MB) und den Medien-Bestand rechnet der Agent nach jedem Index-Lauf
(und einmal nach dem Start) im Hintergrund; eine Anfrage liest nur noch das fertige Ergebnis (`computed_at`, `building: false`).
Gerechnet wird ausserhalb der Sperre des Index (erst Zeilen lesen, dann verarbeiten), Suche und Speicher-Analyse bleiben schnell.
Fehlt das Ergebnis noch (z. B. kleinere Stufe als 100 MB oder direkt nach dem Start), wartet die Anfrage höchstens 2 Sekunden
und antwortet sonst sofort mit `building: true`, leerer Liste und dem Stand des Index (`status`); die App fragt dann nach.

## Medien-Bestand (ab 1.8.0)

`GET /api/inventory` rechnet direkt auf dem NAS aus dem Datei-Index:

- Film-Ordner heissen `Filme`, `Movies` oder `Film`, Serien-Ordner `Serien`, `Series`, `TV` oder `Shows` (Ebene 1 oder 2, z. B. `Media/Filme`).
  Videos: `mkv mp4 avi m4v ts iso`.
- Jeder Unterordner ist ein Film bzw. eine Serie. Titel, Jahr und IMDb-ID aus dem Ordnernamen, z. B. `Babylon 5 (1994) [imdbid-tt0105946]`.
- Folgen aus dem Dateinamen: `S01E02`, `s1e2`, `S01E01E02`, `1x02`. `S01E001` (drei und mehr Stellen) gilt als durchlaufende
  Nummer wie bei One Piece: Lücken dann über die ganze Nummernfolge (`S01E492`, `S01E780-S01E782`). Staffel 0 (Specials) ohne Lücken.
  `mixed_numbering`: in einer Serie gibt es beides.
- «Neu» vergleicht die letzten zwei Index-Läufe (alle 6 Stunden): Am Ende jedes Laufs merkt sich der Agent die Videodateien des
  vorherigen Laufs (Tabelle `inv_prev`). Nach dem ersten Lauf ist noch nichts «neu» (`previous: 0`).

## WireGuard (ab 1.8.0)

`GET /api/wireguard` zeigt die Tunnel (`wg0` …) und pro Gerät: Name, Endpunkt, erlaubte IPs, letzter Handshake, Daten, online
(Handshake vor weniger als 3 Minuten). **Private und Preshared Keys gibt der Agent nie heraus**, vom öffentlichen Schlüssel
nur die ersten 8 Zeichen als Kennung.

Ehrlich: **Im Standard-Aufbau liefert der Endpunkt nichts** (`available: false`). Die App zeigt dann den Grund:

| `reason` | Bedeutung | Abhilfe |
|---|---|---|
| `no_wg` | Das Image `python:3.12-alpine` enthält das Programm `wg` nicht | `wireguard-tools` im Container nachinstallieren (z. B. `apk add --no-cache wireguard-tools` in `start.sh`) |
| `no_interfaces` | `wg` läuft, sieht aber keinen Tunnel: Im Netzwerktyp «Bridge» hat der Container sein eigenes Netz | Netzwerktyp **Host** |
| `no_permission` | `wg show` wird verweigert | zusätzlich die Capability `NET_ADMIN`: Extra Parameters `--cap-add=NET_ADMIN` (nicht «Privilegiert»). Die Vorlage setzt das bewusst nicht |

Peer-Namen (z. B. «Peer 1», «Laptop») sind optional: Pfad `/boot/config/wireguard` **nur lesen** nach
`/host/boot/config/wireguard` einbinden (anderer Ort: Variable `WG_CONFIG_DIR`). Der Agent liest daraus nur die Namen
(Kommentar im `[Peer]`-Block von `wg0.conf` oder `NAME:n`/`PUBLIC:n` in `wg0.cfg`). Achtung: In diesen Dateien steht auch der
private Schlüssel des NAS. Der Agent gibt ihn nie weiter, aber der Container könnte ihn lesen. Ohne den Pfad fehlen nur die Namen.

## Lizenz

Kostenlos, Quellcode öffentlich einsehbar, unter der **PolyForm Shield License 1.0.0** (`PolyForm-Shield-1.0.0`), siehe
[`LICENSE`](LICENSE) und <https://polyformproject.org/licenses/shield/1.0.0>. Required Notice: Copyright (c) 2026 Tuxxler.

- **Erlaubt:** den Agenten kostenlos nutzen, auch geschäftlich; den Code ansehen, ändern und weitergeben (mit Lizenztext
  und der Zeile «Required Notice»).
- **Verboten:** den Agenten oder Teile davon für ein Produkt verwenden, das mit ihm oder mit NASlab (früher NZBLab) konkurriert.

English: free to use, also commercially, and you may read and change the code; you may not use it to build a competing
product. Full text: [`LICENSE`](LICENSE).
