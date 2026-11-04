"""Run Archie's learning pipeline by hand or from a schedule.

    python scripts/archie_learn.py ingest --pr 88            # extract only (dry run, default)
    python scripts/archie_learn.py ingest --pr 88 --propose  # also open an instructor-repo PR
    python scripts/archie_learn.py reconcile [--propose]

Configuration (environment): ARCHIE_SOURCE_TOKEN (read-only on the source repo),
ARCHIE_INSTRUCTOR_TOKEN (needed only with --propose; contents and pull-requests write on the
instructor repo only), LEARNING_SOURCE_REPO, LEARNING_INSTRUCTOR_REPO, LEARNING_STORE_DIR.
No model is called, and nothing is merged.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from viharbot.learning.github import InstructorProposer, SourceReader
from viharbot.learning.ingest import process, reconcile
from viharbot.learning.store import IngestionStore
from viharbot.observability import configure_logging
from viharbot.sage_review_github import GitHubClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("ingest")
    one.add_argument("--pr", type=int, required=True)
    rec = sub.add_parser("reconcile")
    rec.add_argument("--limit", type=int, default=30)
    for p in (one, rec):
        p.add_argument("--propose", action="store_true", help="open an instructor-repo pull request")
    args = parser.parse_args(argv)
    configure_logging()

    source = os.getenv("LEARNING_SOURCE_REPO", "vchokshi/viharbot")
    instructor = os.getenv("LEARNING_INSTRUCTOR_REPO", "vchokshi/viharbot-onboarding-instructor")
    token = os.getenv("ARCHIE_SOURCE_TOKEN", "")
    if not token:
        print("ARCHIE_SOURCE_TOKEN is not set", file=sys.stderr)
        return 2
    reader = SourceReader(GitHubClient(token), source)
    proposer = None
    if args.propose:
        write_token = os.getenv("ARCHIE_INSTRUCTOR_TOKEN", "")
        if not write_token:
            print("ARCHIE_INSTRUCTOR_TOKEN is required with --propose", file=sys.stderr)
            return 2
        proposer = InstructorProposer(GitHubClient(write_token), instructor)
    store = IngestionStore(Path(os.getenv("LEARNING_STORE_DIR", "data/learning")))
    events = ([process(args.pr, reader, store, proposer)] if args.command == "ingest"
              else reconcile(reader, store, proposer, args.limit))
    for e in events:
        print(f"PR #{e.pull_request}: {e.state.value}" + (f" ({e.error})" if e.error else "")
              + (f" {e.proposal_url}" if e.proposal_url else ""))
    return 1 if any(e.state.value == "failed" for e in events) else 0


if __name__ == "__main__":
    raise SystemExit(main())
