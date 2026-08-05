"""Delete every object in the configured OCI Object Storage bucket.

DESTRUCTIVE — this permanently deletes all objects in OCI_BUCKET (not the
bucket itself). Intended for wiping a test/dev bucket clean before a fresh
backfill run. Requires typing the bucket name to confirm, unless run with
--yes for non-interactive use.

Usage:
  python empty_bucket.py            # asks for confirmation
  python empty_bucket.py --yes      # skips confirmation (careful!)
  python empty_bucket.py --dry-run  # lists what would be deleted, deletes nothing
"""
import argparse
import logging
import os

import oci
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("empty_bucket")


def build_client():
    profile = os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT")
    if os.getenv("USE_INSTANCE_PRINCIPAL", "false").lower() == "true":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        return oci.object_storage.ObjectStorageClient(config={}, signer=signer)
    config = oci.config.from_file(profile_name=profile)
    region = os.environ.get("OCI_REGION", "")
    if region:
        config["region"] = region
    return oci.object_storage.ObjectStorageClient(config)


def iter_object_names(client, namespace, bucket):
    next_start = None
    while True:
        resp = client.list_objects(namespace_name=namespace, bucket_name=bucket, start=next_start, limit=1000)
        for obj in resp.data.objects:
            yield obj.name
        next_start = resp.data.next_start_with
        if not next_start:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")
    parser.add_argument("--dry-run", action="store_true", help="list objects that would be deleted, delete nothing")
    args = parser.parse_args()

    namespace = os.environ["OCI_NAMESPACE"]
    bucket = os.environ["OCI_BUCKET"]

    client = build_client()
    names = list(iter_object_names(client, namespace, bucket))

    if not names:
        log.info("Bucket '%s' is already empty. Nothing to do.", bucket)
        return

    log.info("Found %d object(s) in bucket '%s' (namespace '%s').", len(names), bucket, namespace)

    if args.dry_run:
        for n in names:
            print(n)
        log.info("Dry run — nothing deleted.")
        return

    if not args.yes:
        typed = input(f"Type the bucket name ('{bucket}') to confirm PERMANENT deletion of all {len(names)} object(s): ")
        if typed != bucket:
            log.error("Confirmation did not match bucket name. Aborting, nothing deleted.")
            return

    deleted, failed = 0, 0
    for name in names:
        try:
            client.delete_object(namespace_name=namespace, bucket_name=bucket, object_name=name)
            deleted += 1
        except Exception:
            failed += 1
            log.exception("Failed to delete object %s", name)

    log.info("Done: deleted=%d failed=%d (bucket '%s' left in place, only its objects were removed)", deleted, failed, bucket)


if __name__ == "__main__":
    main()
