#!/bin/sh
# Start des Agenten. Zwei Wege, gleicher Befehl:
#  - eigenes Image ghcr.io/tuxxler/nzblab-agent (Dockerfile): Code liegt im Image unter /agent
#  - altes Setup im offiziellen python:3.12-alpine: Code als Ordner nach /agent eingebunden (nur lesen)
# Ab 1.20.0: HTTPS-Zertifikat entsteht beim ersten Start des Agenten selbst (openssl im Image), siehe README
exec python3 -u /agent/nzblab_agent.py
