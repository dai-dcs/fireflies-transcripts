"""Reset the local idempotency ledger (state.db).

DESTRUCTIVE — after this, every transcript looks "never uploaded" again to
the connector, so the next backfill (or any redelivered webhook) will
re-fetch and re-upload everything. Pair this with empty_bucket.py when you
want a genuinely clean slate; running this alone against a bucket that
still has objects will re-upload and overwrite them (harmless, just wasted
API calls) rather than duplicate them, since object names are deterministic.

Usage:
  python reset_state.py            # asks for confirmation
  python reset_state.py --yes      # skips confirmation (careful!)
"""
import argparse
import logging
import os

from dotenv import load_dotenv

from state import StateStore

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("reset_state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    args = parser.parse_args()

    db_path = os.environ.get("STATE_DB_PATH", "./state.db")
    state = StateStore(db_path)

    with state._conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM uploaded_transcripts").fetchone()[0]

    if count == 0:
        log.info("state.db ('%s') already has no recorded uploads. Nothing to reset.", db_path)
        return

    log.info("state.db ('%s') currently tracks %d uploaded transcript(s).", db_path, count)

    if not args.yes:
        typed = input(f"Type RESET to permanently clear all {count} record(s) from state.db: ")
        if typed != "RESET":
            log.error("Confirmation did not match. Aborting, nothing reset.")
            return

    with state._conn() as conn:
        conn.execute("DELETE FROM uploaded_transcripts")
        conn.execute("DELETE FROM checkpoints")
        conn.commit()

    log.info("state.db reset: cleared %d record(s) and any backfill checkpoint.", count)


if __name__ == "__main__":
    main()
