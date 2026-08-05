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
import hashlib
import hmac
import logging
import os

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

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
            source="webhook", include_media=INCLUDE_MEDIA,
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
