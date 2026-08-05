"""Fireflies -> OCI Object Storage webhook listener.

Run with:  uvicorn app:app --host 0.0.0.0 --port 8787
(or via the included systemd unit, which is the recommended way on the server)

Flow:
  1. Fireflies POSTs to /webhooks/fireflies when a transcript finishes processing.
  2. We verify the HMAC-SHA256 signature (x-hub-signature header) against
     FIREFLIES_WEBHOOK_SECRET.
  3. We immediately return 200 (Fireflies expects a fast ack) and hand the
     actual fetch+upload work to a background task.
  4. The background task fetches the full transcript and streams it to
     Object Storage, recording success in the local idempotency ledger.
"""
import gzip
import hashlib
import hmac
import json
import logging
import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

from fireflies_client import FirefliesClient
from oci_uploader import ObjectStorageUploader
from pipeline import sync_transcript
from state import StateStore

load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("webhook")

app = FastAPI(title="Fireflies -> OCI Object Storage connector")

FIREFLIES_API_KEY = os.environ["FIREFLIES_API_KEY"]
WEBHOOK_SECRET = os.environ.get("FIREFLIES_WEBHOOK_SECRET", "")
INCLUDE_MEDIA = os.getenv("INCLUDE_MEDIA", "false").lower() == "true"
# When true, transcript JSON is gzipped before upload (smaller objects, same
# free-tier storage allowance goes further). Configurable via .env so it can
# be toggled without touching code.
COMPRESS_TRANSCRIPTS = os.getenv("COMPRESS_TRANSCRIPTS", "false").lower() == "true"
# Enables the /test/decompress testing-only route below. Leave this off in
# any deployment reachable from the internet unless you actually need it.
ENABLE_TEST_ROUTES = os.getenv("ENABLE_TEST_ROUTES", "false").lower() == "true"

fireflies = FirefliesClient(FIREFLIES_API_KEY)
uploader = ObjectStorageUploader(
    namespace=os.environ["OCI_NAMESPACE"],
    bucket=os.environ["OCI_BUCKET"],
    region=os.environ.get("OCI_REGION", ""),
    profile=os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT"),
    use_instance_principal=os.getenv("USE_INSTANCE_PRINCIPAL", "false").lower() == "true",
)
state = StateStore(os.environ.get("STATE_DB_PATH", "./state.db"))


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not WEBHOOK_SECRET:
        # No secret configured -> skip verification (not recommended for production).
        return True
    if not signature_header:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.replace("sha256=", "").strip()
    return hmac.compare_digest(expected, provided)


def _process(transcript_id: str):
    try:
        sync_transcript(
            transcript_id, fireflies, uploader, state,
            source="webhook", include_media=INCLUDE_MEDIA, compress=COMPRESS_TRANSCRIPTS,
        )
    except Exception:
        log.exception("Failed to sync transcript %s from webhook", transcript_id)


@app.post("/webhooks/fireflies")
async def fireflies_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature: str | None = Header(default=None, alias="x-hub-signature"),
):
    raw_body = await request.body()
    if not verify_signature(raw_body, x_hub_signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    event_type = payload.get("eventType")
    meeting_id = payload.get("meetingId")

    log.info("Received webhook: eventType=%s meetingId=%s", event_type, meeting_id)

    if event_type == "Transcription completed" and meeting_id:
        background_tasks.add_task(_process, meeting_id)
    else:
        log.info("Ignoring event type %s", event_type)

    return {"status": "accepted"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


if ENABLE_TEST_ROUTES:
    # ---- Testing-only routes -------------------------------------------
    # Not part of the sync pipeline. Lets you drag a .json.gz transcript
    # object (downloaded from the bucket, or any gzip you want to inspect)
    # onto a page and see it decompressed and pretty-printed, to sanity
    # check that COMPRESS_TRANSCRIPTS is producing valid, readable output.
    # Gate this behind ENABLE_TEST_ROUTES so it isn't reachable in a normal
    # production deployment.

    _UPLOAD_FORM = """
    <!doctype html>
    <html>
    <head><title>Fireflies connector — decompress test</title></head>
    <body style="font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto;">
      <h2>Decompress a transcript (testing only)</h2>
      <p>Upload a <code>.json.gz</code> object (or any gzip) to view its decompressed contents.</p>
      <form action="/test/decompress" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".gz,.json.gz" required>
        <button type="submit">Decompress &amp; view</button>
      </form>
    </body>
    </html>
    """

    @app.get("/test/decompress", response_class=HTMLResponse)
    async def decompress_form():
        return _UPLOAD_FORM

    @app.post("/test/decompress")
    async def decompress_upload(file: UploadFile = File(...)):
        raw = await file.read()
        try:
            decompressed = gzip.decompress(raw)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Not a valid gzip file: {e}")

        try:
            parsed = json.loads(decompressed)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            # Not JSON inside the gzip — still show the raw decompressed text.
            pretty = decompressed.decode("utf-8", errors="replace")

        html = f"""
        <!doctype html>
        <html>
        <head><title>Decompressed: {file.filename}</title></head>
        <body style="font-family: system-ui, sans-serif; max-width: 960px; margin: 40px auto;">
          <h3>{file.filename}</h3>
          <p>{len(raw)} bytes compressed &rarr; {len(decompressed)} bytes decompressed</p>
          <pre style="background:#f5f5f5; padding:16px; border-radius:6px; overflow-x:auto; white-space:pre-wrap;">{pretty}</pre>
          <p><a href="/test/decompress">&larr; upload another</a></p>
        </body>
        </html>
        """
        return HTMLResponse(content=html)
