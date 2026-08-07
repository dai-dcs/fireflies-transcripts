"""Minimal client for the Fireflies GraphQL API.

Fireflies webhooks only carry a meetingId/eventType, not the transcript
content, so after a webhook fires we call back into the GraphQL API to fetch
the full transcript (text, summary, speakers, and the audio/video download
URL) before uploading it to Object Storage.
"""
import logging
import time

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("fireflies_client")

GRAPHQL_URL = "https://api.fireflies.ai/graphql"

# Fireflies rejects any `transcripts` query with limit > 50 (HTTP 400,
# invalid_arguments). Any caller wanting more than one page's worth of
# transcripts must paginate with skip, not raise this value.
MAX_LIST_LIMIT = 50

TRANSCRIPT_QUERY = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    dateString
    duration
    transcript_url
    audio_url
    video_url
    meeting_link
    organizer_email
    participants
    speakers {
      id
      name
    }
    sentences {
      index
      speaker_name
      speaker_id
      text
      start_time
      end_time
    }
    summary {
      keywords
      action_items
      outline
      shorthand_bullet
      overview
      bullet_gist
      gist
      short_summary
    }
  }
}
"""

LIST_TRANSCRIPTS_QUERY = """
query Transcripts($limit: Int, $skip: Int) {
  transcripts(limit: $limit, skip: $skip) {
    id
    title
    dateString
  }
}
"""


class FirefliesClient:
    def __init__(self, api_key: str, timeout: float = 30.0):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(headers=self._headers, timeout=timeout)

    @retry(stop=stop_after_attempt(6), wait=wait_exponential(multiplier=2, max=90))
    def _post(self, query: str, variables: dict) -> dict:
        resp = self._client.post(GRAPHQL_URL, json={"query": query, "variables": variables})
        if resp.status_code == 429:
            # Fireflies rate limit. Respect Retry-After if given, otherwise let
            # tenacity's exponential backoff (above) handle the wait before retrying.
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                wait_s = float(retry_after)
                log.warning("Fireflies rate limit hit (429), waiting %.1fs per Retry-After header", wait_s)
                time.sleep(wait_s)
            else:
                log.warning("Fireflies rate limit hit (429), backing off before retry")
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload and payload["errors"]:
            raise RuntimeError(f"Fireflies GraphQL error: {payload['errors']}")
        return payload["data"]

    def get_transcript(self, transcript_id: str) -> dict:
        data = self._post(TRANSCRIPT_QUERY, {"id": transcript_id})
        return data["transcript"]

    def list_transcripts(self, limit: int = 50, skip: int = 0) -> list:
        if limit > MAX_LIST_LIMIT:
            raise ValueError(
                f"list_transcripts limit={limit} exceeds Fireflies' max of {MAX_LIST_LIMIT}. "
                "Paginate with skip instead of requesting more per call."
            )
        data = self._post(LIST_TRANSCRIPTS_QUERY, {"limit": limit, "skip": skip})
        return data["transcripts"]

    def iter_all_transcripts(self, page_size: int = 50, start_skip: int = 0):
        """Yield every transcript id/title the account has, page by page.
        start_skip lets a caller resume pagination from a prior checkpoint
        instead of always starting over from the beginning."""
        skip = start_skip
        while True:
            page = self.list_transcripts(limit=page_size, skip=skip)
            if not page:
                return
            for item in page:
                yield item
            skip += page_size
