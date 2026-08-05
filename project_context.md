# Project Context — Fireflies → OCI Object Storage Connector (Webhook)

## Goal
Sync every Fireflies transcript into an Oracle Object Storage bucket in near
real time, staying inside OCI's Always Free tier, running on the user's
existing 12 GB / 2 OCPU Always Free compute instance.

## Strategy
Fireflies fires a webhook ("Transcription completed") the moment a
transcript finishes processing. A FastAPI listener on the OCI VM receives
it, verifies the HMAC-SHA256 signature, fetches the full transcript over the
Fireflies GraphQL API, and streams it into Object Storage — typically within
seconds. A 15-minute cron reconciliation job and a one-time backfill script
cover anything missed or already accumulated.

```
Fireflies meeting ends
  → Fireflies webhook ("Transcription completed")
    → FastAPI listener on OCI VM (port 8787, behind Caddy/TLS)
      → Fireflies GraphQL API (fetch full transcript JSON)
        → OCI Object Storage bucket (transcript.json, partitioned by date)
```

## Files
| File | Purpose |
|---|---|
| `app.py` | FastAPI webhook listener — verifies signature, hands off to `pipeline.sync_transcript` via background task |
| `fireflies_client.py` | Fireflies GraphQL client (`get_transcript`, `list_transcripts`, `iter_all_transcripts`) |
| `oci_uploader.py` | OCI Object Storage upload helper (JSON + streamed media, config-file or instance-principal auth) |
| `pipeline.py` | Shared fetch+upload logic used by all entry points; partitions objects as `YYYY/MM/DD/<id>_<title>/transcript.json` |
| `state.py` | SQLite idempotency ledger — every entry point checks/marks here to avoid duplicate uploads |
| `backfill.py` | One-time script: pushes every transcript already in the Fireflies account |
| `reconcile.py` | Cron job (every 15 min): re-checks the most recent N transcripts in case a webhook was dropped |
| `deploy/fireflies-connector.service` | systemd unit for `app.py` |
| `deploy/Caddyfile` | Reverse proxy + automatic Let's Encrypt TLS for the public webhook endpoint |
| `deploy/crontab.txt` | Cron line for `reconcile.py` |

## Key decisions
- **Webhook over polling** was the user's explicit choice for this variant (see the polling variant in the sibling project for the alternative).
- **Idempotency via SQLite** (`state.db`), not reliance on OCI object-exists checks alone, so re-delivered webhooks, backfill, and reconcile can all run concurrently/safely.
- **HMAC signature verification** on every webhook request (`FIREFLIES_WEBHOOK_SECRET`), rejecting anything not actually from Fireflies.
- **Date-partitioned object keys** (`YYYY/MM/DD/...`) for browsability and future lifecycle/archive rules.
- **Media upload is opt-in** (`INCLUDE_MEDIA=false` by default) to stay comfortably inside the 20 GB Always Free Object Storage allowance — text transcripts alone are a few hundred KB each.
- **Dual OCI auth paths** supported: config-file profile (simplest on a personal VM) or instance principal (no key files, needs a dynamic-group policy).

## Setup status (as of last session)
- Code and deployment artifacts are complete and syntax-verified.
- **Not yet deployed** — no SSH access to the user's OCI VM from this environment. User must run the steps in `Fireflies-to-OCI-Setup-Guide.docx` (Oracle Object Storage bucket + IAM policy + API key, Fireflies API key + webhook config, then Section 5 for server deployment).
- Requires a domain/subdomain pointed at the VM's public IP (for Caddy's TLS cert) and inbound 80/443 open in the OCI Security List/NSG — this is the main extra setup cost vs. the polling variant.

## Open items / things to revisit
- User has not yet confirmed the OCI bucket, IAM policy, or Fireflies webhook secret are created — those are prerequisites before `app.py` can start successfully.
- No domain name has been specified yet; `deploy/Caddyfile` currently has a placeholder (`fireflies.yourdomain.com`) that needs to be edited before deployment.
