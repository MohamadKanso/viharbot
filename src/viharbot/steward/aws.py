"""The only place the steward imports AWS. Everything else takes injected clients, so the core and
the orchestration stay testable offline and a reviewer can see exactly where AWS is touched."""

from __future__ import annotations

# Short timeouts and few retries so a hung AWS call cannot hold a poll past its deadline: each call
# is bounded to roughly (connect + read) x attempts, about 20 seconds at worst.
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 7
MAX_ATTEMPTS = 2


def client(service: str, region: str):
    import boto3  # imported lazily: tests and the pure core never need it
    from botocore.config import Config

    config = Config(connect_timeout=CONNECT_TIMEOUT_SECONDS, read_timeout=READ_TIMEOUT_SECONDS,
                    retries={"max_attempts": MAX_ATTEMPTS, "mode": "standard"})
    return boto3.client(service, region_name=region, config=config)


def secrets_client(region: str):
    return client("secretsmanager", region)


def dynamodb_client(region: str):
    return client("dynamodb", region)


def sns_client(region: str):
    return client("sns", region)


def cloudwatch_client(region: str):
    return client("cloudwatch", region)
