"""Steward config, GitHub authentication, deduplication state, audit chain and alert formatting.
Offline: fake AWS clients and a fake token opener. No GitHub, no AWS, no model."""

from __future__ import annotations

import base64
import datetime
import io
import json
import urllib.error

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from steward_fakes import Clock, FakeDynamo, FakeSecrets, FakeSns

from viharbot.steward import github_auth as auth
from viharbot.steward.alerts import AlertUnavailable, SnsPublisher, format_alert
from viharbot.steward.audit import (
    FIELDS,
    AuditIntegrityError,
    AuditLog,
    AuditUnavailable,
    build_body,
    verify_chain,
)
from viharbot.steward.config import ConfigError, StewardConfig, from_env
from viharbot.steward.drafts import DraftDecision
from viharbot.steward.model import OUTSTANDING, Observation, PullAssessment, Requirement
from viharbot.steward.state import NEW, RETRY, SEEN, ObservationState, StateUnavailable

NOW = datetime.datetime(2026, 11, 5, 4, 0, 0, tzinfo=datetime.timezone.utc)
TOPIC = "arn:aws:sns:us-west-2:123456789012:viharbot-kepler-alerts"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                        serialization.NoEncryption()).decode()


def good_config(**over) -> StewardConfig:
    base = dict(repo="vchokshi/viharbot", region="us-west-2",
                reader_secret_id="viharbot/kepler/github-app-reader",
                state_table="viharbot-kepler-state", audit_table="viharbot-kepler-audit",
                alert_topic_arn=TOPIC)
    base.update(over)
    return StewardConfig(**base)


# ---- config ------------------------------------------------------------------------------------


def test_defaults_and_environment():
    cfg = from_env({"KEPLER_ALERT_TOPIC_ARN": TOPIC})
    assert cfg.repo == "vchokshi/viharbot" and cfg.repo_name == "viharbot"
    assert cfg.deadline_seconds == 240 and cfg.max_pulls == 25 and cfg.reminder_after_hours == 72
    tuned = from_env({"KEPLER_ALERT_TOPIC_ARN": TOPIC, "KEPLER_MAX_PULLS": "5"})
    assert tuned.max_pulls == 5


@pytest.mark.parametrize("over", [
    {"repo": "vchokshi/viharbot-onboarding-instructor"}, {"repo": "VChokshi/Viharbot-Onboarding-Instructor"},
    {"repo": "../x"}, {"repo": "noslash"}, {"region": "mars"},
    {"reader_" "secret_id": "other/name"}, {"reader_" "secret_id": "viharbot/kepler/Upper"},
    {"state_table": "other-table"}, {"audit_table": "viharbot-kepler-state"},
    {"alert_topic_arn": "arn:aws:sns:us-west-2:123456789012:other-topic"},
    {"alert_topic_arn": "https://evil.example/topic"}, {"base_branch": "main; rm -rf /"},
    {"deadline_seconds": 5}, {"deadline_seconds": 100_000}, {"max_pulls": 0}, {"max_pulls": 1000},
    {"reminder_after_hours": 0}, {"dedup_ttl_days": 0}, {"pending_retry_minutes": 0},
    {"max_alert_attempts": 0}, {"max_alert_attempts": 6},
])
def test_unsafe_configuration_is_refused(over):
    with pytest.raises(ConfigError):
        good_config(**over)


def test_environment_errors_are_clear():
    with pytest.raises(ConfigError, match="KEPLER_ALERT_TOPIC_ARN"):
        from_env({})
    with pytest.raises(ConfigError, match="whole number"):
        from_env({"KEPLER_ALERT_TOPIC_ARN": TOPIC, "KEPLER_MAX_PULLS": "many"})


# ---- GitHub authentication ---------------------------------------------------------------------


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Response(json.dumps(reply).encode())


def issued(token="ghs_ONE", expires="2026-11-05T05:00:00Z", permissions=None, repos=("viharbot",)):
    return {"token": token, "expires_at": expires,
            "permissions": permissions or dict(auth.EXPECTED_READER_PERMISSIONS),
            "repositories": [{"name": r} for r in repos]}


def tokens(replies, clock=None):
    secret = {"app_id": 4242, "installation_id": 777, "private_key_pem": PEM}
    opener = Opener(replies)
    return auth.InstallationTokens(lambda: secret, "vchokshi/viharbot",
                                   clock or (lambda: NOW), opener), opener


def _pad(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_jwt_is_rs256_short_lived_and_signed():
    header, claims, signature = auth.make_jwt(4242, PEM, NOW).split(".")
    assert json.loads(_pad(header)) == {"alg": "RS256", "typ": "JWT"}
    decoded = json.loads(_pad(claims))
    assert decoded["iss"] == "4242" and decoded["exp"] - decoded["iat"] == 600
    KEY.public_key().verify(_pad(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(),
                            hashes.SHA256())
    with pytest.raises(InvalidSignature):
        KEY.public_key().verify(_pad(signature), f"{header}.{claims}x".encode(), padding.PKCS1v15(),
                                hashes.SHA256())
    with pytest.raises(auth.AuthError):
        auth.make_jwt(1, "not a key", NOW)


def test_the_token_exchange_is_the_one_post_and_is_scoped_to_one_repository():
    source, opener = tokens([issued()])
    assert source.token() == "ghs_ONE"
    (request,) = opener.requests
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.github.com/app/installations/777/access_tokens"
    assert json.loads(request.data) == {"repositories": ["viharbot"]}
    jwt = request.get_header("Authorization").split()[1]
    header, claims, signature = jwt.split(".")
    KEY.public_key().verify(_pad(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(),
                            hashes.SHA256())


def test_the_token_is_cached_then_refreshed_before_it_expires():
    clock = Clock(NOW)
    source, opener = tokens([issued("ghs_ONE"), issued("ghs_TWO", "2026-11-05T07:00:00Z")], clock)
    assert source.token() == "ghs_ONE" and source.token() == "ghs_ONE"
    assert len(opener.requests) == 1
    clock.now = datetime.datetime(2026, 11, 5, 4, 56, tzinfo=datetime.timezone.utc)  # < 5 min left
    assert source.token() == "ghs_TWO" and len(opener.requests) == 2


@pytest.mark.parametrize("permissions", [
    {**auth.EXPECTED_READER_PERMISSIONS, "issues": "write"},
    {**auth.EXPECTED_READER_PERMISSIONS, "pull_requests": "write"},
    {**auth.EXPECTED_READER_PERMISSIONS, "administration": "read"},
    {"metadata": "read"}, {},
])
def test_a_token_that_is_not_exactly_the_readers_set_is_refused(permissions):
    source, _ = tokens([issued(permissions=permissions or {"x": "y"})])
    with pytest.raises(auth.UnsafeCredential):
        source.token()


def test_a_token_reaching_other_repositories_is_refused():
    source, _ = tokens([issued(repos=("viharbot", "private-thing"))])
    with pytest.raises(auth.UnsafeCredential):
        source.token()


def test_failures_are_fixed_wording_without_key_or_token():
    for reply in (urllib.error.HTTPError("u", 401, "bad", {}, io.BytesIO(b"SECRET-BODY ghs_X")),
                  urllib.error.URLError("net down ghs_LEAK")):
        source, _ = tokens([reply])
        with pytest.raises(auth.AuthError) as info:
            source.token()
        text = str(info.value)
        assert "ghs_" not in text and "SECRET" not in text and "BEGIN" not in text
    source, _ = tokens([issued(expires="not a time")])
    with pytest.raises(auth.AuthError):
        source.token()


def test_the_secret_loader_validates_and_never_echoes_content():
    good = {"app_id": 1, "installation_id": 2, "private_key_pem": PEM}
    assert auth.load_secret_from_aws(FakeSecrets(good), "viharbot/kepler/github-app-reader") == good
    for bad in ({"app_id": 1}, "not json", RuntimeError(f"denied {PEM}"), ["a list"]):
        with pytest.raises(auth.AuthError) as info:
            auth.load_secret_from_aws(FakeSecrets(bad), "viharbot/kepler/github-app-reader")
        assert "BEGIN" not in str(info.value)


# ---- deduplication state -------------------------------------------------------------------------


def state_pair(minutes=30):
    db, clock = FakeDynamo(), Clock(NOW)
    return ObservationState(db, "viharbot-kepler-state", clock, 30, minutes), db, clock


def test_an_observation_is_new_once_then_seen():
    state, db, _ = state_pair()
    assert state.peek("abc") == "unseen"
    assert state.claim("abc") == NEW and state.claim("abc") == SEEN
    assert state.peek("abc") == "pending"
    state.mark_sent("abc")
    assert state.peek("abc") == "sent" and state.claim("abc") == SEEN
    item = db.tables["viharbot-kepler-state"][("obs#abc", "v1")]
    assert int(item["expires_at"]["N"]) == int((NOW + datetime.timedelta(days=30)).timestamp())


def test_a_stale_pending_claim_is_retried_once_not_forever():
    state, _, clock = state_pair(minutes=30)
    assert state.claim("abc") == NEW
    clock.advance(minutes=10)
    assert state.claim("abc") == SEEN  # another poll is probably still working on it
    clock.advance(minutes=25)
    assert state.claim("abc") == RETRY  # the first poll died before confirming
    assert state.claim("abc") == SEEN  # and the retry cannot be taken twice
    state.mark_sent("abc")
    clock.advance(days=3)
    assert state.claim("abc") == SEEN  # a sent alert is never repeated


def test_a_stale_claim_is_taken_over_at_most_once_then_reported_exhausted():
    """Sage HIGH on #203: `attempts` was incremented but never checked, so repeated crashes after
    publishing could take the same claim over forever and duplicate the email without bound."""
    state, _, clock = state_pair(minutes=30)
    assert state.claim("x") == NEW
    clock.advance(minutes=31)
    assert state.claim("x") == RETRY  # the one allowed retry
    clock.advance(minutes=31)
    assert state.claim("x") == "exhausted"  # not another retry: reported, once, for a human
    assert state.peek("x") == "exhausted"
    clock.advance(minutes=31)
    assert state.claim("x") == SEEN  # and reported only once
    clock.advance(days=3)
    assert state.claim("x") == SEEN


def test_two_racing_claims_cannot_both_win():
    state, db, clock = state_pair()
    other = ObservationState(db, "viharbot-kepler-state", clock, 30, 30)
    assert [state.claim("x"), other.claim("x")].count(NEW) == 1


def test_a_damaged_record_is_left_for_a_human_not_resent():
    state, db, clock = state_pair()
    state.claim("abc")
    db.tables["viharbot-kepler-state"][("obs#abc", "v1")]["claimed_at"] = {"S": "garbage"}
    clock.advance(days=2)
    assert state.claim("abc") == SEEN


def test_a_state_outage_raises_and_leaks_nothing():
    state, db, _ = state_pair()
    db.fail = {"put"}
    with pytest.raises(StateUnavailable):
        state.claim("abc")
    db.fail = {"get"}
    with pytest.raises(StateUnavailable):
        state.peek("abc")
    db.fail = set()
    state.claim("abc")
    db.fail = {"update"}
    with pytest.raises(StateUnavailable):
        state.mark_sent("abc")


# ---- the audit chain -----------------------------------------------------------------------------


def audit_pair(**db_kwargs):
    db, clock = FakeDynamo(**db_kwargs), Clock(NOW)
    return AuditLog(db, "viharbot-kepler-audit", clock), db


def test_events_form_an_intact_chain_and_tampering_is_detected():
    log, _ = audit_pair()
    for event in ("poll_started", "alert_sent", "poll_completed"):
        log.append(event, pr=179)
    items = log.read_all()
    assert [i["seq"] for i in items] == [0, 1, 2]
    verify_chain(items)
    edited = json.loads(json.dumps(items))
    edited[1]["body"] = edited[1]["body"].replace("alert_sent", "nothing_happened")
    for broken in (edited, items[1:], [items[0], items[2]], [items[1], items[0], items[2]]):
        with pytest.raises(AuditIntegrityError):
            verify_chain(broken)


def test_a_lost_write_race_is_retried_and_the_chain_stays_intact():
    log, db = audit_pair()
    log.append("poll_started")

    def compete(table, key):
        body = build_body({"sha256": json.loads(json.dumps(next(iter(table.values()))["sha256"]["S"]))},
                          1, "2026-11-05T04:00:00Z", "someone_else", "ok", {})
        import hashlib
        table[key] = {"pk": {"S": "chain"}, "sk": {"S": key[1]}, "body": {"S": body},
                      "sha256": {"S": hashlib.sha256(body.encode()).hexdigest()}}

    db.race_hook, db.races_left = compete, 1
    log.append("alert_sent")
    items = log.read_all()
    assert [json.loads(i["body"])["event"] for i in items] == [
        "poll_started", "someone_else", "alert_sent"]
    verify_chain(items)


def test_a_persistent_race_or_an_outage_raises_unavailable():
    log, db = audit_pair()
    db.race_hook, db.races_left = (lambda table, key: None), 10
    with pytest.raises(AuditUnavailable):
        log.append("poll_started")
    log2, db2 = audit_pair()
    db2.fail = {"put"}
    with pytest.raises(AuditUnavailable):
        log2.append("poll_started")
    db3 = FakeDynamo()
    db3.fail = {"query"}
    with pytest.raises(AuditUnavailable):
        AuditLog(db3, "viharbot-kepler-audit", Clock(NOW)).append("poll_started")


@pytest.mark.parametrize("fields", [
    {"password": "x"}, {"error_code": "line one\nline two"}, {"status": "a" * 200},
    {"pr": "SELECT * FROM <script>"}, {"request_id": "ghs_token with spaces & symbols!"},
])
def test_unknown_fields_and_free_text_cannot_be_recorded(fields):
    log, db = audit_pair()
    with pytest.raises(ValueError):
        log.append("poll_started", **fields)
    assert not db.tables.get("viharbot-kepler-audit")


def test_the_audit_vocabulary_is_closed():
    assert "comment" not in FIELDS and "body" not in FIELDS and "title" not in FIELDS
    assert "model_alias" in FIELDS  # the logging contract in AGENTS.md


def test_reading_a_long_chain_pages_through_it():
    log, _ = audit_pair(page=2)
    for n in range(5):
        log.append("tick", pr=n)
    items = log.read_all()
    assert len(items) == 5
    verify_chain(items)


# ---- alerts ----------------------------------------------------------------------------------------


def assessment(login="MohamadKanso") -> PullAssessment:
    reqs = (Requirement("cla_recorded", "CLA acceptance recorded", OUTSTANDING, "No acceptance is recorded."),
            Requirement("ci", "Automated checks", "done", "All automated checks passed."))
    return PullAssessment("vchokshi/viharbot", 179, login, 91088196, "a" * 40, True, True, reqs,
                          ("CLA acceptance recorded: No acceptance is recorded.",),
                          ("Ask the author for the CLA acceptance.",),
                          (Observation("id1", "cla_recorded", OUTSTANDING,
                                       "PR #179: CLA acceptance recorded is outstanding."),),
                          "f" * 64)


def test_the_email_names_the_pr_what_is_new_and_marks_a_draft_as_not_posted():
    a = assessment()
    subject, body = format_alert(a, list(a.observations), DraftDecision("draft_ready", "welcome", "Hi there", ("cla",)))
    assert subject == "Kepler: PR #179 needs attention (1 new)" and "\n" not in subject
    assert "https://github.com/vchokshi/viharbot/pull/179" in body and "@MohamadKanso" in body
    assert "PR #179: CLA acceptance recorded is outstanding." in body
    assert "[done] Automated checks" in body and "[outstanding] CLA acceptance recorded" in body
    assert "DRAFT message (NOT posted" in body and "Hi there" in body
    assert body.rstrip().endswith("Nothing has been posted to GitHub.")


def test_the_email_explains_a_missing_draft_and_distrusts_odd_logins():
    a = assessment(login="evil\n@everyone <script>")
    _, body = format_alert(a, list(a.observations), DraftDecision("already_requested_waiting"))
    assert "(unusual login)" in body and "<script>" not in body and "@everyone" not in body
    assert "No draft: the CLA was already requested recently" in body


def test_publishing_validates_the_subject_and_hides_failures():
    sns = FakeSns()
    publisher = SnsPublisher(sns, TOPIC)
    assert publisher.publish("Kepler: PR #1 needs attention (1 new)", "body") == "m-1"
    assert sns.messages[0]["TopicArn"] == TOPIC
    for bad in ("two\nlines", "x" * 101):
        with pytest.raises(ValueError):
            publisher.publish(bad, "body")
    sns.fail = True
    with pytest.raises(AlertUnavailable):
        publisher.publish("Kepler: ok", "body")
