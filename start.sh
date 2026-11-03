#!/bin/sh
# Start des Agenten. Zwei Wege, gleicher Befehl:
#  - eigenes Image ghcr.io/tuxxler/nzblab-agent (Dockerfile): Code liegt im Image unter /agent
#  - altes Setup im offiziellen python:3.12-alpine: Code als Ordner nach /agent eingebunden (nur lesen)
exec python3 -u /agent/nzblab_agent.py
