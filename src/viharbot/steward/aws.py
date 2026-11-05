"""The only place the steward imports AWS. Everything else takes injected clients, so the core and
the orchestration stay testable offline and a reviewer can see exactly where AWS is touched."""

from __future__ import annotations


def client(service: str, region: str):
    import boto3  # imported lazily: tests and the pure core never need it

    return boto3.client(service, region_name=region)


def secrets_client(region: str):
    return client("secretsmanager", region)


def dynamodb_client(region: str):
    return client("dynamodb", region)


def sns_client(region: str):
    return client("sns", region)


def cloudwatch_client(region: str):
    return client("cloudwatch", region)
