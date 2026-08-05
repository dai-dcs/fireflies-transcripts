"""Shared transcript -> Object Storage pipeline.

One function, used by the webhook listener (near-real-time path), the
backfill script (one-time historical dump), and the reconcile script
(periodic safety net in case a webhook was ever missed).
"""
import logging
from datetime import datetime

from fireflies_client import FirefliesClient
from oci_uploader import ObjectStorageUploader
from state import StateStore

log = logging.getLogger("pipeline")


def object_prefix_for(date_string: str | None) -> str:
    """Partition objects by date (YYYY/MM/DD) so the bucket stays browsable
    and lifecycle rules can target date ranges if you ever archive old data."""
    try:
        dt = datetime.fromisoformat(date_string.replace("Z", "+00:00")) if date_string else datetime.utcnow()
    except Exception:
        dt = datetime.utcnow()
    return dt.strftime("%Y/%m/%d")


def sync_transcript(
    transcript_id: str,
    fireflies: FirefliesClient,
    uploader: ObjectStorageUploader,
    state: StateStore,
    source: str,
    include_media: bool = False,
    force: bool = False,
    compress: bool = False,
) -> str | None:
    """Fetch one transcript from Fireflies and push it to Object Storage.
    Returns the object name on success, or None if it was already synced.
    When compress=True, the transcript JSON is gzipped before upload and the
    object gets a .json.gz name so it's unambiguous from the key alone."""
    if not force and state.already_uploaded(transcript_id):
        log.info("Skipping %s, already uploaded", transcript_id)
        return None

    transcript = fireflies.get_transcript(transcript_id)
    prefix = object_prefix_for(transcript.get("dateString"))
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (transcript.get("title") or "untitled"))[:80]
    base_name = f"{prefix}/{transcript_id}_{safe_title}"

    json_object_name = f"{base_name}/transcript.json.gz" if compress else f"{base_name}/transcript.json"
    uploader.upload_json(json_object_name, transcript, compress=compress)

    if include_media:
        for field, ext in (("audio_url", "mp3"), ("video_url", "mp4")):
            url = transcript.get(field)
            if url:
                media_name = f"{base_name}/media.{ext}"
                if not uploader.object_exists(media_name):
                    uploader.upload_from_url(media_name, url)

    state.mark_uploaded(transcript_id, json_object_name, source)
    log.info("Synced transcript %s -> %s", transcript_id, json_object_name)
    return json_object_name
