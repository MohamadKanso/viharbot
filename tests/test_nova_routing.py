"""Deterministic, offline tests for the Nova routing decision engine (ADR-008)."""

import copy
import json
from pathlib import Path

import pytest

from viharbot.deadline import RequestDeadline
from viharbot.nova_routing import (
    EVIDENCE_ITEMS,
    EVIDENCE_SCOPE,
    Action,
    AttemptDefaults,
    CircuitBreaker,
    ManifestError,
    NovaRouter,
    Outcome,
    RoutingRequest,
    TransportResult,
    classify,
    load_manifest,
    next_action,
    parse_manifest,
    retry_budget_admitted,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "nova" / "instances.example.json"
PRIMARY, SECONDARY = "nova-agentcore-01", "nova-fargate-01"
GOOD_BUDGET = {
    "attempt_max_output_tokens": 1000, "total_max_output_tokens": 2000,
    "attempt_max_cost_usd": 100_000, "total_max_cost_usd": 200_000,
}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def manifest_dict(metered=True, classes=("read_only",), honors=False, secondary_metered=True):
    raw = json.loads(EXAMPLE.read_text())
    for inst in raw["instances"]:
        inst["operation_classes"] = list(classes)
        inst["honors_idempotency_key"] = honors
    if metered:
        targets = raw["instances"] if secondary_metered else raw["instances"][:1]
        for entry in targets:
            entry["budget_enforcement"] = "metered"
            entry["budget_evidence"] = {k: "recorded" for k in EVIDENCE_ITEMS + EVIDENCE_SCOPE}
    return raw


def manifest(**kw):
    return parse_manifest(manifest_dict(**kw))


def ok(instance_id):
    return TransportResult("response", 200, (), instance_id, True, result_present=True)


def refused(instance_id, status=503):
    return TransportResult("response", status, (("X-Nova-Not-Accepted", "1"),), instance_id, True)


PRE_SEND = TransportResult("pre_send_failure")
TIMEOUT = TransportResult("post_send_failure")


def app_error(instance_id):
    return TransportResult("response", 500, (), instance_id, True, application_error=True)


def gateway(status):
    return TransportResult("response", status, (), None, False)


class Scripted:
    """Fake transport: per-instance scripted results; records every send."""

    def __init__(self, **script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def send(self, instance, request, attempt, timeout_s):
        self.calls.append((instance.instance_id, request.request_id, attempt, timeout_s))
        return self.script[instance.instance_id].pop(0)


def request(op="read_only", seconds=60.0, clock=None, budget=GOOD_BUDGET, key=None):
    clock = clock or Clock()
    return RoutingRequest(
        "req-1", op, RequestDeadline.after(seconds, clock), "internal",
        idempotency_key=key, budget=budget,
    )


def router(transport, m=None, eligible=lambda i, r: True, **kw):
    return NovaRouter(m or manifest(), transport, eligible, **kw)


# ---------------------------------------------------------------- manifest


def test_example_manifest_loads_and_is_fake():
    m = load_manifest(str(EXAMPLE))
    assert [i.instance_id for i in m.instances] == [PRIMARY, SECONDARY]
    assert m.primary.budget_enforcement == "none"
    text = EXAMPLE.read_text()
    assert "example.invalid" in text and "000000000000" in text


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(version=2),
    lambda r: r.update(extra=1),
    lambda r: r.update(logical_agent=""),
    lambda r: r.update(instances=[]),
    lambda r: r.update(instances=r["instances"] * 2),
    lambda r: r["instances"][1].update(instance_id=PRIMARY),
    lambda r: r["instances"][1].update(priority=0),
    lambda r: r["instances"][0].update(runtime_kind="eks"),
    lambda r: r["instances"][0].update(priority=-1),
    lambda r: r["instances"][0].update(priority=True),
    lambda r: r["instances"][0].update(endpoint="https://x.example.invalid"),
    lambda r: r["instances"][0].update(arn="not-an-arn"),
    lambda r: r["instances"][1].update(endpoint="http://plain.example.invalid"),
    lambda r: r["instances"][0].update(auth_type="none"),
    lambda r: r["instances"][0].update(operation_classes=[]),
    lambda r: r["instances"][0].update(operation_classes=["write"]),
    lambda r: r["instances"][0].update(budget_enforcement="maybe"),
    lambda r: r["instances"][0].update(honors_idempotency_key="yes"),
    lambda r: r["instances"][0].update(surprise=True),
    lambda r: r["instances"][0].pop("region"),
    lambda r: r["instances"][0].update(budget_enforcement="metered"),
    lambda r: r["instances"][0].update(budget_enforcement="metered", budget_evidence={}),
])
def test_malformed_manifest_fails_closed(mutate):
    raw = manifest_dict(metered=False)
    mutate(raw)
    with pytest.raises(ManifestError):
        parse_manifest(raw)


def test_manifest_must_be_an_object_and_file_errors_fail_closed(tmp_path):
    with pytest.raises(ManifestError):
        parse_manifest([])
    with pytest.raises(ManifestError):
        load_manifest(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ManifestError):
        load_manifest(str(bad))


def test_metered_requires_every_evidence_field():
    for name in EVIDENCE_ITEMS + EVIDENCE_SCOPE:
        raw = manifest_dict()
        del raw["instances"][0]["budget_evidence"][name]
        with pytest.raises(ManifestError):
            parse_manifest(raw)
    assert manifest().primary.budget_enforcement == "metered"


def test_instances_sorted_by_priority():
    raw = manifest_dict(metered=False)
    raw["instances"].reverse()
    assert parse_manifest(raw).primary.instance_id == PRIMARY


# ---------------------------------------------------------------- breaker


def test_breaker_open_halfopen_failback_with_injected_clock():
    clock = Clock()
    b = CircuitBreaker(failure_threshold=2, cooldown_s=10, healthy_probes_for_failback=2, clock=clock)
    assert b.state == "closed"
    b.record_attempt(False)
    b.record_attempt(True)  # resets the consecutive count
    b.record_attempt(False)
    assert b.state == "closed"
    b.record_attempt(False)
    assert b.state == "open"
    b.record_probe(True)  # ignored while open and cooling down
    clock.now = 9.9
    assert b.state == "open"
    clock.now = 10
    assert b.state == "half_open"
    b.record_attempt(True)  # real requests never close it
    assert b.state == "half_open"
    b.record_probe(True)
    b.record_probe(False)  # resets
    assert b.state == "open"
    clock.now = 25
    assert b.state == "half_open"
    b.record_probe(True)
    assert b.state == "half_open"
    b.record_probe(True)
    assert b.state == "closed"


def test_breaker_closed_unhealthy_probe_counts_as_failure():
    b = CircuitBreaker(failure_threshold=1, clock=Clock())
    b.record_probe(True)
    assert b.state == "closed"
    b.record_probe(False)
    assert b.state == "open"


def test_failback_never_mid_request():
    clock = Clock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=5, healthy_probes_for_failback=1,
                             clock=clock)
    breaker.record_attempt(False)

    class Probing(Scripted):
        def send(self, instance, req, attempt, timeout_s):
            clock.now = 100  # cooldown elapses and a probe could now close the breaker
            breaker.record_probe(True)
            return super().send(instance, req, attempt, timeout_s)

    t = Probing(**{SECONDARY: [ok(SECONDARY)]})
    r = router(t, breakers={PRIMARY: breaker}).route(request())
    assert [c[0] for c in t.calls] == [SECONDARY]  # target was fixed at the start
    assert r.decisions[0].breaker_state == "closed"
    assert breaker.state == "closed"  # failback landed after the request, for the next one


# ---------------------------------------------------------------- classification


def inst(kind=PRIMARY):
    return {i.instance_id: i for i in manifest().instances}[kind]


@pytest.mark.parametrize("result,expected", [
    (PRE_SEND, Outcome.NOT_ACCEPTED),
    (TIMEOUT, Outcome.UNKNOWN),
    (TransportResult("weird"), Outcome.UNKNOWN),
    (refused(PRIMARY, 429), Outcome.NOT_ACCEPTED),
    (gateway(429), Outcome.UNKNOWN),
    (gateway(409), Outcome.UNKNOWN),
    (gateway(502), Outcome.UNKNOWN),
    (gateway(503), Outcome.UNKNOWN),
    (gateway(504), Outcome.UNKNOWN),
    (TransportResult("response", 503, (), PRIMARY, True), Outcome.UNKNOWN),
    (refused(SECONDARY), Outcome.UNKNOWN),  # marker from an unexpected instance
    (TransportResult("response", 503, (("X-Nova-Not-Accepted", "1"),), PRIMARY, False),
     Outcome.UNKNOWN),  # not authenticated
    (TransportResult("response", 200, (("X-Nova-Not-Accepted", "1"),), PRIMARY, True,
                     result_present=True), Outcome.UNKNOWN),  # marker beside a result body
    (TransportResult("response", 500, (("X-Nova-Not-Accepted", "1"),), PRIMARY, True,
                     application_error=True), Outcome.UNKNOWN),
    (TransportResult("response", 503, (("X-Nova-Not-Accepted", "0"),), PRIMARY, True),
     Outcome.UNKNOWN),
    (TransportResult("response", 503, (("X-Nova-Not-Accepted", "1"),
                                       ("x-nova-not-accepted", "1")), PRIMARY, True),
     Outcome.UNKNOWN),  # duplicated marker
    (app_error(PRIMARY), Outcome.FAILED_AFTER_RUNNING),
    (app_error(SECONDARY), Outcome.UNKNOWN),
    (ok(PRIMARY), Outcome.COMPLETED),
    (ok(SECONDARY), Outcome.UNKNOWN),
    (TransportResult("response", 200, (), PRIMARY, True), Outcome.UNKNOWN),  # no result body
])
def test_classification(result, expected):
    assert classify(result, inst(PRIMARY)) is expected


def test_body_marker_only_trusted_for_agentcore():
    body = TransportResult("response", 503, (), PRIMARY, True, body_not_accepted=True)
    assert classify(body, inst(PRIMARY)) is Outcome.NOT_ACCEPTED
    fargate = TransportResult("response", 503, (), SECONDARY, True, body_not_accepted=True)
    assert classify(fargate, inst(SECONDARY)) is Outcome.UNKNOWN


# ---------------------------------------------------------------- decision table


@pytest.mark.parametrize("outcome,op,shares,honors,key,expected", [
    (Outcome.NOT_ACCEPTED, "read_only", False, False, False, Action.FAIL_OVER),
    (Outcome.NOT_ACCEPTED, "idempotent", False, False, False, Action.FAIL_OVER),
    (Outcome.NOT_ACCEPTED, "stateful", True, False, False, Action.FAIL_OVER),
    (Outcome.NOT_ACCEPTED, "stateful", False, False, False, Action.FAIL),
    (Outcome.NOT_ACCEPTED, "side-effecting", False, True, True, Action.FAIL_OVER),
    (Outcome.NOT_ACCEPTED, "side-effecting", False, True, False, Action.FAIL),
    (Outcome.NOT_ACCEPTED, "side-effecting", False, False, True, Action.FAIL),
    (Outcome.UNKNOWN, "read_only", False, False, False, Action.RETRY_IF_ADMITTED),
    (Outcome.UNKNOWN, "idempotent", False, False, True, Action.FAIL),
    (Outcome.UNKNOWN, "idempotent", False, True, False, Action.FAIL),
    (Outcome.UNKNOWN, "idempotent", False, True, True, Action.RETRY_IF_ADMITTED),
    (Outcome.UNKNOWN, "stateful", True, True, True, Action.FAIL),
    (Outcome.UNKNOWN, "side-effecting", True, True, True, Action.FAIL),
    (Outcome.FAILED_AFTER_RUNNING, "read_only", True, True, True, Action.FAIL),
    (Outcome.COMPLETED, "read_only", True, True, True, Action.FAIL),
])
def test_decision_table(outcome, op, shares, honors, key, expected):
    m = manifest(honors=honors)
    secondary = m.instances[1]
    assert next_action(outcome, op, secondary, key, shares) is expected


# ---------------------------------------------------------------- budget admission

DEFAULTS = AttemptDefaults(output_tokens=1000, cost_micro_usd=100_000)


@pytest.mark.parametrize("budget,expected", [
    (GOOD_BUDGET, True),
    ({**GOOD_BUDGET, "total_max_output_tokens": 1999}, False),
    ({**GOOD_BUDGET, "total_max_cost_usd": 199_999}, False),
    ({**GOOD_BUDGET, "total_max_output_tokens": 2001, "total_max_cost_usd": 200_001}, True),
    ({"attempt_max_output_tokens": 1000, "attempt_max_cost_usd": 100_000}, False),  # totals absent
    (None, False),
    ("nope", False),
    ({}, False),  # no totals, defaults give one attempt
    ({"total_max_output_tokens": 2000, "total_max_cost_usd": 200_000}, True),  # attempt defaults
    ({**GOOD_BUDGET, "attempt_max_output_tokens": 0}, False),
    ({**GOOD_BUDGET, "attempt_max_cost_usd": -5}, False),
    ({**GOOD_BUDGET, "total_max_output_tokens": 2000.0}, False),
    ({**GOOD_BUDGET, "total_max_cost_usd": "200000"}, False),
    ({**GOOD_BUDGET, "attempt_max_output_tokens": True}, False),
    ({**GOOD_BUDGET, "total_max_cost_usd": 0}, False),
])
def test_retry_budget_admission(budget, expected):
    assert retry_budget_admitted(budget, DEFAULTS) is expected


def test_budget_without_defaults_or_with_spend():
    only_totals = {"total_max_output_tokens": 5000, "total_max_cost_usd": 500_000}
    assert retry_budget_admitted(only_totals) is False  # no attempt value anywhere
    assert retry_budget_admitted(GOOD_BUDGET, DEFAULTS, spent_tokens=1) is False
    assert retry_budget_admitted(GOOD_BUDGET, DEFAULTS, spent_cost_micro_usd=1) is False
    assert retry_budget_admitted(GOOD_BUDGET, DEFAULTS, spent_tokens=-1) is False
    big = {**GOOD_BUDGET, "total_max_output_tokens": 3000, "total_max_cost_usd": 300_000}
    assert retry_budget_admitted(big, DEFAULTS, 1000, 100_000) is True


# ---------------------------------------------------------------- routing scenarios


def test_primary_available():
    t = Scripted(**{PRIMARY: [ok(PRIMARY)]})
    r = router(t).route(request())
    assert r.status == "completed" and r.error is None
    assert [c[0] for c in t.calls] == [PRIMARY]
    d = r.decisions[0]
    assert (d.routing_decision, d.attempt, d.failover_from, d.possible_duplicate) == (
        "initial_primary", 1, None, False)


def test_primary_unavailable_not_accepted_fails_over_without_budget():
    t = Scripted(**{PRIMARY: [PRE_SEND], SECONDARY: [ok(SECONDARY)]})
    r = router(t, manifest(metered=False)).route(request(budget=None))
    assert r.status == "completed"
    assert [c[0] for c in t.calls] == [PRIMARY, SECONDARY]
    d = r.decisions[1]
    assert (d.failover_from, d.failover_reason, d.routing_decision, d.possible_duplicate) == (
        PRIMARY, "not_accepted", "failover_not_accepted", False)


def test_marker_refusal_fails_over():
    t = Scripted(**{PRIMARY: [refused(PRIMARY, 429)], SECONDARY: [ok(SECONDARY)]})
    assert router(t).route(request()).status == "completed"


def test_unknown_outcome_retries_once_when_admitted():
    t = Scripted(**{PRIMARY: [TIMEOUT], SECONDARY: [ok(SECONDARY)]})
    r = router(t).route(request())
    assert r.status == "completed"
    d = r.decisions[1]
    assert (d.failover_reason, d.routing_decision, d.possible_duplicate) == (
        "unknown_outcome", "retry_after_unknown", True)


@pytest.mark.parametrize("budget", [None, {**GOOD_BUDGET, "total_max_output_tokens": 1999}, {}])
def test_unknown_outcome_not_retried_without_budget(budget):
    t = Scripted(**{PRIMARY: [gateway(504)]})
    r = router(t).route(request(budget=budget))
    assert (r.status, r.error) == ("failed", "retry_budget_not_admitted")
    assert [c[0] for c in t.calls] == [PRIMARY]
    assert r.decisions[-1].possible_duplicate == "unknown"


def test_unknown_outcome_not_retried_when_primary_not_metered():
    t = Scripted(**{PRIMARY: [TIMEOUT]})
    r = router(t, manifest(metered=False)).route(request())
    assert r.error == "retry_budget_not_admitted" and len(t.calls) == 1


def test_unknown_outcome_not_retried_on_unmetered_secondary():
    t = Scripted(**{PRIMARY: [TIMEOUT]})
    r = router(t, manifest(secondary_metered=False)).route(request())
    assert r.error == "retry_budget_not_admitted" and len(t.calls) == 1
    # Reverse direction: start on the unmetered secondary (primary breaker open).
    breaker = CircuitBreaker(failure_threshold=1, clock=Clock())
    breaker.record_attempt(False)
    t = Scripted(**{SECONDARY: [TIMEOUT]})
    r = router(t, manifest(secondary_metered=False), breakers={PRIMARY: breaker}).route(request())
    assert r.error == "retry_budget_not_admitted" and len(t.calls) == 1


def test_secondary_unavailable_after_failover_fails_with_two_attempts_at_most():
    t = Scripted(**{PRIMARY: [TIMEOUT], SECONDARY: [TIMEOUT]})
    r = router(t).route(request(budget={**GOOD_BUDGET, "total_max_output_tokens": 5000,
                                        "total_max_cost_usd": 500_000}))
    assert (r.status, r.error) == ("failed", "attempts_exhausted")
    assert len(t.calls) == 2  # never a third attempt
    assert r.decisions[-1].possible_duplicate == "unknown"


def test_terminal_record_keeps_uncertainty_from_unknown_first_attempt():
    big = {**GOOD_BUDGET, "total_max_output_tokens": 5000, "total_max_cost_usd": 500_000}
    t = Scripted(**{PRIMARY: [TIMEOUT], SECONDARY: [app_error(SECONDARY)]})
    r = router(t).route(request(budget=big))
    assert (r.error, r.decisions[-1].possible_duplicate) == ("failure_after_running", True)
    t = Scripted(**{PRIMARY: [TIMEOUT], SECONDARY: [PRE_SEND]})
    r = router(t).route(request(budget=big))
    assert (r.error, r.decisions[-1].possible_duplicate) == ("attempts_exhausted", "unknown")


def test_secondary_unavailable_after_not_accepted():
    t = Scripted(**{PRIMARY: [PRE_SEND], SECONDARY: [PRE_SEND]})
    r = router(t).route(request())
    assert (r.error, r.decisions[-1].possible_duplicate) == ("attempts_exhausted", False)


def test_secondary_available_when_primary_breaker_open():
    breaker = CircuitBreaker(failure_threshold=1, clock=Clock())
    breaker.record_attempt(False)
    t = Scripted(**{SECONDARY: [ok(SECONDARY)]})
    r = router(t, breakers={PRIMARY: breaker}).route(request())
    assert r.status == "completed" and r.decisions[0].routing_decision == "initial_secondary"
    assert r.decisions[0].breaker_state == "closed"


def test_all_breakers_open_still_tries_in_priority_order():
    breakers = {}
    for name in (PRIMARY, SECONDARY):
        b = CircuitBreaker(failure_threshold=1, clock=Clock())
        b.record_attempt(False)
        breakers[name] = b
    t = Scripted(**{PRIMARY: [ok(PRIMARY)]})
    r = router(t, breakers=breakers).route(request())
    assert r.decisions[0].routing_decision == "no_closed_breaker"
    assert r.decisions[0].breaker_state == "open"


def test_breaker_updated_from_attempts():
    breaker = CircuitBreaker(failure_threshold=1, clock=Clock())
    t = Scripted(**{PRIMARY: [PRE_SEND], SECONDARY: [ok(SECONDARY)]})
    router(t, breakers={PRIMARY: breaker}).route(request())
    assert breaker.state == "open"


def test_application_error_is_not_failed_over_and_keeps_breaker_closed():
    breaker = CircuitBreaker(failure_threshold=1, clock=Clock())
    t = Scripted(**{PRIMARY: [app_error(PRIMARY)]})
    r = router(t, breakers={PRIMARY: breaker}).route(request())
    assert (r.status, r.error) == ("failed", "failure_after_running")
    assert r.final.application_error and len(t.calls) == 1
    assert breaker.state == "closed"


def test_timeout_handling_passes_remaining_deadline_and_never_hedges():
    clock = Clock()

    class Slow(Scripted):
        def send(self, instance, req, attempt, timeout_s):
            result = super().send(instance, req, attempt, timeout_s)
            clock.now += 10  # the attempt consumed time
            return result

    t = Slow(**{PRIMARY: [TIMEOUT], SECONDARY: [ok(SECONDARY)]})
    r = router(t).route(request(seconds=60, clock=clock))
    assert r.status == "completed"
    assert [c[3] for c in t.calls] == [60.0, 50.0]
    assert [c[2] for c in t.calls] == [1, 2]  # strictly sequential attempts
    assert [d.deadline_remaining_ms for d in r.decisions] == [60000, 50000]


def test_deadline_exhausted_before_first_attempt():
    clock = Clock()
    req = request(seconds=0, clock=clock)
    t = Scripted()
    r = router(t).route(req)
    assert (r.error, t.calls) == ("deadline_exhausted", [])
    assert r.decisions[0].deadline_remaining_ms == 0


def test_deadline_exhausted_blocks_retry_and_failover():
    for first, op_error in ((TIMEOUT, "deadline_exhausted"), (PRE_SEND, "deadline_exhausted")):
        clock = Clock()

        class Slow(Scripted):
            def send(self, instance, req, attempt, timeout_s):
                result = super().send(instance, req, attempt, timeout_s)
                clock.now += 100
                return result

        t = Slow(**{PRIMARY: [first]})
        r = router(t).route(request(seconds=30, clock=clock))
        assert r.error == op_error and len(t.calls) == 1


class TickingBreaker(CircuitBreaker):
    """Consumes time when its state is read, as a slow breaker inspection would."""

    def __init__(self, clock, step):
        super().__init__(clock=clock)
        self.ticker, self.step = clock, step

    @property
    def state(self):
        self.ticker.now += self.step
        return super().state


class TickingBudget(dict):
    """A budget whose inspection consumes time, as a slow retry preparation would."""

    def __init__(self, clock, step, *a):
        super().__init__(*a)
        self.clock, self.step = clock, step

    def __contains__(self, key):
        self.clock.now += self.step
        return super().__contains__(key)


def test_deadline_expiring_between_selection_and_send_never_reaches_transport():
    clock = Clock()
    breakers = {PRIMARY: TickingBreaker(clock, 60), SECONDARY: TickingBreaker(clock, 0)}
    t = Scripted()
    r = router(t, breakers=breakers).route(request(seconds=30, clock=clock))
    assert (r.status, r.error, t.calls) == ("failed", "deadline_exhausted", [])
    last = r.decisions[-1]
    assert (last.routing_decision, last.deadline_remaining_ms, last.possible_duplicate) == (
        "deadline_exhausted", 0, False)


def test_deadline_exactly_zero_never_reaches_transport():
    clock = Clock()
    t = Scripted()
    r = router(t).route(request(seconds=0, clock=clock))
    assert (r.error, t.calls) == ("deadline_exhausted", [])


def test_deadline_expiring_before_retry_send_sends_nothing_more():
    clock = Clock()
    budget = TickingBudget(clock, 100, GOOD_BUDGET)
    t = Scripted(**{PRIMARY: [TIMEOUT]})
    r = router(t).route(request(seconds=30, clock=clock, budget=budget))
    assert r.error == "deadline_exhausted" and len(t.calls) == 1
    assert all(c[3] > 0 for c in t.calls)
    assert r.decisions[-1].possible_duplicate == "unknown"
    assert r.decisions[-1].attempt == 1


def test_idempotent_unknown_outcome_never_retried_today():
    m = manifest(classes=("read_only", "idempotent"))
    t = Scripted(**{PRIMARY: [TIMEOUT]})
    r = router(t, m).route(request(op="idempotent", key="k1"))
    assert r.error == "failover_not_allowed" and len(t.calls) == 1


def test_idempotent_unknown_outcome_retried_when_target_honors_key():
    m = manifest(classes=("read_only", "idempotent"), honors=True)
    t = Scripted(**{PRIMARY: [TIMEOUT], SECONDARY: [ok(SECONDARY)]})
    r = router(t, m).route(request(op="idempotent", key="k1"))
    assert r.status == "completed"
    assert router(Scripted(**{PRIMARY: [TIMEOUT]}), m).route(
        request(op="idempotent", key=None)).error == "failover_not_allowed"


@pytest.mark.parametrize("op", ["stateful", "side-effecting"])
def test_stateful_and_side_effecting_unknown_never_retried(op):
    m = manifest(classes=OPERATION_CLASSES_ALL, honors=True)
    t = Scripted(**{PRIMARY: [TIMEOUT]})
    r = router(t, m, shares_state=lambda i: True).route(request(op=op, key="k"))
    assert r.error == "failover_not_allowed" and len(t.calls) == 1


OPERATION_CLASSES_ALL = ("read_only", "idempotent", "stateful", "side-effecting")


def test_stateful_initial_selection_on_secondary_needs_shared_state():
    m = manifest(classes=OPERATION_CLASSES_ALL)
    breaker = CircuitBreaker(failure_threshold=1, clock=Clock())
    breaker.record_attempt(False)  # primary breaker open
    t = Scripted(**{PRIMARY: [PRE_SEND]})
    r = router(t, m, breakers={PRIMARY: breaker}).route(request(op="stateful"))
    assert [c[0] for c in t.calls] == [PRIMARY]  # the secondary never received it
    assert r.error == "no_failover_target"
    t = Scripted(**{SECONDARY: [ok(SECONDARY)]})
    r = router(t, m, breakers={PRIMARY: breaker}, shares_state=lambda i: True).route(
        request(op="stateful"))
    assert r.status == "completed" and [c[0] for c in t.calls] == [SECONDARY]


def test_stateful_not_accepted_needs_shared_state():
    m = manifest(classes=OPERATION_CLASSES_ALL)
    t = Scripted(**{PRIMARY: [PRE_SEND]})
    assert router(t, m).route(request(op="stateful")).error == "no_failover_target"
    t = Scripted(**{PRIMARY: [PRE_SEND], SECONDARY: [ok(SECONDARY)]})
    r = router(t, m, shares_state=lambda i: i.instance_id == SECONDARY).route(request(op="stateful"))
    assert r.status == "completed"


def test_side_effecting_not_accepted_needs_idempotency_key_and_honoring_target():
    m = manifest(classes=OPERATION_CLASSES_ALL, honors=True)
    t = Scripted(**{PRIMARY: [PRE_SEND], SECONDARY: [ok(SECONDARY)]})
    assert router(t, m).route(request(op="side-effecting", key="k")).status == "completed"
    t = Scripted(**{PRIMARY: [PRE_SEND]})
    assert router(t, m).route(request(op="side-effecting")).error == "failover_not_allowed"


# ---------------------------------------------------------------- authorization


def test_failover_never_widens_permissions():
    asked = []

    def only_primary(instance, req):
        asked.append((instance.instance_id, req.data_classification))
        return instance.instance_id == PRIMARY

    t = Scripted(**{PRIMARY: [PRE_SEND]})
    r = router(t, eligible=only_primary).route(request())
    assert (r.status, r.error) == ("failed", "no_failover_target")
    assert [c[0] for c in t.calls] == [PRIMARY]  # the unauthorized secondary never saw it
    assert (SECONDARY, "internal") in asked


def test_ineligible_primary_routes_only_to_authorized_secondary():
    t = Scripted(**{SECONDARY: [ok(SECONDARY)]})
    r = router(t, eligible=lambda i, req: i.instance_id == SECONDARY).route(request())
    assert r.status == "completed" and [c[0] for c in t.calls] == [SECONDARY]


def test_no_eligible_instance_and_operation_class_not_allowed():
    t = Scripted()
    assert router(t, eligible=lambda i, r: False).route(request()).error == "no_eligible_instance"
    assert router(t).route(request(op="stateful")).error == "no_eligible_instance"
    assert router(t).route(request(op="bogus")).error == "no_eligible_instance"
    assert t.calls == []


# ---------------------------------------------------------------- correlation and reporting


def test_request_correlation_and_log_safe_records():
    t = Scripted(**{PRIMARY: [TIMEOUT], SECONDARY: [ok(SECONDARY)]})
    r = router(t).route(request())
    assert {c[1] for c in t.calls} == {"req-1"}
    assert {d.request_id for d in r.decisions} == {"req-1"}
    record = r.decisions[1].to_log()
    assert set(record) == {
        "logical_agent", "request_id", "attempt", "runtime_instance", "runtime_kind",
        "failover_from", "failover_reason", "routing_decision", "breaker_state",
        "deadline_remaining_ms", "possible_duplicate",
    }
    assert (record["runtime_instance"], record["runtime_kind"]) == (SECONDARY, "fargate")


def test_failure_reporting_has_terminal_record():
    t = Scripted(**{PRIMARY: [app_error(PRIMARY)]})
    r = router(t).route(request())
    last = r.decisions[-1]
    assert (last.runtime_instance, last.routing_decision, last.attempt) == (
        None, "failure_after_running", 1)


def test_manifest_copy_is_unmodified_by_helpers():
    raw = manifest_dict()
    snapshot = copy.deepcopy(raw)
    parse_manifest(raw)
    assert raw == snapshot


@pytest.mark.parametrize("start_on_secondary", [False, True])
@pytest.mark.parametrize("shared", [False, True])
def test_stateful_not_accepted_moves_only_between_state_sharing_instances(
        start_on_secondary, shared):
    m = manifest(classes=OPERATION_CLASSES_ALL)
    start, other = (SECONDARY, PRIMARY) if start_on_secondary else (PRIMARY, SECONDARY)
    breakers = {}
    if start_on_secondary:
        breakers[PRIMARY] = CircuitBreaker(failure_threshold=1, clock=Clock())
        breakers[PRIMARY].record_attempt(False)  # initial selection lands on the secondary
    t = Scripted(**{start: [PRE_SEND], other: [ok(other)]})
    r = router(t, m, breakers=breakers,
               shares_state=lambda i: shared and i.instance_id == SECONDARY).route(
        request(op="stateful"))
    if start_on_secondary and not shared:
        # the secondary may not even receive stateful work without shared state
        assert [c[0] for c in t.calls] == [PRIMARY]
    elif shared:
        assert r.status == "completed" and [c[0] for c in t.calls] == [start, other]
    else:
        assert r.error == "no_failover_target" and [c[0] for c in t.calls] == [start]


@pytest.mark.parametrize("start_on_secondary", [False, True])
def test_stateful_unknown_outcome_never_fails_over_either_direction(start_on_secondary):
    m = manifest(classes=OPERATION_CLASSES_ALL)
    breakers = {}
    if start_on_secondary:
        breakers[PRIMARY] = CircuitBreaker(failure_threshold=1, clock=Clock())
        breakers[PRIMARY].record_attempt(False)
    start = SECONDARY if start_on_secondary else PRIMARY
    t = Scripted(**{start: [TIMEOUT]})
    r = router(t, m, breakers=breakers, shares_state=lambda i: True).route(
        request(op="stateful"))
    assert r.error == "failover_not_allowed" and [c[0] for c in t.calls] == [start]
