"""Nova routing and failover decision engine (ADR-008): pure logic, no network, no AWS.

This module decides which Nova runtime instance gets a request, whether a failed attempt may be
retried on the other instance, and what to record about it. It sends nothing itself: the caller
injects a `Transport`. It is not wired into any runtime path. See docs/nova-routing.md.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from viharbot.deadline import RequestDeadline

OPERATION_CLASSES = ("read_only", "idempotent", "stateful", "side-effecting")
RUNTIME_KINDS = ("agentcore", "fargate")
AUTH_TYPES = ("sigv4", "bearer")
BUDGET_ENFORCEMENT = ("metered", "none")
NOT_ACCEPTED_HEADER = "x-nova-not-accepted"
MAX_ATTEMPTS = 2

# The six acceptance items ADR-008 requires before `metered`, plus the scope the evidence covers.
EVIDENCE_ITEMS = (
    "rates_checked",
    "prompt_bound_checked",
    "max_tokens_honored",
    "settlement_checked",
    "concurrency_and_failure_cases_passed",
    "refusal_observed",
)
EVIDENCE_SCOPE = ("model_alias", "provider", "price_table_version", "meter_version")

_ARN = re.compile(r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/[\w.-]+$")
_INSTANCE_FIELDS = {
    "instance_id", "runtime_kind", "priority", "endpoint", "arn", "auth_type", "region",
    "contract_version", "operation_classes", "budget_enforcement", "budget_evidence",
    "honors_idempotency_key",
}


class ManifestError(ValueError):
    """The instance manifest is malformed. Callers must not route with a bad manifest."""


# ---------------------------------------------------------------- manifest


@dataclass(frozen=True)
class Instance:
    instance_id: str
    runtime_kind: str
    priority: int
    location: str  # endpoint URL or runtime ARN
    auth_type: str
    region: str
    contract_version: str
    operation_classes: tuple[str, ...]
    budget_enforcement: str
    honors_idempotency_key: bool = False


@dataclass(frozen=True)
class Manifest:
    logical_agent: str
    instances: tuple[Instance, ...]  # sorted by priority, primary first

    @property
    def primary(self) -> Instance:
        return self.instances[0]


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ManifestError(message)


def _text(obj: Mapping[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    _require(isinstance(value, str) and value.strip() != "", f"{where}: {key} must be a string")
    return value


def _parse_instance(raw: Any, index: int) -> Instance:
    where = f"instances[{index}]"
    _require(isinstance(raw, dict), f"{where} must be an object")
    _require(not set(raw) - _INSTANCE_FIELDS, f"{where}: unknown field")
    kind = _text(raw, "runtime_kind", where)
    _require(kind in RUNTIME_KINDS, f"{where}: runtime_kind must be one of {RUNTIME_KINDS}")
    priority = raw.get("priority")
    _require(
        isinstance(priority, int) and not isinstance(priority, bool) and priority >= 0,
        f"{where}: priority must be a non-negative integer",
    )
    _require(("endpoint" in raw) != ("arn" in raw), f"{where}: set exactly one of endpoint, arn")
    if kind == "agentcore":
        location = _text(raw, "arn", where)
        _require(_ARN.match(location) is not None, f"{where}: arn is not an AgentCore runtime ARN")
    else:
        location = _text(raw, "endpoint", where)
        _require(location.startswith("https://"), f"{where}: endpoint must be https")
    auth = _text(raw, "auth_type", where)
    _require(auth in AUTH_TYPES, f"{where}: auth_type must be one of {AUTH_TYPES}")
    classes = raw.get("operation_classes")
    _require(
        isinstance(classes, list) and bool(classes) and all(c in OPERATION_CLASSES for c in classes),
        f"{where}: operation_classes must be a non-empty list from {OPERATION_CLASSES}",
    )
    mode = _text(raw, "budget_enforcement", where)
    _require(mode in BUDGET_ENFORCEMENT, f"{where}: budget_enforcement must be metered or none")
    if mode == "metered":
        evidence = raw.get("budget_evidence")
        _require(isinstance(evidence, dict), f"{where}: metered requires budget_evidence")
        for name in EVIDENCE_ITEMS + EVIDENCE_SCOPE:
            _text(evidence, name, f"{where}.budget_evidence")
    honors = raw.get("honors_idempotency_key", False)
    _require(isinstance(honors, bool), f"{where}: honors_idempotency_key must be a boolean")
    return Instance(
        instance_id=_text(raw, "instance_id", where),
        runtime_kind=kind,
        priority=priority,
        location=location,
        auth_type=auth,
        region=_text(raw, "region", where),
        contract_version=_text(raw, "contract_version", where),
        operation_classes=tuple(classes),
        budget_enforcement=mode,
        honors_idempotency_key=honors,
    )


def parse_manifest(raw: Any) -> Manifest:
    """Validate a decoded manifest. Any defect raises ManifestError (fail closed)."""
    _require(isinstance(raw, dict), "manifest must be an object")
    _require(not set(raw) - {"version", "logical_agent", "instances"}, "unknown top-level field")
    _require(raw.get("version") == 1, "unsupported manifest version")
    logical = _text(raw, "logical_agent", "manifest")
    items = raw.get("instances")
    _require(isinstance(items, list) and 1 <= len(items) <= 2, "instances must list one or two")
    parsed = [_parse_instance(r, i) for i, r in enumerate(items)]
    instances = sorted(parsed, key=lambda inst: inst.priority)
    _require(len({i.instance_id for i in instances}) == len(instances), "duplicate instance_id")
    _require(len({i.priority for i in instances}) == len(instances), "duplicate priority")
    return Manifest(logical_agent=logical, instances=tuple(instances))


def load_manifest(path: str) -> Manifest:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest: {type(exc).__name__}") from exc
    return parse_manifest(raw)


# ---------------------------------------------------------------- circuit breaker

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


class CircuitBreaker:
    """Passive health for one instance. It only influences the INITIAL target of a request.

    Closed until `failure_threshold` consecutive failed attempts; open for `cooldown_s`; then
    half-open, where only health probes count. `healthy_probes_for_failback` consecutive healthy
    probes close it (failback). Failback is a probe-driven transition, never a side effect of a
    request, and a request reads the state once, so it cannot change mid-request.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_s: float = 30.0,
        healthy_probes_for_failback: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_s
        self._probes_needed = healthy_probes_for_failback
        self._clock = clock
        self._state = CLOSED
        self._failures = 0
        self._healthy = 0
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        if self._state == OPEN and self._clock() - self._opened_at >= self._cooldown:
            self._state = HALF_OPEN
            self._healthy = 0
        return self._state

    def _trip(self) -> None:
        self._state = OPEN
        self._opened_at = self._clock()
        self._failures = 0
        self._healthy = 0

    def record_attempt(self, ok: bool) -> None:
        """Passive signal from a real request. Requests never close the breaker."""
        if self.state != CLOSED:
            return
        self._failures = 0 if ok else self._failures + 1
        if self._failures >= self._threshold:
            self._trip()

    def record_probe(self, healthy: bool) -> None:
        """Result of a health probe supplied by the caller (this module sends none)."""
        state = self.state
        if state == CLOSED:
            if not healthy:
                self.record_attempt(False)
        elif state == HALF_OPEN:
            if not healthy:
                self._trip()
                return
            self._healthy += 1
            if self._healthy >= self._probes_needed:
                self._state, self._failures, self._healthy = CLOSED, 0, 0


# ---------------------------------------------------------------- outcome classification


class Outcome(str, Enum):
    COMPLETED = "completed"
    NOT_ACCEPTED = "known_not_accepted"
    UNKNOWN = "unknown_outcome"
    FAILED_AFTER_RUNNING = "known_failure_after_running"


@dataclass(frozen=True)
class TransportResult:
    """What the transport observed. It carries no prompt text.

    kind: "response", "pre_send_failure" (DNS, refused, connect or TLS setup failed, nothing
    written) or "post_send_failure" (timeout, reset or lost response after bytes were sent).
    """

    kind: str
    status: int | None = None
    headers: tuple[tuple[str, str], ...] = ()
    responder_instance: str | None = None  # from the authenticated channel, not the body
    authenticated: bool = False
    result_present: bool = False
    application_error: bool = False
    body_not_accepted: bool = False  # AgentCore fallback for a header it may not pass through


def classify(result: TransportResult, instance: Instance) -> Outcome:
    """ADR-008 classification. Anything not provably one of the other cases is UNKNOWN."""
    if result.kind == "pre_send_failure":
        return Outcome.NOT_ACCEPTED
    if result.kind != "response":
        return Outcome.UNKNOWN
    trusted = result.authenticated and result.responder_instance == instance.instance_id
    markers = [v for k, v in result.headers if k.lower() == NOT_ACCEPTED_HEADER]
    if markers or result.body_not_accepted:
        body_ok = not markers and instance.runtime_kind == "agentcore"
        valid = (
            (markers == ["1"] or body_ok)
            and trusted
            and not result.result_present
            and not result.application_error
        )
        return Outcome.NOT_ACCEPTED if valid else Outcome.UNKNOWN
    if trusted and result.application_error:
        return Outcome.FAILED_AFTER_RUNNING
    ok_status = result.status is not None and 200 <= result.status < 300
    if trusted and result.result_present and ok_status:
        return Outcome.COMPLETED
    return Outcome.UNKNOWN


# ---------------------------------------------------------------- budget admission


@dataclass(frozen=True)
class AttemptDefaults:
    """The agent's configured per-attempt defaults, in tokens and micro-USD."""

    output_tokens: int | None = None
    cost_micro_usd: int | None = None


def _valid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _unit(
    budget: Mapping[str, Any], attempt_key: str, total_key: str, default: int | None
) -> tuple[int, int] | None:
    """Return (attempt, total) for one unit, or None when either is not established.

    Absent attempt takes the configured default; absent total takes the attempt (one attempt).
    A present but zero, negative, non-integer or unparseable value is not established.
    """
    attempt = budget[attempt_key] if attempt_key in budget else default
    if not _valid(attempt):
        return None
    total = budget[total_key] if total_key in budget else attempt
    if not _valid(total):
        return None
    return attempt, total


def retry_budget_admitted(
    budget: Mapping[str, Any] | None,
    defaults: AttemptDefaults = AttemptDefaults(),
    spent_tokens: int = 0,
    spent_cost_micro_usd: int = 0,
) -> bool:
    """May a retry follow an unknown outcome? Tokens and cost are checked separately.

    remaining = total - spent_by_resolved_attempts - full attempt allowance reserved for the
    unresolved primary; admitted only if remaining >= attempt. Absent or invalid values refuse.
    """
    if not isinstance(budget, Mapping):
        return False
    units = (
        ("attempt_max_output_tokens", "total_max_output_tokens", defaults.output_tokens,
         spent_tokens),
        ("attempt_max_cost_usd", "total_max_cost_usd", defaults.cost_micro_usd,
         spent_cost_micro_usd),
    )
    for attempt_key, total_key, default, spent in units:
        pair = _unit(budget, attempt_key, total_key, default)
        if pair is None or spent < 0:
            return False
        attempt, total = pair
        if total - spent - attempt < attempt:
            return False
    return True


# ---------------------------------------------------------------- decision table


class Action(str, Enum):
    FAIL_OVER = "fail_over"
    RETRY_IF_ADMITTED = "retry_if_admitted"  # unknown outcome: also needs deadline and budget
    FAIL = "fail"


def next_action(
    outcome: Outcome,
    operation_class: str,
    secondary: Instance,
    has_idempotency_key: bool,
    secondary_shares_state: bool = False,
) -> Action:
    """The ADR-008 table. Eligibility, deadline, budget and attempt count are checked elsewhere."""
    if outcome in (Outcome.COMPLETED, Outcome.FAILED_AFTER_RUNNING):
        return Action.FAIL
    dedup = secondary.honors_idempotency_key and has_idempotency_key
    if outcome is Outcome.NOT_ACCEPTED:
        if operation_class in ("read_only", "idempotent"):
            return Action.FAIL_OVER
        if operation_class == "stateful":
            return Action.FAIL_OVER if secondary_shares_state else Action.FAIL
        return Action.FAIL_OVER if dedup else Action.FAIL
    if operation_class == "read_only":
        return Action.RETRY_IF_ADMITTED
    if operation_class == "idempotent" and dedup:
        return Action.RETRY_IF_ADMITTED
    return Action.FAIL


# ---------------------------------------------------------------- routing


@dataclass(frozen=True)
class RoutingRequest:
    request_id: str
    operation_class: str
    deadline: RequestDeadline
    data_classification: str
    logical_agent: str = "nova"
    idempotency_key: str | None = None
    budget: Mapping[str, Any] | None = None


class Transport(Protocol):
    def send(
        self, instance: Instance, request: RoutingRequest, attempt: int, timeout_s: float
    ) -> TransportResult: ...


Eligibility = Callable[[Instance, RoutingRequest], bool]


@dataclass(frozen=True)
class RoutingDecision:
    """One log-safe record per attempt, plus a terminal record when routing stops."""

    logical_agent: str
    request_id: str
    attempt: int
    runtime_instance: str | None
    runtime_kind: str | None
    failover_from: str | None
    failover_reason: str | None
    routing_decision: str
    breaker_state: str | None
    deadline_remaining_ms: int
    possible_duplicate: bool | str  # True, False, or "unknown"

    def to_log(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class RoutingResult:
    status: str  # "completed" or "failed"
    error: str | None
    final: TransportResult | None
    decisions: tuple[RoutingDecision, ...] = field(default_factory=tuple)


class NovaRouter:
    def __init__(
        self,
        manifest: Manifest,
        transport: Transport,
        eligible: Eligibility,
        breakers: Mapping[str, CircuitBreaker] | None = None,
        defaults: AttemptDefaults = AttemptDefaults(),
        shares_state: Callable[[Instance], bool] = lambda _inst: False,
    ) -> None:
        self._manifest = manifest
        self._transport = transport
        self._eligible = eligible
        self._breakers = dict(breakers or {})
        self._defaults = defaults
        self._shares_state = shares_state
        for inst in manifest.instances:
            self._breakers.setdefault(inst.instance_id, CircuitBreaker())

    def route(self, request: RoutingRequest) -> RoutingResult:
        log: list[RoutingDecision] = []

        def record(attempt, inst, from_id, reason, decision, breaker, dup) -> None:
            remaining = max(0, int(request.deadline.remaining() * 1000))
            log.append(RoutingDecision(
                request.logical_agent, request.request_id, attempt,
                inst.instance_id if inst else None, inst.runtime_kind if inst else None,
                from_id, reason, decision, breaker, remaining, dup,
            ))

        def stop(error: str, attempt: int, dup: bool | str, final=None) -> RoutingResult:
            record(attempt, None, None, None, error, None, dup)
            return RoutingResult("failed", error, final, tuple(log))

        def state_compatible(holder: Instance, other: Instance) -> bool:
            """May state held on `holder` be used on `other`? Symmetric over the pair.

            `shares_state(i)` says instance i shares the primary's state, so the pair is
            compatible when the non-primary side shares it, whichever side is the holder.
            """
            primary = self._manifest.primary
            if holder is other:
                return True
            if holder is primary:
                return self._shares_state(other)
            if other is primary:
                return self._shares_state(holder)
            return self._shares_state(holder) and self._shares_state(other)

        # Eligibility is evaluated per request and instance, so failover never widens it.
        # Stateful work may start on a non-primary only if it shares the primary's state.
        candidates = [
            i for i in self._manifest.instances
            if request.operation_class in i.operation_classes
            and self._eligible(i, request)
            and (
                request.operation_class != "stateful"
                or state_compatible(self._manifest.primary, i)
            )
        ]
        if not candidates:
            return stop("no_eligible_instance", 0, False)
        if request.deadline.expired():
            return stop("deadline_exhausted", 0, False)

        # The breaker is read once, here: it picks the initial target and nothing else.
        snapshot = {i.instance_id: self._breakers[i.instance_id].state for i in candidates}
        closed = [i for i in candidates if snapshot[i.instance_id] == CLOSED]
        target = (closed or candidates)[0]
        if not closed:
            decision = "no_closed_breaker"
        else:
            decision = "initial_primary" if target is self._manifest.primary else "initial_secondary"
        failover_from: str | None = None
        reason: str | None = None
        possible_dup: bool | str = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            # Re-checked immediately before every send: the transport never sees a timeout <= 0.
            remaining = request.deadline.remaining()
            if remaining <= 0:
                return stop("deadline_exhausted", attempt - 1,
                            "unknown" if possible_dup else False)
            record(attempt, target, failover_from, reason, decision,
                   snapshot[target.instance_id], possible_dup)
            result = self._transport.send(target, request, attempt, remaining)
            outcome = classify(result, target)
            self._breakers[target.instance_id].record_attempt(
                outcome in (Outcome.COMPLETED, Outcome.FAILED_AFTER_RUNNING)
            )
            if outcome is Outcome.COMPLETED:
                return RoutingResult("completed", None, result, tuple(log))
            if outcome is Outcome.FAILED_AFTER_RUNNING:
                return stop("failure_after_running", attempt, possible_dup, result)
            unknown = outcome is Outcome.UNKNOWN
            # Uncertainty is cumulative: an earlier unresolved attempt stays unresolved.
            dup: bool | str = "unknown" if unknown or possible_dup else False
            others = [i for i in candidates if i is not target]
            if attempt >= MAX_ATTEMPTS:
                return stop("attempts_exhausted", attempt, dup, result)
            if not others:
                return stop("no_failover_target", attempt, dup, result)
            secondary = others[0]
            action = next_action(
                outcome, request.operation_class, secondary,
                request.idempotency_key is not None, state_compatible(target, secondary),
            )
            if action is Action.FAIL:
                return stop("failover_not_allowed", attempt, dup, result)
            if request.deadline.expired():
                return stop("deadline_exhausted", attempt, dup, result)
            if unknown and not (
                target.budget_enforcement == "metered"
                and secondary.budget_enforcement == "metered"
                and retry_budget_admitted(request.budget, self._defaults)
            ):
                return stop("retry_budget_not_admitted", attempt, dup, result)
            failover_from, target = target.instance_id, secondary
            reason = "unknown_outcome" if unknown else "not_accepted"
            decision = "retry_after_unknown" if unknown else "failover_not_accepted"
            possible_dup = possible_dup or unknown
        raise AssertionError("unreachable: the loop returns on its final attempt")  # pragma: no cover
