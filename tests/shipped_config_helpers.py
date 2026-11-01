"""Paths of the shipped configuration files, shared by the configuration tests."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SPEC = "deploy/digitalocean/app.yaml"
