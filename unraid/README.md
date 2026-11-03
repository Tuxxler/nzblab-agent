# NZBLab – Unraid template

Unraid Community Applications (CA) template for the **NZBLab-Agent** (this folder: `unraid/` in the agent repo).

| Template | Image | What it does |
|---|---|---|
| [`nzblab-agent.xml`](nzblab-agent.xml) | `ghcr.io/tuxxler/nzblab-agent:latest` | Read-only NAS monitor for the NZBLab Android app: temperatures, CPU, GPU, RAM, disk spin-ups, file index. Never changes anything on the server. |

## Install without CA (works today)

1. Unraid › **Docker** › scroll down to **Template repositories**, add the line
   `https://github.com/Tuxxler/nzblab-agent` › **Save**.
2. **Add Container** › *Template*: pick **nzblab-agent**. Check the paths (remove *Cache pool* if you have no pool named `cache`).
   Keep *Data (/config)* → `/mnt/user/appdata/nzblab-agent` (read/write): the agent stores its token and history there;
   without it you get a new token after every update.
3. **Apply.** Open the container **Logs**: the line `NZBLab-Agent Token: …` appears once.
4. App: *Settings › Server connections › NZBLab-Agent* › address `http://<unraid-ip>:8099`, paste the token, *Test connection*.

All host paths are mounted **read-only**. Only `/config` (`/mnt/user/appdata/nzblab-agent`) is writable.
After start the agent drops root and runs as `PUID`/`PGID` (99/100). Set `PUID=0` only if you need container names
in the "top load" list or appdata sizes of containers with strict permissions.

Source code (free, publicly viewable), Docker/Compose files and license (PolyForm Shield 1.0.0): <https://github.com/Tuxxler/nzblab-agent>.
