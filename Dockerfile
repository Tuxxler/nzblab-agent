# NZBLab-Agent: misst Temperaturen, CPU, Grafikchip und Plattenzugriffe auf dem NAS und liefert sie der NZBLab-App.
# Nur Python-Standardbibliothek, keine Python-Pakete aus dem Netz. Einziges Zusatzpaket (ab 1.15.0): smartmontools aus Alpine
# (smartctl, rund 3 MB mit libstdc++), wird nur benutzt, wenn NZBLAB_SMART=1 gesetzt ist. Lizenz PolyForm Shield 1.0.0 (LICENSE). Bauen (im Ordner mit diesem Dockerfile):
#   docker build -t nzblab-agent .
# Start: siehe docker-compose.yml bzw. unraid/nzblab-agent.xml. Alle Host-Pfade nur lesend, nur /config schreibbar.
# /config immer auf einen Ordner des NAS legen: VOLUME unten ist nur ein anonymer Volume, der beim Neuerstellen ersetzt wird
# (neuer Token). Der Agent warnt ab 1.13.0 im Log, in /api/ping und auf der Browser-Seite, wenn das fehlt.
# Ab 1.16.1: Basis-Image per Digest festgenagelt (gleicher Bau für alle, kein still ausgetauschtes Image). Vor jedem Release
# den Digest erneuern (docs/Agent-Bereitstellung.md), sonst fehlen Sicherheits-Updates von Alpine/Python.
ARG PYTHON_IMAGE=python:3.12-alpine3.22@sha256:a190708a2dec1bd18b1decb539f8e8f5407abaa9bf39cacda583f7f8c11db322
FROM ${PYTHON_IMAGE}

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="NZBLab-Agent" \
      org.opencontainers.image.description="Read-only NAS monitor for the NZBLab app: temperatures, CPU, GPU, RAM, disk spin-ups, file index. Never changes anything on the NAS." \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.source="https://github.com/Tuxxler/nzblab-agent" \
      org.opencontainers.image.url="https://tuxxler.github.io/nzblab/agent.html" \
      org.opencontainers.image.documentation="https://tuxxler.github.io/nzblab/en/agent.html" \
      org.opencontainers.image.vendor="NZBLab" \
      org.opencontainers.image.licenses="PolyForm-Shield-1.0.0"

# PUID/PGID: Der Agent legt beim Start als root /config an, gibt den Ordner diesem Benutzer und läuft dann ohne
# root-Rechte weiter (99:100 = nobody:users wie bei Unraid-Freigaben). Standard ist also «ohne root»; kein «USER» hier, weil
# bestehende Installationen /config noch als root besitzen (sonst neuer Token) und der SMART-Helfer root braucht.
# PUID=0 lässt ihn als root laufen (nur nötig für Container-Namen und appdata-Grössen anderer Container mit engen Rechten).
# Mit «user:» im Compose (Linux-Vorlage) startet er gleich ohne root; dann geht SMART nicht (siehe README).
# Ab 1.16.1 (Härtung): Die Vorlagen (Compose, Unraid) geben dem Container statt der Docker-Standardrechte nur noch
# CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID (für /config und den Wechsel auf PUID), dazu read-only und no-new-privileges.
# Nach dem Wechsel auf PUID hat der Agent keine Rechte mehr. Ein «USER» hier würde bestehende Installationen brechen
# (/config gehört noch root, Token wäre nicht lesbar) und den SMART-Helfer verhindern.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    PORT=8099 \
    PUID=99 \
    PGID=100

WORKDIR /agent
# smartctl für das freiwillige SMART-Lesen (NZBLAB_SMART=1, nur «--scan» und «-a», nie Selbsttests). Im selben Image statt eines
# eigenen Tags: ein Image für alle, einfacher zu pflegen; ohne NZBLAB_SMART=1 und ohne «devices» im Compose ruht es.
RUN apk add --no-cache smartmontools
COPY nzblab_agent.py web.html start.sh ./
RUN chmod 0644 nzblab_agent.py web.html && chmod 0755 start.sh && python3 -m py_compile nzblab_agent.py && rm -rf __pycache__

VOLUME /config
EXPOSE 8099

# Ab 1.15.0: /api/health antwortet 503, wenn die Messschleife länger als 2 Intervalle (+15 s) steht. NZBLAB_BIND=Adresse
# ohne 127.0.0.1 (z. B. nur die LAN-Adresse): dann prüft der Healthcheck diese Adresse.
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
    CMD wget -q -O /dev/null "http://$( [ "${NZBLAB_BIND:-0.0.0.0}" = "0.0.0.0" ] || [ "${NZBLAB_BIND}" = "::" ] && echo 127.0.0.1 || echo "${NZBLAB_BIND}" ):${PORT:-8099}/api/health" || exit 1

CMD ["sh", "/agent/start.sh"]
