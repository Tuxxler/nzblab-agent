# NZBLab-Agent: misst Temperaturen, CPU, Grafikchip und Plattenzugriffe auf dem NAS und liefert sie der NZBLab-App.
# Nur Python-Standardbibliothek, keine Pakete aus dem Netz. Lizenz PolyForm Shield 1.0.0 (LICENSE). Bauen (im Ordner mit diesem Dockerfile):
#   docker build -t nzblab-agent .
# Start: siehe docker-compose.yml bzw. unraid/nzblab-agent.xml. Alle Host-Pfade nur lesend, nur /config schreibbar.
# /config immer auf einen Ordner des NAS legen: VOLUME unten ist nur ein anonymer Volume, der beim Neuerstellen ersetzt wird
# (neuer Token). Der Agent warnt ab 1.13.0 im Log, in /api/ping und auf der Browser-Seite, wenn das fehlt.
ARG PYTHON_IMAGE=python:3.12-alpine3.22
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
# root-Rechte weiter (99:100 = nobody:users wie bei Unraid-Freigaben). PUID=0 lässt ihn als root laufen
# (nur nötig für Container-Namen und appdata-Grössen anderer Container mit engen Rechten).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    PORT=8099 \
    PUID=99 \
    PGID=100

WORKDIR /agent
COPY nzblab_agent.py web.html start.sh ./
RUN chmod 0644 nzblab_agent.py web.html && chmod 0755 start.sh && python3 -m py_compile nzblab_agent.py && rm -rf __pycache__

VOLUME /config
EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${PORT:-8099}/api/ping" || exit 1

CMD ["sh", "/agent/start.sh"]
