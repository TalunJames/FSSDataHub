# Host the Tax Database on TrueNAS SCALE

The collector runs on the NAS. SQLite lives on the **sql** dataset. Your Mac can be off.

```
Mac (browser)  →  http://<nas-ip>:3490  →  TrueNAS container
                                        →  /mnt/Seawolf/FogSignal/taxdata/sql/tax.db
```

| Dataset | Mount | Purpose |
|---|---|---|
| `Seawolf/FogSignal/taxdata` | `/mnt/Seawolf/FogSignal/taxdata` | This project (code) |
| `Seawolf/FogSignal/taxdata/sql` | `/mnt/Seawolf/FogSignal/taxdata/sql` | `tax.db`, crawl archive, intake uploads |

Enable SSH if you will deploy from a Mac: **System → Services → SSH**.

## Clone on the NAS (public GitHub)

From a shell on TrueNAS, with the `taxdata` dataset already created:

```bash
cd /mnt/Seawolf/FogSignal/taxdata
git clone https://github.com/TalunJames/FSSDataHub.git .
sh deploy/truenas-setup.sh
```

Later updates: `git pull` in that directory, then re-run `deploy/truenas-setup.sh` so the image rebuilds.

## Deploy from this Mac

```bash
NAS=truenas.local sh deploy/push-to-truenas.sh
```

Use your NAS hostname or IP in place of `truenas.local`. That copies code onto `taxdata` (it never writes into `sql/`), builds the image **on the NAS**, and starts the app.

Or copy the folder onto an SMB share for `taxdata`, then SSH in:

```bash
sh /mnt/Seawolf/FogSignal/taxdata/deploy/truenas-setup.sh
```

## Custom App in the UI

Image: `ghcr.io/talunjames/fssdatahub:latest` (GitHub Container Registry).

If a previous install failed, delete it first. **Apps → Discover → Custom App → Install via YAML**. Name the app `datahub`. Paste `compose.truenas.yml`. The sql dataset must already exist.

Host port is **3490** only (`3490:8080` — 8080 is inside the container, not on the NAS). Open `http://<nas-ip>:3490`.

## Open it

`http://<nas-ip>:3490`

Seed jurisdictions, plan a state, set API keys or a Llama URL under **Keys & crawl**. The file on disk is `/mnt/Seawolf/FogSignal/taxdata/sql/tax.db`.

If you already seeded on the Mac:

```bash
scp tax.db root@truenas.local:/mnt/Seawolf/FogSignal/taxdata/sql/tax.db
```

Stop the app first if it is running, copy the file, start it again.

## Local Llama on the same NAS

If Ollama is another TrueNAS app, set **Local Llama / Ollama → Base URL** to that app’s IP (`http://172.16.x.x:11434`) or `http://host.docker.internal:11434`.

## Always-on

- App shows **Running**; compose uses `restart: unless-stopped`.
- Snapshot `Seawolf/FogSignal/taxdata/sql` — that is the backup of the ledger.

CLI on the NAS:

```bash
docker exec -e TAX_DATABASE_DATA=/data -w /app $(docker ps -qf name=collector) python -m taxdb status
```
