"""Request model and controller-owned policy (ADR-007 decisions 3 and 4).

A request is data, not a spec: unknown fields (commands, volumes, free-form specs, secrets) are
rejected by the model, and every value is checked against the policy before anything is built.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, StrictInt

TEMPLATE_VERSION = "orbit-app-v1"
RESERVED_ENV_PREFIX = "ORBIT_"  # marker entries live here; a request may never set them
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REQUEST_HASH = re.compile(r"[0-9a-f]{64}")  # what request_hash() returns
_NAME_SUFFIX = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_REPOSITORY = re.compile(r"[a-z0-9/_.-]+")
_ENV_VALUE = re.compile(r"[A-Za-z0-9._:/@+-]{0,128}")
_SECRETISH = re.compile(r"(?i)(secret|token|passw|api[-_]?key|bearer|private|credential)")
_HIGH_ENTROPY = re.compile(r"[A-Za-z0-9+/_-]{32,}")


class PolicyDenied(Exception):
    """A request or action is refused. ``reason`` is a fixed code that is safe to log."""

    def __init__(self, reason: str, status: int = 422) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageRef(_Strict):
    registry: str
    repository: str
    digest: str


class Replace(_Strict):
    app_id: str
    expected_version: StrictInt


class DeploymentRequest(_Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    name: str
    image: ImageRef
    size: str
    region: str
    port: StrictInt | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ttl_minutes: StrictInt = 120
    replaces: Replace | None = None


@dataclass(frozen=True)
class Policy:
    """Reviewed like bindings.toml. Registry, repository prefix, size and env keys are placeholders
    until the real registry and app size are chosen; nothing here is a verified provider value."""

    requesters: frozenset[str] = frozenset({"orbit"})
    approvers: frozenset[str] = frozenset({"approver"})
    name_prefix: str = "orbit-"
    registry: str = "registry.example.invalid/viharbot-orbit"
    repository_prefix: str = "orbit/"
    region: str = "nyc"
    size: str = "512mib"
    env_keys: frozenset[str] = frozenset({"LOG_LEVEL", "APP_MODE", "FEATURE_FLAG"})
    max_concurrent: int = 3
    max_creates_per_hour: int = 5
    max_actions_per_day: int = 10
    max_ttl_minutes: int = 24 * 60
    approval_ttl_seconds: int = 30 * 60
    max_log_lines: int = 500
    max_log_bytes: int = 64 * 1024

    def check(self, req: DeploymentRequest) -> None:
        """Raise PolicyDenied for the first rule the request breaks."""
        suffix = req.name[len(self.name_prefix):]
        if not req.name.startswith(self.name_prefix) or not _NAME_SUFFIX.fullmatch(suffix):
            raise PolicyDenied("name_not_allowed")
        image = req.image
        if image.registry != self.registry:
            raise PolicyDenied("registry_not_allowed")
        repo = image.repository
        if not repo.startswith(self.repository_prefix) or not _REPOSITORY.fullmatch(repo):
            raise PolicyDenied("repository_not_allowed")
        if not _DIGEST.fullmatch(image.digest):
            raise PolicyDenied("image_must_be_pinned_by_digest")
        if req.size != self.size:
            raise PolicyDenied("size_not_allowed")
        if req.region != self.region:
            raise PolicyDenied("region_not_allowed")
        if req.port is not None and not 1 <= req.port <= 65535:
            raise PolicyDenied("port_not_allowed")
        for key, value in req.env.items():
            if key.startswith(RESERVED_ENV_PREFIX) or key not in self.env_keys:
                raise PolicyDenied("env_key_not_allowed")
            if not _ENV_VALUE.fullmatch(value):
                raise PolicyDenied("env_value_not_allowed")
            if _SECRETISH.search(value) or _HIGH_ENTROPY.search(value):
                raise PolicyDenied("secret_env_value")
        if not 1 <= req.ttl_minutes <= self.max_ttl_minutes:
            raise PolicyDenied("ttl_not_allowed")


def canonical_request(req: DeploymentRequest, team_id: str) -> str:
    """The complete canonical request: every field, the full env mapping, the spec-template
    version and the provider team. For a replacement ``replaces`` carries the immutable app id and
    the expected version, so an approval cannot move to another resource."""
    body = req.model_dump()
    body["env"] = dict(sorted(req.env.items()))
    body["template_version"] = TEMPLATE_VERSION
    body["team_id"] = team_id
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def request_hash(req: DeploymentRequest, team_id: str) -> str:
    return hashlib.sha256(canonical_request(req, team_id).encode()).hexdigest()


def is_request_hash(value: object) -> bool:
    """True only for a string shaped like ``request_hash()`` output: 64 lowercase hex digits."""
    return isinstance(value, str) and _REQUEST_HASH.fullmatch(value) is not None


def build_spec(req: DeploymentRequest, marker_env: dict[str, str]) -> dict:
    """The fixed app-spec template. Nothing from the request is passed through but these fields."""
    return {
        "template": TEMPLATE_VERSION,
        "name": req.name,
        "region": req.region,
        "size": req.size,
        "image": req.image.model_dump(),
        "port": req.port,
        "env": {**req.env, **marker_env},
    }
