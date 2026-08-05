"""One-time backfill: push every transcript already sitting in Fireflies
into Object Storage. Safe to re-run — already-uploaded transcripts are
skipped via the state ledger.

Usage:
  python backfill.py
"""
import logging
import os
import time

from dotenv import load_dotenv

from fireflies_client import FirefliesClient
from oci_uploader import ObjectStorageUploader
from pipeline import sync_transcript
from state import StateStore

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("backfill")


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
    # Fireflies enforces a per-account rate limit on the GraphQL API. Backfill
    # calls it once per transcript, so on an account with a lot of history
    # that's a burst of requests — pace it out to stay under the limit.
    delay_seconds = float(os.getenv("BACKFILL_DELAY_SECONDS", "1.5"))

    total, uploaded, skipped, failed = 0, 0, 0, 0
    for item in fireflies.iter_all_transcripts(page_size=50):
        total += 1
        tid = item["id"]
        try:
            result = sync_transcript(tid, fireflies, uploader, state, source="backfill", include_media=include_media)
            if result:
                uploaded += 1
                time.sleep(delay_seconds)
            else:
                skipped += 1
        except Exception:
            failed += 1
            log.exception("Failed to backfill transcript %s (%s)", tid, item.get("title"))

    log.info("Backfill complete: total=%d uploaded=%d skipped=%d failed=%d", total, uploaded, skipped, failed)


if __name__ == "__main__":
    main()
