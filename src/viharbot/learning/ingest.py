"""Idempotent, retryable ingestion of merged pull requests.

The trigger is trusted: a verified GitHub webhook, or `reconcile`, which asks the API for merged
pull requests. Both go through `process`, which keys work by source revision, so reprocessing the
same revision never creates a second artifact or a second proposal. Failures are recorded and a
later call retries them. Nothing here merges.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Callable

from .extract import extract, render_markdown
from .github import InstructorProposer, SourceReader
from .models import IngestionEvent, IngestState
from .store import IngestionStore, event_key

logger = logging.getLogger("archie-learning")
MAX_ATTEMPTS = 5
INSTRUCTOR_DIR = "knowledge/pr-case-studies"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check a GitHub `X-Hub-Signature-256` header in constant time."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def process(
    number: int, reader: SourceReader, store: IngestionStore,
    proposer: InstructorProposer | None, request_id: str = "-",
    sleep: Callable[[float], None] = time.sleep,
) -> IngestionEvent:
    """Ingest one merged pull request. `proposer=None` extracts and stops (a dry run)."""
    started = time.monotonic()
    snapshot = None
    event = None
    try:
        snapshot = reader.snapshot(number)
        event = store.record(snapshot.repository, number, snapshot.merge_commit_sha)
        if event.state is IngestState.PROPOSED:
            return event  # already done for this revision
        # A record stuck in `processing` means an earlier run crashed; this single-writer run retries it.
        if event.attempts >= MAX_ATTEMPTS:
            return event
        event.state, event.attempts, event.error = IngestState.PROCESSING, event.attempts + 1, None
        store.save(event)
        artifact = extract(snapshot)
        event.artifact_id = artifact.artifact_id
        event.state = IngestState.EXTRACTED
        store.save(event)
        if proposer is not None:
            path = f"{INSTRUCTOR_DIR}/{artifact.artifact_id}.md"
            body = (f"Proposed by Archie from {snapshot.repository}#{number}. Status: "
                    f"{artifact.verification.value}. Needs human review before merge; "
                    "nothing is merged automatically.")
            event.proposal_url = proposer.propose(
                path, render_markdown(artifact), f"archie/{artifact.artifact_id.lower()}",
                f"Add learning artifact {artifact.artifact_id}", body)
            event.state = IngestState.PROPOSED
            store.save(event)
        return event
    except Exception as exc:
        # Record only the exception class so no token, URL or response body reaches disk or logs.
        if event is not None:
            event.state, event.error = IngestState.FAILED, type(exc).__name__
            store.save(event)
            return event
        raise
    finally:
        logger.info("learning_ingest", extra={
            "agent": "archie", "request_id": request_id, "destination_agent": "instructor-repo",
            "model_alias": None, "duration_ms": int((time.monotonic() - started) * 1000),
            **({"error": event.error} if event is not None and event.error else {})})


def reconcile(reader: SourceReader, store: IngestionStore, proposer: InstructorProposer | None,
              limit: int = 30) -> list[IngestionEvent]:
    """Scheduled catch-up: process merged pull requests not yet proposed, and retry failures."""
    results = []
    for pr in reader.merged_pull_requests(limit):
        known = store.get(event_key(pr["base"]["repo"]["full_name"], pr["number"], pr["merge_commit_sha"]))
        if known and known.state is IngestState.PROPOSED:
            continue
        if known and known.state is IngestState.EXTRACTED and proposer is None:
            continue
        results.append(process(pr["number"], reader, store, proposer))
    return results
