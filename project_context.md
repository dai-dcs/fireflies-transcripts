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
| `app.py` | FastAPI webhook listener — verifies signature, hands off to `pipeline.sync_transcript` via background task. Also hosts the optional `/test/decompress` route (see below). |
| `fireflies_client.py` | Fireflies GraphQL client (`get_transcript`, `list_transcripts`, `iter_all_transcripts`). Handles 429 rate-limit responses explicitly (see Rate limiting section). |
| `oci_uploader.py` | OCI Object Storage upload helper (JSON + streamed media, config-file or instance-principal auth). Owns the gzip-compression behavior (see Compression / content-encoding section — read this before touching upload metadata). |
| `pipeline.py` | Shared fetch+upload logic used by all entry points; partitions objects as `YYYY/MM/DD/<id>_<title>/transcript.json` (or `.json.gz` if compressed) |
| `state.py` | SQLite idempotency ledger — every entry point checks/marks here to avoid duplicate uploads. Also holds a generic `checkpoints` table used by `backfill.py`'s resume logic. |
| `backfill.py` | One-time script: pushes every transcript already in the Fireflies account. Resumable (checkpointed), rate-limit-paced. |
| `reconcile.py` | Cron job (every 15 min): re-checks the most recent N transcripts in case a webhook was dropped |
| `empty_bucket.py` | Destructive utility: deletes all objects in the bucket (not the bucket itself). Requires typed confirmation. |
| `reset_state.py` | Destructive utility: clears `state.db` (uploaded-transcript records + backfill checkpoint). Requires typed confirmation. |
| `deploy/fireflies-connector.service` | systemd unit for `app.py`. Binds to `127.0.0.1:8787` (not `0.0.0.0`) since nginx is the public edge. |
| `deploy/Caddyfile` | Fallback reverse proxy + automatic Let's Encrypt TLS, for a bare VM with no existing proxy. **Not what this deployment actually uses** — see nginx note below. |
| `deploy/nginx-fireflies.conf` | The actual reverse-proxy config in use: a `location /fireflies/` block added to the existing nginx server block for `dashboards.decirclesolar.com`. |
| `deploy/crontab.txt` | Cron line for `reconcile.py` |

## Key decisions
- **Webhook over polling** was the user's explicit choice for this variant (see the polling variant in the sibling project for the alternative).
- **Idempotency via SQLite** (`state.db`), not reliance on OCI object-exists checks alone, so re-delivered webhooks, backfill, and reconcile can all run concurrently/safely. Object names are also deterministic (built from transcript ID + title), so even a retry that bypasses the ledger just overwrites the same key rather than duplicating.
- **HMAC signature verification** on every webhook request (`FIREFLIES_WEBHOOK_SECRET`), rejecting anything not actually from Fireflies.
- **Date-partitioned object keys** (`YYYY/MM/DD/...`) for browsability and future lifecycle/archive rules.
- **Media upload is opt-in** (`INCLUDE_MEDIA=false` by default) to stay comfortably inside the 20 GB Always Free Object Storage allowance — text transcripts alone are a few hundred KB each.
- **Dual OCI auth paths** supported: config-file profile (simplest on a personal VM) or instance principal (no key files, needs a dynamic-group policy).

## Compression / content-encoding — read before touching upload metadata
- `COMPRESS_TRANSCRIPTS` (`.env`, default `false`) gzips transcript JSON before upload via `oci_uploader.upload_json(..., compress=True)`. When on, the object gets a `.json.gz` name instead of `.json` (done in `pipeline.py`).
- **`content_encoding: gzip` is deliberately NOT set on the uploaded object**, even though that's the "obvious" way to mark a gzipped HTTP body. It was tried and reverted. Reason: `content_encoding: gzip` tells any HTTP-compliant client (browsers, some CLIs, presigned-URL fetches) to transparently decompress the response in transit. That silently defeated testing — downloading a `.json.gz` object through a browser handed back plain decompressed JSON while keeping the `.gz` filename, so `gzip.decompress()` on it failed with "Not a gzipped file" (this exact symptom was hit and diagnosed during development, uploaded object `2026_06_25_...Tibra_Sync/transcript.json.gz`).
- The user explicitly tested via OCI CLI (`oci os object get`) and confirmed that path does NOT auto-decompress — CLI/SDK access gets the raw stored bytes regardless of `content_encoding`. Since the user's downstream automation reads via CLI/SDK, not a browser, **the user chose to keep `content_encoding: gzip` set** (reverted the fix that removed it) rather than switch to `content_type: application/gzip`. This is intentional and final — do not "fix" this again without re-confirming with the user, since it was a considered decision, not an oversight.
- Practical implication for anyone (human or AI) writing a *new* reader/automation against this bucket: if you fetch objects over plain HTTP/browser/any client that auto-decompresses on `Content-Encoding: gzip`, you may receive already-decompressed JSON even though the object is named/stored as `.json.gz`. Don't assume "ends in .gz → must run gzip.decompress() on the bytes I received" — check whether your HTTP client already decompressed it (e.g., try `json.loads()` first; only fall back to `gzip.decompress()` if that fails). The `/test/decompress` route in `app.py` does exactly this pattern of "try, fall back" is NOT yet implemented there — it currently assumes raw gzip bytes always, which is correct for CLI/SDK-downloaded files but will fail if fed a browser-downloaded one. Worth hardening if this route gets used more.
- No migration was needed for existing objects: the live service was never restarted with the (reverted) `content_encoding`-removal fix, so every object ever uploaded is consistently in the original format (`content_encoding: gzip` set, when `COMPRESS_TRANSCRIPTS=true`). There is exactly one format in the bucket, not two — confirmed with the user directly.
- `ENABLE_TEST_ROUTES` (`.env`, default `false`) gates `/test/decompress` (GET form + POST handler in `app.py`). Testing only; the user was reminded to turn it back off after use since it sits on a public nginx path (`https://dashboards.decirclesolar.com/fireflies/test/decompress`).
- Form/link URLs in `/test/decompress`'s HTML use empty/relative paths (`action=""`, `href=""`), not absolute paths (`/test/decompress`). This was a deliberate fix: nginx strips the `/fireflies/` prefix before forwarding to the app, so the app has no idea it's mounted under a prefix — absolute paths in generated HTML resolve wrong in the browser (they'd point at domain root instead of `/fireflies/...`). Any future HTML this app generates should follow the same relative-URL pattern.

## Rate limiting
- Fireflies enforces an account-wide rate limit on the GraphQL API (same limit applies to `FIREFLIES_API_KEY` regardless of which script/endpoint uses it — webhook-triggered fetches and `backfill.py` share the same budget). The user hit a 24h lockout once during initial backfill of ~490 historical transcripts.
- `fireflies_client.py`'s `_post()` retries up to 6 times with exponential backoff (max 90s between attempts) and explicitly checks for HTTP 429, respecting a `Retry-After` header if Fireflies sends one.
- `backfill.py` paces itself with `BACKFILL_DELAY_SECONDS` (`.env`, default `1.5`s) after each successful upload, specifically to avoid re-triggering the account-wide limit during a full historical sync.
- During an active rate-limit lockout, webhook-triggered syncs will also fail (same API, same key) — but non-destructively: `app.py`'s background task catches the exception, logs it, and moves on. Nothing crashes, and nothing is lost, because the failed transcript was never marked in `state.db`. It gets picked up automatically by the next `reconcile.py` cron run or a manual `backfill.py` once the lockout clears.
- `backfill.py` checkpoints its pagination offset in `state.db`'s `checkpoints` table (key `backfill_skip`) after each completed page of 50, so an interrupted run (rate limit, crash, Ctrl-C) resumes near where it stopped rather than re-scanning from page one. Deliberately checkpointed by pagination offset, not by "last uploaded transcript ID" — Fireflies' listing order isn't guaranteed stable if new transcripts land mid-run, so ID-based lookback risked skipping something; offset-based resume plus the idempotency ledger is safe because re-processing a page is a no-op for anything already uploaded.

## Setup status (as of last session)
- **Live and deployed.** User cloned this repo onto their server via Git (not the OCI Always Free VM this was originally scoped for — same idea, different host: `~/fireflies-transcripts/fireflies-transcripts`, user `ubuntu`), running under systemd as `fireflies-connector.service`, behind nginx.
- OCI bucket `fireflies-transcripts` (namespace `bmpzn7ox458e`, compartment `fireflies-dump`) exists and is reachable — required a two-statement IAM policy: `manage objects` (upload/read/delete objects) plus `read buckets` (the `head_bucket` existence check on startup needs the separate bucket-level resource-type grant; object-level `manage` alone returns a 404 for this, which was diagnosed and fixed).
- Actual reverse proxy is **nginx**, not Caddy — `deploy/Caddyfile` is the fallback for a bare VM with no existing proxy; `deploy/nginx-fireflies.conf` is the real config, added as a `location /fireflies/` block inside the existing server block for `dashboards.decirclesolar.com` (which already runs another app).
- `deploy/fireflies-connector.service` binds uvicorn to `127.0.0.1:8787`, not `0.0.0.0:8787`, since nginx is the public-facing edge.
- Webhook URL: `https://dashboards.decirclesolar.com/fireflies/webhooks/fireflies`. The `/fireflies/` prefix is stripped by nginx before forwarding to the app's actual route, `/webhooks/fireflies` — no app code changes were needed for this part (only for the `/test/decompress` HTML, see above).
- `~/.oci/config`'s `key_file` needed to be an absolute path with no surrounding quotes — the OCI Console's copy-paste snippet included quotes and a `~`-relative path, which the SDK's config parser doesn't handle. Fixed manually on the server.
- Historical backfill (~490 transcripts) has been run; hit and recovered from a Fireflies 24h rate-limit lockout during the process (see Rate limiting section).
- `COMPRESS_TRANSCRIPTS` and `ENABLE_TEST_ROUTES` exist in `.env` but their current on/off state on the live server wasn't confirmed in this session — check `.env` directly rather than assuming.

## Open items / things to revisit
- If `ENABLE_TEST_ROUTES=true` is still set, remind the user it's a public path (`.../fireflies/test/decompress`) and should be turned off when not actively testing.
- The `/test/decompress` handler assumes its input is always raw (never-transport-decompressed) gzip bytes. This is correct for anything downloaded via OCI CLI/SDK, but would break if someone fed it a browser-downloaded copy of an object (since `content_encoding: gzip` causes browsers to auto-decompress on download). Not yet hardened with a "try `json.loads` first, fall back to `gzip.decompress`" pattern — see Compression section above.
- Reconciliation cron (`deploy/crontab.txt`) — confirm it was actually installed via `crontab -e` on the server; this was provided but not explicitly confirmed as done.
