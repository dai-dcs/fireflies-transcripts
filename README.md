# Fireflies -> OCI Object Storage Connector

Near-real-time pipeline: Fireflies fires a webhook the moment a transcript
finishes processing, a small FastAPI service on your OCI VM fetches the full
transcript over the Fireflies GraphQL API, and uploads it straight into an
Object Storage bucket. A 15-minute cron reconciliation job and a one-time
backfill script cover anything already accumulated or ever missed.

Full setup instructions (OCI bucket, IAM policy, Fireflies API key/webhook,
server deployment, testing, troubleshooting) are in **Fireflies-to-OCI Setup Guide.docx**.

## Files
- `app.py` — webhook listener (FastAPI), the near-real-time path.
- `fireflies_client.py` — Fireflies GraphQL API client.
- `oci_uploader.py` — OCI Object Storage upload helper.
- `pipeline.py` — shared fetch+upload logic used by all three entry points.
- `state.py` — SQLite idempotency ledger (prevents duplicate uploads).
- `backfill.py` — one-time script to push every existing transcript.
- `reconcile.py` — periodic safety net for missed webhooks (run via cron).
- `deploy/` — systemd unit, nginx location snippet, crontab snippet. (`deploy/Caddyfile` is the original design for a bare VM with no existing reverse proxy — superseded by `deploy/nginx-fireflies.conf` if nginx is already installed and terminating TLS, which is this deployment's actual setup.)

## Quick start (after following the setup guide for credentials/bucket)
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python backfill.py     # push everything that's already in Fireflies
sudo cp deploy/fireflies-connector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fireflies-connector
```

### Reverse proxy: nginx (this deployment)
The app binds to `127.0.0.1:8787` (see `deploy/fireflies-connector.service`) and is not
directly exposed. Add the location block from `deploy/nginx-fireflies.conf` into the
existing `server { }` block for your domain, then:
```bash
sudo nginx -t && sudo systemctl reload nginx
```
Point Fireflies' webhook settings at the public path, e.g.
`https://dashboards.decirclesolar.com/fireflies/webhooks/fireflies`, and add the
crontab entry in `deploy/crontab.txt`.

### Reverse proxy: Caddy (alternative, bare VM with no existing proxy)
If nginx isn't already running, `deploy/Caddyfile` is a simpler one-file alternative —
it also handles the Let's Encrypt certificate automatically.
