"""Run one steward poll by hand, as a DRY RUN: read, assess, print what would be emailed.

    python -m viharbot.steward                # every open first-time pull request
    python -m viharbot.steward --pr 179       # one pull request

This is the local proof that the reader GitHub App works end to end. It uses the reader app's
secret from AWS Secrets Manager (your AWS login), mints a read-only token (refusing any token with
a write permission), and reads GitHub with GET requests. It writes nothing: no state, no audit
row, no email, no GitHub comment. The key never leaves this process and is never printed.

A live run (state, audit, email) is deliberately not available here; it belongs to the deployed
runtime. For a dry run the alert topic is only a placeholder that is never contacted.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from .config import ConfigError, from_env
from .github_auth import AuthError, InstallationTokens, load_secret_from_aws
from .metrics import NoMetrics
from .poller import run_poll

PLACEHOLDER_TOPIC = "arn:aws:sns:us-west-2:000000000000:viharbot-kepler-alerts"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    token_source: Any = None,
    make_client: Callable[[str], Any] | None = None,
    clock: Callable[[], datetime.datetime] = _now,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m viharbot.steward", description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, help="assess only this pull request number")
    args = parser.parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    env.setdefault("KEPLER_ALERT_TOPIC_ARN", PLACEHOLDER_TOPIC)  # never contacted in a dry run
    try:
        config = from_env(env)
    except ConfigError as error:
        out(f"configuration problem: {error}")
        return 2
    if token_source is None:
        from .aws import secrets_client

        secrets = secrets_client(config.region)
        token_source = InstallationTokens(
            lambda: load_secret_from_aws(secrets, config.reader_secret_id), config.repo, clock)
    try:
        result = run_poll(
            config, token_source=token_source, make_client=make_client, state=None, audit=None,
            publisher=None, metrics=NoMetrics(), clock=clock, request_id="local-dry-run",
            dry_run=True, only_pr=args.pr)
    except AuthError as error:
        out(f"authentication failed: {error}")
        return 1
    out(f"DRY RUN: nothing was written, stored or sent. Status: {result.status}"
        + (f" (error: {result.error_code})" if result.error_code else ""))
    out(f"Pull requests assessed: {result.prs_assessed} of {result.pull_numbers}; "
        f"failures: {result.failures}")
    if not result.emails:
        out("No alert would be sent: nothing new to report.")
    for subject, body in result.emails:
        out(f"\n===== WOULD EMAIL: {subject} =====\n{body}")
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
