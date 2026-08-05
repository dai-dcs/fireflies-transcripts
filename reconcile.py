"""Safety-net reconciliation job.

Webhooks are the primary near-real-time path, but any webhook delivery
system can drop a request (server restart mid-delivery, transient network
blip, etc). This script scans only the most recent transcripts (default:
last 200) and uploads any that the webhook path missed. It's cheap to run
often — everything already uploaded is skipped via the state ledger.

Intended to run on a cron schedule, e.g. every 15 minutes:
  */15 * * * * cd /opt/fireflies-oci-connector && \
      /opt/fireflies-oci-connector/venv/bin/python reconcile.py >> logs/reconcile.log 2>&1
"""
import logging
import os

from dotenv import load_dotenv

from fireflies_client import FirefliesClient
from oci_uploader import ObjectStorageUploader
from pipeline import sync_transcript
from state import StateStore

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("reconcile")

RECENT_WINDOW = int(os.getenv("RECONCILE_WINDOW", "200"))


def main():
    fireflies = FirefliesClient(os.environ["FIREFLIES_API_KEY"])
    uploader = ObjectStorageUploader(
        namespace=os.environ["OCI_NAMESPACE"],
        bucket=os.environ["OCI_BUCKET"],
        region=os.environ.get("OCI_REGION", ""),
        profile=os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT"),
        use_instance_principal=os.getenv("USE_INSTANCE_PRINCIPAL", "false").lower() == "true",
    )
    state = StateStore(os.environ.get("STATE_DB_PATH", "./state.db"))
    include_media = os.getenv("INCLUDE_MEDIA", "false").lower() == "true"
    compress = os.getenv("COMPRESS_TRANSCRIPTS", "false").lower() == "true"

    recent = fireflies.list_transcripts(limit=RECENT_WINDOW, skip=0)
    missed = 0
    for item in recent:
        tid = item["id"]
        if state.already_uploaded(tid):
            continue
        try:
            sync_transcript(tid, fireflies, uploader, state, source="reconcile", include_media=include_media, compress=compress)
            missed += 1
        except Exception:
            log.exception("Reconcile failed for transcript %s", tid)

    if missed:
        log.warning("Reconcile picked up %d transcript(s) the webhook missed", missed)
    else:
        log.info("Reconcile: nothing missed, all recent transcripts already synced")


if __name__ == "__main__":
    main()
