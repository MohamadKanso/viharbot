"""Runtime-neutral request envelope and instance identity for Nova (ADR-008, C2a).

Everything here is additive and optional: a bare ``{request_id, input}`` invocation behaves
exactly as before. The envelope fields are validated and carried to logs only. Nothing in this
module meters a budget, retries, or routes; those belong to later work.
"""

from __future__ import annotations

import os
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, fields
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

CONTRACT_VERSION = "1"
NOT_ACCEPTED_HEADER = "X-Nova-Not-Accepted"
INSTANCE_HEADER = "X-Nova-Instance"
RUNTIME_KINDS = ("local", "agentcore", "fargate")
DEFAULT_INSTANCE_ID = "nova-local-01"
DEFAULT_RUNTIME = "local"
MAX_ATTEMPTS = 2  # ADR-008: at most two attempts in total
MAX_DEADLINE_MS = 900_000  # AgentCore's 15 minute synchronous limit
MAX_TOKENS = 1_000_000
MAX_COST_MICRO_USD = 100_000_000  # 100 USD, in micro-USD so no floating point is compared

_ID_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"
_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
_INSTANCE_RE = re.compile(_ID_PATTERN)

_Tokens = Annotated[StrictInt, Field(ge=0, le=MAX_TOKENS)]
_Cost = Annotated[StrictInt, Field(ge=0, le=MAX_COST_MICRO_USD)]


class NovaEnvelope(BaseModel):
    """Optional envelope fields. Unknown top-level keys are ignored, as before."""

    model_config = ConfigDict(extra="ignore")

    logical_agent: Annotated[StrictStr, Field(pattern=_NAME_PATTERN)] | None = None
    contract_version: Literal["1"] | None = None
    attempt: Annotated[StrictInt, Field(ge=1, le=MAX_ATTEMPTS)] | None = None
    deadline_ms: Annotated[StrictInt, Field(ge=1, le=MAX_DEADLINE_MS)] | None = None
    attempt_max_output_tokens: _Tokens | None = None
    attempt_max_cost_usd: _Cost | None = None  # micro-USD
    total_max_output_tokens: _Tokens | None = None
    total_max_cost_usd: _Cost | None = None  # micro-USD
    idempotency_key: Annotated[StrictStr, Field(pattern=_ID_PATTERN)] | None = None
    operation_class: Literal["read_only", "idempotent"] | None = None


@dataclass(frozen=True)
class NovaCallContext:
    """What one invocation carries to logs. Absent fields stay None and are not logged."""

    logical_agent: str | None = None
    runtime_instance: str | None = None
    runtime_kind: str | None = None
    attempt: int | None = None
    contract_version: str | None = None
    deadline_ms: int | None = None
    attempt_max_output_tokens: int | None = None
    attempt_max_cost_usd: int | None = None
    total_max_output_tokens: int | None = None
    total_max_cost_usd: int | None = None
    idempotency_key: str | None = None
    operation_class: str | None = None


_current: ContextVar[NovaCallContext | None] = ContextVar("nova_call_context", default=None)


def nova_log_fields() -> dict[str, Any]:
    """Log fields for the invocation in progress; empty outside an enveloped invocation."""
    context = _current.get()
    if context is None:
        return {}
    return {f.name: getattr(context, f.name) for f in fields(context)
            if getattr(context, f.name) is not None}


def set_call_context(context: NovaCallContext) -> Token:
    return _current.set(context)


def reset_call_context(token: Token) -> None:
    _current.reset(token)


def instance_identity() -> tuple[str, str]:
    """(instance id, runtime kind) from the environment. These are identity, not feature flags.

    Raises ValueError for a malformed value so the adapter can refuse before any work.
    """
    instance = os.getenv("NOVA_INSTANCE_ID", "").strip() or DEFAULT_INSTANCE_ID
    runtime = os.getenv("NOVA_RUNTIME", "").strip() or DEFAULT_RUNTIME
    if not _INSTANCE_RE.fullmatch(instance):
        raise ValueError("NOVA_INSTANCE_ID is malformed")
    if runtime not in RUNTIME_KINDS:
        raise ValueError("NOVA_RUNTIME is not a known runtime kind")
    return instance, runtime


def not_accepted_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Headers for a refusal made BEFORE any work starts (validation, authentication, startup).

    Never attach these to a response once the handler, an inference call or a memory write
    has started: an error after work began is a failure after running, not a refusal.
    """
    return {**(extra or {}), NOT_ACCEPTED_HEADER: "1"}
