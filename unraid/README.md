# NASlab Agent (formerly NZBLab-Agent) – Unraid template

Unraid Community Applications (CA) template for the **NASlab-Agent** (this folder: `unraid/` in the agent repo).

| Template | Image | What it does |
|---|---|---|
| [`nzblab-agent.xml`](nzblab-agent.xml) | `ghcr.io/tuxxler/nzblab-agent:latest` | Read-only NAS monitor for the NASlab Android app: temperatures, CPU, GPU, RAM, disk spin-ups, file index. Never changes anything on the server. |

## Install without CA (works today)

1. Unraid › **Docker** › scroll down to **Template repositories**, add the line
   `https://github.com/Tuxxler/nzblab-agent` › **Save**.
2. **Add Container** › *Template*: pick **nzblab-agent**. Check the paths (remove *Cache pool* if you have no pool named `cache`).
   Keep *Data (/config)* → `/mnt/user/appdata/nzblab-agent` (read/write): the agent stores its token and history there;
   without it you get a new token after every update.
3. **Apply.** Open the container **Logs**: the line `NZBLab-Agent Token: …` appears once.
4. App: *Settings › Server connections › NASlab-Agent* › address `http://<unraid-ip>:8099`, paste the token, *Test connection*.

All host paths are mounted **read-only**. Only `/config` (`/mnt/user/appdata/nzblab-agent`) is writable.
After start the agent drops root and runs as `PUID`/`PGID` (99/100). Set `PUID=0` only if you need container names
in the "top load" list or appdata sizes of containers with strict permissions.

Optional and off by default: SMART (`NZBLAB_SMART`), log excerpts (`NZBLAB_LOGS`, also needed for the system log in "Today"), the home-network device scan (`NZBLAB_LAN`: every 30 minutes one empty UDP packet to each address of your /24 network, reverse DNS, push for new devices; your router may report it as a port scan). The optional paths *System log* and *Docker container names* are empty by default. Map `/config` to its own folder (`/mnt/user/appdata/nzblab-agent`), never to the whole `appdata`.

Source code (free, open source), Docker/Compose files and license (GNU AGPL-3.0): <https://github.com/Tuxxler/nzblab-agent>.
