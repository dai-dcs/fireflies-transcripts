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
- `deploy/` — systemd unit, Caddy reverse-proxy config, crontab snippet.

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
Then point Fireflies' webhook settings at `https://fireflies.yourdomain.com/webhooks/fireflies`
(behind Caddy — see `deploy/Caddyfile`) and add the crontab entry in `deploy/crontab.txt`.
