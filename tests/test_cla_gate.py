"""CLA gate: record and exceptions parsing, the decision, evidence, GitHub handling and activation.

Everything runs offline. GitHub is replaced by stubs and no network, secret or model is used.
"""

from __future__ import annotations

import argparse
import datetime
import email.message
import importlib.util
import json
import re
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / ".github" / "scripts"
SCRIPT = SCRIPTS / "cla_gate.py"
WORKFLOW = ROOT / ".github" / "workflows" / "cla-gate.yml"
RULESET = ROOT / ".github" / "rulesets" / "protect-main.json"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason=".github is not in this environment")

sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("cla_gate", SCRIPT)
cla = importlib.util.module_from_spec(spec)
sys.modules["cla_gate"] = cla  # dataclasses resolve annotations through it
spec.loader.exec_module(cla)

TODAY = datetime.date(2026, 11, 10)
FOUNDER = (1, "founder")
ALICE = (2002, "alice")
BOB = (3003, "bob")
DEPENDABOT = (49699333, "dependabot[bot]")
WEB_FLOW = (19864447, "web-flow")
REPO = "vchokshi/viharbot"
URL = f"https://github.com/{REPO}/pull/7#issuecomment-99"

REGISTRY = (
    "## Registry\n\n| GitHub username | GitHub numeric account ID | Role | Approved on (UTC) |\n"
    "| --- | ---: | --- | --- |\n| [@founder](https://github.com/founder) | 1 | Founder | N/A |\n"
    "| [@carol](https://github.com/carol) | 4004 | Trusted contributor | 2026-10-30 |\n"
)
AGREEMENT = "# Agreement\n\n**Legal-terms version: 1.0**\n**Editorial revision: 1**\n\nThe terms.\n"


def row(account=ALICE, version="1.0", status="active", withdrawn="-", eligibility="adult-individual",
        username=None, effective="2026-11-05T10:00:00Z", ev_id=None, ev_sha="a" * 64) -> str:
    login = username or account[1]
    return (f"| {account[0]} | [@{login}](https://github.com/{login}) | {version} | {effective} | {status} "
            f"| {withdrawn} | {eligibility} | {ev_id or f'EV-{account[0]}-99'} | {ev_sha} |")


def record(*rows, status="draft", legal="1.0", editorial=1, sha="unfrozen", review="pending",
           approval="pending", storage="not-verified", ruleset="not-verified") -> str:
    return "\n".join([
        "# Contribution Agreement Acceptances", "", "## Status", "",
        f"* CLA status: {status}", f"* CLA legal-terms version: {legal}", f"* CLA editorial revision: {editorial}",
        f"* CLA text SHA-256: {sha}", f"* Legal review: {review}", f"* Founder approval: {approval}",
        f"* Private evidence storage: {storage}", f"* Required ruleset: {ruleset}", "", "## Acceptances", "",
        "| " + " | ".join(cla.RECORD_HEADER) + " |", "| " + " | ".join("---" for _ in cla.RECORD_HEADER) + " |",
        *rows, "", "## Rules", "", "| not | a | row |",
    ]) + "\n"


def in_force(*rows, legal="1.0", editorial=1, agreement=AGREEMENT) -> str:
    return record(*rows, status="in-force", legal=legal, editorial=editorial, sha=cla.sha256_text(agreement),
                  review="complete: counsel memo", approval="approved: founder",
                  storage="verified: restore test", ruleset="verified: activation-check")


def exc_row(ex_id="EX-001", account=DEPENDABOT, kind="automation", scope="commits", authorizer="founder",
            reason="Dependency update bot reviewed by the founder", approved="2026-11-04", expires="none",
            ref="https://github.com/vchokshi/viharbot/pull/181") -> str:
    return (f"| {ex_id} | {account[0]} | {account[1]} | {kind} | {scope} | {reason} | {authorizer} "
            f"| {approved} | {expires} | {ref} |")


def exceptions(*rows) -> str:
    return "\n".join([
        "# Exceptions", "", "## Exceptions", "", "| " + " | ".join(cla.EXCEPTION_HEADER) + " |",
        "| " + " | ".join("---" for _ in cla.EXCEPTION_HEADER) + " |", *rows, "", "## Rules", "",
    ]) + "\n"


EXC = exceptions(exc_row(), exc_row("EX-002", WEB_FLOW, reason="GitHub web committer identity, adds no authorship"))
IN_FORCE = in_force(row())


def user(account, kind="User"):
    return {"id": account[0], "login": account[1], "type": kind}


SAME = object()
LOGINS = {"alice": 2002, "bob": 3003, "founder": 1, "carol": 4004}


def resolver(login):
    """What GitHub would answer for a login lookup."""
    return LOGINS.get(login)


def commit(author=ALICE, message="Change", committer=SAME, sha="a1b2c3d4e5f6", email=None):
    who = author if committer is SAME else committer
    return {"sha": sha, "message": message, "author_email": email,
            "author": user(author) if author else None, "committer": user(who) if who else None}


def make_pr(author=ALICE, commits=None, files=(), number=7, unsupported=()):
    kind = "Bot" if author[1].endswith("[bot]") else "User"
    return {"number": number, "author": user(author, kind), "files": list(files), "unsupported": list(unsupported),
            "commits": [commit(author)] if commits is None else commits}


def judge(rec=IN_FORCE, author=ALICE, commits=None, files=(), exc=EXC, agreement=AGREEMENT, head=None,
          resolve=lambda login: None, number=7, registry_text=REGISTRY, unsupported=()):
    return cla.evaluate(cla.Files(rec, exc, registry_text, agreement, head or {}),
                        make_pr(author, commits, files, number, unsupported), resolve, TODAY)


def errors_text(result) -> str:
    return "\n".join(result.errors)


# ---- the record -------------------------------------------------------------------------------
def test_valid_record_parses():
    terms, rows, errors = cla.parse_record(IN_FORCE)
    assert errors == [] and terms.in_force and terms.frozen and terms.gaps() == []
    assert [(r.account_id, r.username, r.version, r.status) for r in rows] == [(2002, "alice", "1.0", "active")]


@pytest.mark.parametrize("bad", [
    row(ev_id="EV-xyz"), row(ev_sha="short"), row(eligibility="whatever"), row(status="gone"),
    row(effective="2026-11-05"), row(effective="2026-13-45T00:00:00Z"), row(version="v1"),
    row(version="9.0"), row(status="withdrawn"), row(status="active", withdrawn="2026-11-08T09:00:00Z"),
    row(account=(2002, "alice"), username="bad_name"),
    "| 02002 | [@alice](https://github.com/alice) | 1.0 | 2026-11-05T10:00:00Z | active | - | adult-individual | EV-1-1 | " + "a" * 64 + " |",
    "| 2002 | [@alice](https://github.com/mallory) | 1.0 | 2026-11-05T10:00:00Z | active | - | adult-individual | EV-1-1 | " + "a" * 64 + " |",
    "| 2002 | [@alice](https://github.com/alice) | 1.0 |",
])
def test_malformed_rows_are_reported(bad):
    assert cla.parse_record(in_force(bad))[2]


def test_duplicate_account_and_version_is_reported_but_other_versions_are_fine():
    assert cla.parse_record(in_force(row(), row(ev_id="EV-2002-100")))[2]
    assert cla.parse_record(in_force(row(version="1.0"), row(version="0.9", ev_id="EV-2002-100"), legal="1.0"))[2] == []


def test_status_lines_must_each_appear_exactly_once_and_be_valid():
    for label in cla.STATUS_FIELDS:
        text = IN_FORCE.replace(f"* {label}:", f"* x{label}:")
        assert any(label in e for e in cla.parse_record(text)[2])
    assert cla.parse_record(IN_FORCE.replace("in-force", "maybe"))[0] is None
    assert cla.parse_record(IN_FORCE + "* CLA status: draft\n")[2]


def test_header_must_match():
    assert cla.parse_record(IN_FORCE.replace("Evidence SHA-256", "Evidence"))[2]


# ---- draft to active --------------------------------------------------------------------------
def test_in_force_is_invalid_while_any_prerequisite_is_open():
    for kwargs in ({"sha": "unfrozen"}, {"review": "pending"}, {"approval": "pending"},
                   {"storage": "not-verified"}, {"ruleset": "not-verified"}):
        base = dict(status="in-force", sha=cla.sha256_text(AGREEMENT), review="complete: x", approval="approved: x",
                    storage="verified: x", ruleset="verified: x")
        base.update(kwargs)
        terms, _, errors = cla.parse_record(record(**base))
        assert terms is not None and terms.gaps() and any("in-force" in e for e in errors)


def test_draft_with_open_prerequisites_is_valid_and_lists_the_gaps():
    terms, _, errors = cla.parse_record(record())
    assert errors == [] and not terms.in_force and len(terms.gaps()) == 5


def test_founder_can_flip_to_in_force_only_with_every_prerequisite_recorded():
    base = record()
    ok = judge(rec=base, author=FOUNDER, commits=[commit(FOUNDER)], files=[cla.RECORD_PATH],
               head={cla.RECORD_PATH: in_force()})
    assert ok.ok, ok.errors
    early = record(status="in-force", sha=cla.sha256_text(AGREEMENT))
    bad = judge(rec=base, author=FOUNDER, commits=[commit(FOUNDER)], files=[cla.RECORD_PATH],
                head={cla.RECORD_PATH: early})
    assert not bad.ok and "in-force" in errors_text(bad)


def test_a_non_founder_cannot_activate_and_a_stale_hash_blocks_activation():
    result = judge(rec=record(), author=ALICE, files=[cla.RECORD_PATH], head={cla.RECORD_PATH: in_force()})
    assert not result.ok and "protected files" in errors_text(result)
    stale = record(status="in-force", sha="0" * 64, review="complete: x", approval="approved: x",
                   storage="verified: x", ruleset="verified: x")
    result = judge(rec=record(), author=FOUNDER, commits=[commit(FOUNDER)], files=[cla.RECORD_PATH],
                   head={cla.RECORD_PATH: stale})
    assert not result.ok and "frozen" in errors_text(result)


def test_base_in_force_with_a_mismatched_agreement_fails_closed():
    result = judge(agreement=AGREEMENT + "tampered\n")
    assert not result.ok and "does not match the frozen text" in errors_text(result)
    assert not judge(agreement=AGREEMENT.replace("1.0", "1.1")).ok


# ---- the decision -----------------------------------------------------------------------------
def test_valid_individual_acceptance_passes():
    result = judge()
    assert result.ok and any("accepted version 1.0" in c for c in result.covered)


def test_missing_acceptance_fails_with_instructions():
    result = judge(author=BOB)
    text = errors_text(result)
    assert not result.ok and "no recorded acceptance of legal-terms version 1.0" in text
    assert "request, not a record" in text


def test_acceptance_of_another_version_fails_and_says_why():
    result = judge(rec=in_force(row(version="0.9"), legal="1.0"))
    assert not result.ok and "renewed acceptance" in errors_text(result)


def test_username_change_keeps_the_stable_numeric_id():
    renamed = in_force(row(username="old-alice"))
    result = judge(rec=renamed)
    assert result.ok and "username changed since acceptance" in result.covered[0]
    assert not judge(rec=IN_FORCE, author=(9999, "alice")).ok  # same name, different account


def test_draft_fails_every_non_exempt_contributor_even_with_a_row():
    result = judge(rec=record(row()))
    assert not result.ok and "none is in force" in errors_text(result)


def test_multiple_commit_authors_must_each_be_covered():
    result = judge(commits=[commit(ALICE), commit(BOB)])
    assert not result.ok and "`bob`" in errors_text(result)
    both = in_force(row(), row(BOB, ev_id="EV-3003-98"))
    assert judge(rec=both, commits=[commit(ALICE), commit(BOB)]).ok
    assert judge(commits=[commit(ALICE), commit(ALICE)]).ok


def test_committers_are_checked_and_the_web_committer_is_exempt_by_exception():
    assert judge(commits=[commit(ALICE, committer=WEB_FLOW)]).ok
    result = judge(commits=[commit(ALICE, committer=BOB)])
    assert not result.ok and "committer" in errors_text(result)
    no_exceptions = exceptions()
    assert not judge(commits=[commit(ALICE, committer=WEB_FLOW)], exc=no_exceptions).ok


def test_an_unlinked_committer_fails_even_when_the_author_is_accepted():
    # Sage finding: a null committer used to be skipped, so this passed.
    result = judge(commits=[commit(ALICE, committer=None)])
    assert not result.ok and "unlinked committer" in errors_text(result)
    assert judge(commits=[commit(ALICE, committer=ALICE)]).ok
    founder_pr = judge(author=FOUNDER, commits=[commit(FOUNDER, committer=None)])
    assert not founder_pr.ok and "unlinked committer" in errors_text(founder_pr)


def test_co_authored_by_trailers_are_checked():
    trailer = "Fix\n\nCo-authored-by: Bob <3003+bob@users.noreply.github.com>"
    result = judge(commits=[commit(ALICE, message=trailer)], resolve=resolver)
    assert not result.ok and "`bob`" in errors_text(result) and "co-author" in errors_text(result)
    accepted = in_force(row(), row(BOB, ev_id="EV-3003-98"))
    assert judge(rec=accepted, commits=[commit(ALICE, message=trailer)], resolve=resolver).ok


def test_legacy_noreply_trailers_are_resolved_by_login_and_unresolvable_ones_fail():
    legacy = "Fix\n\nCo-authored-by: Bob <bob@users.noreply.github.com>"
    result = judge(commits=[commit(ALICE, message=legacy)], resolve=lambda login: 3003 if login == "bob" else None)
    assert not result.ok and "`bob`" in errors_text(result)
    result = judge(commits=[commit(ALICE, message=legacy)], resolve=lambda login: None)
    assert not result.ok and "cannot be matched" in errors_text(result)
    other = "Fix\n\nCo-authored-by: X <x@example.com>"
    assert not judge(commits=[commit(ALICE, message=other)]).ok
    founder_trailer = "Fix\n\nCo-authored-by: F <1+founder@users.noreply.github.com>"
    assert judge(commits=[commit(ALICE, message=founder_trailer)], resolve=resolver).ok


def test_a_forged_noreply_trailer_is_not_believed():
    # An ID written into a trailer is a claim. GitHub must confirm the login owns that ID.
    wrong_id = "Fix\n\nCo-authored-by: X <1+bob@users.noreply.github.com>"  # bob is 3003, not 1
    result = judge(commits=[commit(ALICE, message=wrong_id)], resolve=resolver)
    assert not result.ok and "cannot be matched" in errors_text(result)
    ghost = "Fix\n\nCo-authored-by: X <77+ghost@users.noreply.github.com>"
    assert not judge(commits=[commit(ALICE, message=ghost)], resolve=resolver).ok
    assert not judge(commits=[commit(ALICE, message=wrong_id)], resolve=lambda login: None).ok


def test_a_bot_trailer_is_confirmed_by_lookup_and_needs_an_exception():
    trailer = "Fix\n\nCo-authored-by: d <49699333+dependabot[bot]@users.noreply.github.com>"
    lookup = {"dependabot[bot]": 49699333}.get
    assert judge(commits=[commit(ALICE, message=trailer)], resolve=lookup).ok  # EX-001 covers it
    assert not judge(commits=[commit(ALICE, message=trailer)], resolve=lookup, exc=exceptions()).ok


def test_unresolvable_identities_fail_closed():
    unlinked = commit(None, sha="abc1234def00")
    result = judge(commits=[commit(ALICE), unlinked])
    assert not result.ok and "cannot be matched" in errors_text(result)
    noreply = commit(None, email="3003+bob@users.noreply.github.com")  # an email is a claim, not a link
    result = judge(commits=[noreply])
    assert not result.ok and "cannot be matched" in errors_text(result) and "`bob`" not in errors_text(result)


def test_founder_pull_request_does_not_excuse_third_party_commits():
    result = judge(author=FOUNDER, commits=[commit(FOUNDER), commit(BOB)])
    assert not result.ok and "`bob`" in errors_text(result)
    assert any("founder-owned" in e for e in result.exemptions)
    accepted = in_force(row(BOB, ev_id="EV-3003-98"))
    assert judge(rec=accepted, author=FOUNDER, commits=[commit(FOUNDER), commit(BOB)]).ok
    assert judge(author=FOUNDER, commits=[commit(FOUNDER)]).ok


def test_trusted_contributors_are_not_exempt_and_the_registry_must_be_valid():
    assert not judge(author=(4004, "carol")).ok
    assert not judge(registry_text=None, author=FOUNDER, commits=[commit(FOUNDER)]).ok
    assert not judge(registry_text="garbage", author=FOUNDER, commits=[commit(FOUNDER)]).ok


def test_unauthorized_corporate_contribution_is_held():
    held = in_force(row(eligibility="hold-corporate"))
    result = judge(rec=held)
    assert not result.ok and "corporate CLA" in errors_text(result)
    for fine in ("employee-authorized", "corporate-cla"):
        assert judge(rec=in_force(row(eligibility=fine))).ok


def test_minor_requires_separate_approval():
    result = judge(rec=in_force(row(eligibility="hold-minor")))
    assert not result.ok and "minor" in errors_text(result)
    assert judge(rec=in_force(row(eligibility="minor-approved"))).ok


def test_prospective_withdrawal_blocks_new_contributions_and_keeps_history():
    withdrawn = in_force(row(status="withdrawn", withdrawn="2026-11-08T09:00:00Z"))
    result = judge(rec=withdrawn)
    assert not result.ok and "withdrawn from future participation" in errors_text(result)
    # History is preserved: the row stays, and only withdrawal may be added to it.
    _, base_rows, _ = cla.parse_record(IN_FORCE)
    _, new_rows, _ = cla.parse_record(withdrawn)
    assert cla.record_change_errors(base_rows, new_rows) == []
    assert cla.record_change_errors(new_rows, base_rows)  # cannot be reversed
    assert cla.record_change_errors(base_rows, []) and "never deleted" in cla.record_change_errors(base_rows, [])[0]


def test_a_withdrawn_contributor_needs_a_pull_request_specific_exception():
    withdrawn = in_force(row(status="withdrawn", withdrawn="2026-11-08T09:00:00Z"))
    case = exc_row("EX-010", ALICE, "approved-case", "pr:7", reason="Founder admits PR 7, opened before withdrawal",
                   expires="2026-12-01", ref="https://github.com/vchokshi/viharbot/issues/200")
    exc = exceptions(exc_row(), case)
    ok = judge(rec=withdrawn, exc=exc, number=7)
    assert ok.ok and any("EX-010" in e and "authorized by founder" in e for e in ok.exemptions)
    assert not judge(rec=withdrawn, exc=exc, number=8).ok  # a different pull request
    assert not cla.evaluate(cla.Files(withdrawn, exc, REGISTRY, AGREEMENT), make_pr(), lambda x: None,
                            datetime.date(2026, 12, 2)).ok  # expired


def test_deleting_or_rewriting_a_row_is_refused_even_for_the_founder():
    gone = in_force()
    result = judge(author=FOUNDER, commits=[commit(FOUNDER)], files=[cla.RECORD_PATH], head={cla.RECORD_PATH: gone})
    assert not result.ok and "never deleted" in errors_text(result)
    edited = in_force(row(ev_sha="b" * 64))
    result = judge(author=FOUNDER, commits=[commit(FOUNDER)], files=[cla.RECORD_PATH], head={cla.RECORD_PATH: edited})
    assert not result.ok and "must not be edited" in errors_text(result)
    added = in_force(row(), row(BOB, ev_id="EV-3003-98"))
    assert judge(author=FOUNDER, commits=[commit(FOUNDER)], files=[cla.RECORD_PATH], head={cla.RECORD_PATH: added}).ok


# ---- versions: material versus editorial ------------------------------------------------------
def revised(legal="1.0", editorial=1, text="The terms.\n"):
    return f"# Agreement\n\n**Legal-terms version: {legal}**\n**Editorial revision: {editorial}**\n\n{text}"


def propose(agreement, rec, author=FOUNDER, **kwargs):
    return judge(author=author, commits=[commit(author)], files=[cla.AGREEMENT_PATH, cla.RECORD_PATH],
                 head={cla.AGREEMENT_PATH: agreement, cla.RECORD_PATH: rec}, **kwargs)


def test_editorial_revision_keeps_existing_acceptances():
    a2 = revised(editorial=2, text="The terms, typo fixed.\n")
    assert propose(a2, in_force(row(), editorial=2, agreement=a2)).ok
    base = in_force(row(), editorial=2, agreement=a2)
    assert judge(rec=base, agreement=a2).ok  # alice's 1.0 row still covers revision 2


def test_material_change_requires_renewed_acceptance():
    a2 = revised(legal="2.0", text="Different rights.\n")
    assert propose(a2, in_force(row(), legal="2.0", agreement=a2)).ok
    base = in_force(row(), legal="2.0", agreement=a2)
    result = judge(rec=base, agreement=a2)
    assert not result.ok and "renewed acceptance" in errors_text(result)
    assert judge(rec=in_force(row(), row(version="2.0", ev_id="EV-2002-100"), legal="2.0", agreement=a2),
                 agreement=a2).ok


def test_changing_frozen_text_without_a_new_version_is_refused():
    silent = revised(text="Quietly different.\n")
    result = propose(silent, in_force(row(), agreement=silent))
    assert not result.ok and "without a new version" in errors_text(result)


def test_record_and_agreement_must_agree_and_the_hash_must_match():
    a2 = revised(editorial=2, text="Edited.\n")
    stale = in_force(row(), editorial=2, agreement=AGREEMENT)  # hash of the old text
    result = propose(a2, stale)
    assert not result.ok and "frozen" in errors_text(result)
    mismatch = in_force(row(), editorial=3, agreement=a2)
    assert "They must match" in errors_text(propose(a2, mismatch))
    down = in_force(row(), legal="0.9", agreement=revised(legal="0.9"))
    assert not propose(revised(legal="0.9"), down).ok


def test_only_the_founder_changes_protected_files():
    protected = [cla.RECORD_PATH, cla.EXCEPTIONS_PATH, cla.AGREEMENT_PATH, ".github/workflows/cla-gate.yml",
                 ".github/scripts/cla_gate.py", ".github/rulesets/protect-main.json", ".github/CODEOWNERS",
                 "docs/governance/cla-acceptance.md", "tests/test_cla_gate.py"]
    for path in protected:
        result = judge(files=[path])
        assert not result.ok and "protected files" in errors_text(result), path
    assert judge(files=["README.md"]).ok
    bot = judge(author=DEPENDABOT, files=[".github/workflows/cla-gate.yml"])
    assert not bot.ok  # an exemption from the agreement is not permission to edit enforcement


def test_a_pull_request_cannot_approve_its_own_exception_or_acceptance():
    # The decision reads the base branch's record and exceptions; the pull request's copies only
    # ever go through validation. Bob adding himself to both accepts nothing.
    self_accept = in_force(row(), row(BOB, ev_id="EV-3003-98"))
    self_exc = exceptions(exc_row(), exc_row("EX-050", BOB, "approved-case", "pr:7", reason="Self approved nonsense",
                                             expires="2026-12-01", ref="https://github.com/o/r/issues/1"))
    result = judge(author=BOB, files=[cla.RECORD_PATH, cla.EXCEPTIONS_PATH],
                   head={cla.RECORD_PATH: self_accept, cla.EXCEPTIONS_PATH: self_exc})
    text = errors_text(result)
    assert not result.ok and "protected files" in text and "no recorded acceptance" in text


# ---- exceptions -------------------------------------------------------------------------------
def test_shipped_style_exceptions_parse():
    rules, errors = cla.parse_exceptions(EXC)
    assert errors == [] and [r.exception_id for r in rules] == ["EX-001", "EX-002"]


@pytest.mark.parametrize("bad", [
    exc_row(kind="forever"), exc_row(scope="pr:7"), exc_row("EX-1"), exc_row(reason="short"),
    exc_row(approved="2026-99-99"), exc_row(expires="someday"),
    exc_row(kind="approved-case", scope="commits", expires="2026-12-01"),
    exc_row(kind="approved-case", scope="pr:7", expires="none"),
    exc_row(kind="approved-case", scope="pr:7", expires="2026-12-01", ref="n/a"),
])
def test_bad_exceptions_are_rejected(bad):
    assert cla.parse_exceptions(exceptions(bad))[1]


def test_bots_need_an_exception_and_it_must_be_authorized_by_the_founder():
    assert judge(author=DEPENDABOT, commits=[commit(DEPENDABOT)]).ok
    result = judge(author=DEPENDABOT, commits=[commit(DEPENDABOT)], exc=exceptions())
    assert not result.ok and "not an individual user account" in errors_text(result)
    impostor = exceptions(exc_row(authorizer="mallory"))
    result = judge(author=DEPENDABOT, commits=[commit(DEPENDABOT)], exc=impostor)
    assert not result.ok and "not authorized by the founder" in errors_text(result)


AI_TRAILER = "Fix\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"


def attribution_row(scope="email:noreply@anthropic.com", expires="none", authorizer="founder"):
    return exc_row("EX-003", (0, "-"), "attribution", scope, authorizer=authorizer,
                   reason="Tool attribution trailer, not a person who can accept", expires=expires)


def test_attribution_exception_excuses_only_the_named_co_author_trailer():
    exc = exceptions(exc_row(), attribution_row())
    founder_commit = commit(FOUNDER, message=AI_TRAILER)
    assert judge(author=FOUNDER, commits=[founder_commit], exc=exc).ok
    logged = judge(author=FOUNDER, commits=[founder_commit], exc=exc).exemptions
    assert any("EX-003" in line and "attribution" in line for line in logged)
    assert not judge(author=FOUNDER, commits=[founder_commit]).ok  # without the exception the trailer fails
    other = commit(FOUNDER, message="Fix\n\nCo-authored-by: X <someone@anthropic.com>")
    assert not judge(author=FOUNDER, commits=[other], exc=exc).ok  # a different address is not covered
    shouting = commit(FOUNDER, message="Fix\n\nCO-AUTHORED-BY: Claude <NOREPLY@ANTHROPIC.COM>")
    assert judge(author=FOUNDER, commits=[shouting], exc=exc).ok  # the address is matched case-insensitively


def test_attribution_exception_cannot_excuse_an_unlinked_commit_author_or_committer():
    exc = exceptions(exc_row(), attribution_row())
    unlinked_author = commit(None, message=AI_TRAILER)
    result = judge(author=FOUNDER, commits=[unlinked_author], exc=exc)
    assert not result.ok and "unlinked commit author" in errors_text(result)


def test_attribution_exception_must_be_unexpired_and_founder_authorized():
    for bad in (attribution_row(expires="2026-11-09"), attribution_row(authorizer="mallory")):
        result = judge(author=FOUNDER, commits=[commit(FOUNDER, message=AI_TRAILER)], exc=exceptions(exc_row(), bad))
        assert not result.ok, bad


@pytest.mark.parametrize("bad", [
    exc_row("EX-003", (5, "-"), "attribution", "email:a@b.example", reason="Tool attribution, not a person"),
    exc_row("EX-003", (0, "x"), "attribution", "email:a@b.example", reason="Tool attribution, not a person"),
    exc_row("EX-003", (0, "-"), "attribution", "commits", reason="Tool attribution, not a person"),
    exc_row("EX-003", (0, "-"), "attribution", "email:not-an-address", reason="Tool attribution, not a person"),
    exc_row("EX-003", (0, "-"), "attribution", "email:a@b.example", reason="Tool attribution, not a person",
            expires="someday"),
])
def test_malformed_attribution_exceptions_are_rejected(bad):
    assert cla.parse_exceptions(exceptions(bad))[1]


def test_unlinked_commit_email_fails_even_for_a_founder_authored_pull_request():
    # The machine-default email is not linked to a GitHub account, so authorship cannot be checked.
    result = judge(author=FOUNDER, commits=[commit(None, email="vihar@host.local")])
    assert not result.ok and "unlinked commit author" in errors_text(result)
    forged = commit(None, email="1+founder@users.noreply.github.com", committer=ALICE)
    result = judge(author=ALICE, commits=[forged])
    assert not result.ok and "unlinked commit author" in errors_text(result)  # no founder exemption from an email
    assert not any("founder-owned" in line for line in result.exemptions)


def test_forged_identities_cannot_borrow_the_founder_or_an_accepted_user():
    # Sage finding: raw noreply addresses were trusted. Cover the founder and an accepted contributor.
    for who in ("1+founder", "2002+alice", "3003+bob"):
        forged = commit(None, email=f"{who}@users.noreply.github.com", committer=ALICE)
        result = judge(author=ALICE, commits=[forged])
        assert not result.ok, who
        assert not any("founder-owned" in line for line in result.exemptions), who


def test_exemptions_are_logged_with_reason_authorizer_and_scope():
    result = judge(author=DEPENDABOT, commits=[commit(DEPENDABOT)])
    report = cla.render(result)
    assert "Exemptions applied (audit log)" in report and "EX-001" in report
    assert "authorized by founder" in report and "scope commits" in report and "reason:" in report


# ---- evidence, edited and deleted comments ----------------------------------------------------
def comment(body=None, account=ALICE, created="2026-11-05T10:00:00Z", updated=None, url=URL, kind="User"):
    return {"id": 99, "body": body if body is not None else cla.statement("1.0"), "user": user(account, kind),
            "created_at": created, "updated_at": updated or created, "html_url": url,
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/7"}


class FakeGH:
    """Canned GitHub answers. A value that is an exception is raised."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, path):
        self.calls.append(path)
        value = self.routes[path]
        if isinstance(value, Exception):
            raise value
        return value, {}

    def paginate(self, path, max_pages):
        return self.get(path)[0]


COMMENT_PATH = f"/repos/{REPO}/issues/comments/99"


def verify(tmp_path, gh, capsys, record_text=IN_FORCE, eligibility="adult-individual", url=URL, out=None):
    rec = tmp_path / "rec.md"
    rec.write_text(record_text, encoding="utf-8")
    args = argparse.Namespace(repo=REPO, comment_url=url, eligibility=eligibility, verified_by="founder",
                              evidence_out=str(out or tmp_path / "private"), registry_pr=181,
                              acceptances_file=str(rec))
    code = cla.run_verify_comment(args, gh=gh, now="2026-11-06T08:00:00Z")
    return code, capsys.readouterr().out


def test_valid_comment_yields_private_evidence_and_a_minimal_public_row(tmp_path, capsys):
    code, out = verify(tmp_path, FakeGH({COMMENT_PATH: comment()}), capsys)
    assert code == 0
    line = next(ln for ln in out.splitlines() if ln.startswith("| 2002"))
    evidence = json.loads((tmp_path / "private" / "EV-2002-99.json").read_text())
    assert evidence["evidence"]["comment"]["body"] == cla.statement("1.0")
    assert evidence["evidence"]["verification"]["verified_by"] == "founder"
    assert evidence["evidence_sha256"] in line and "EV-2002-99" in line
    assert "joined" not in line and cla.statement("1.0") not in out  # the public row carries no evidence text
    assert cla.parse_record(in_force(line))[2] == []  # and it is itself a valid row


def test_edited_wrong_or_foreign_comments_are_refused_at_verification(tmp_path, capsys):
    for bad in (comment(updated="2026-11-06T09:00:00Z"), comment(body=cla.statement("0.9")),
                comment(body="I agree"), comment(body="No. " + cla.statement("1.0")),
                comment(kind="Bot"), comment(url="https://github.com/vchokshi/viharbot/pull/7#issuecomment-1")):
        code, out = verify(tmp_path, FakeGH({COMMENT_PATH: bad}), capsys)
        assert code == 1 and "Not recordable" in out
    other = comment()
    other["issue_url"] = "https://api.github.com/repos/someone/else/issues/7"
    assert verify(tmp_path, FakeGH({COMMENT_PATH: other}), capsys)[0] == 1
    assert verify(tmp_path, FakeGH({COMMENT_PATH: comment()}), capsys, record_text=record())[0] == 1  # draft
    assert not (tmp_path / "private" / "EV-2002-99.json").exists()


def test_verify_refuses_other_repos_bad_urls_network_failure_and_public_output_paths(tmp_path, capsys):
    gh = FakeGH({COMMENT_PATH: comment()})
    assert verify(tmp_path, gh, capsys, url="https://evil.example/x")[0] == 2
    assert verify(tmp_path, gh, capsys, url="https://github.com/other/repo/pull/7#issuecomment-99")[0] == 2
    assert verify(tmp_path, FakeGH({COMMENT_PATH: cla.GitHubError("down")}), capsys)[0] == 2
    inside = ROOT / "evidence-should-not-exist"
    assert verify(tmp_path, gh, capsys, out=inside)[0] == 2 and not inside.exists()


def make_audit_case(tmp_path, capsys):
    code, out = verify(tmp_path, FakeGH({COMMENT_PATH: comment()}), capsys)
    assert code == 0
    line = next(ln for ln in out.splitlines() if ln.startswith("| 2002"))
    rec = tmp_path / "rec.md"
    rec.write_text(in_force(line), encoding="utf-8")
    return rec, tmp_path / "private", line


def run_audit(tmp_path, rec, evidence_dir, live):
    args = argparse.Namespace(repo=REPO, evidence_dir=str(evidence_dir), acceptances_file=str(rec))
    return cla.run_audit(args, gh=FakeGH({COMMENT_PATH: live}))


def test_audit_passes_when_private_evidence_and_the_live_comment_agree(tmp_path, capsys):
    rec, private, _ = make_audit_case(tmp_path, capsys)
    assert run_audit(tmp_path, rec, private, comment()) == 0


def test_a_deleted_or_edited_comment_after_verification_is_a_warning_not_a_revocation(tmp_path, capsys):
    rec, private, _ = make_audit_case(tmp_path, capsys)
    assert run_audit(tmp_path, rec, private, cla.NotFound("gone")) == 0
    assert "deleted after verification" in capsys.readouterr().out
    assert run_audit(tmp_path, rec, private, comment(body="Actually no.", updated="2026-11-09T00:00:00Z")) == 0
    assert "edited after verification" in capsys.readouterr().out
    # The gate reads only the public record, so neither event changes its decision.
    assert judge(rec=rec.read_text()).ok


def test_missing_or_invalid_private_evidence_fails_the_audit(tmp_path, capsys):
    rec, private, _ = make_audit_case(tmp_path, capsys)
    path = private / "EV-2002-99.json"
    saved = path.read_text()
    path.unlink()
    assert run_audit(tmp_path, rec, private, comment()) == 1 and "missing or unreadable" in capsys.readouterr().out
    tampered = json.loads(saved)
    tampered["evidence"]["account"]["id"] = 9
    path.write_text(json.dumps(tampered))
    assert run_audit(tmp_path, rec, private, comment()) == 1 and "does not match the public digest" in capsys.readouterr().out
    path.write_text("not json")
    assert run_audit(tmp_path, rec, private, comment()) == 1
    path.write_text(saved)
    other_author = comment(account=BOB)
    assert run_audit(tmp_path, rec, private, other_author) == 1  # live comment is by someone else
    wrong_eligibility = rec.read_text().replace("adult-individual", "corporate-cla")
    rec.write_text(wrong_eligibility)
    assert run_audit(tmp_path, rec, private, comment()) == 1 and "disagrees" in capsys.readouterr().out


def test_audit_fails_when_the_live_comment_cannot_be_read_or_the_record_is_invalid(tmp_path, capsys):
    rec, private, _ = make_audit_case(tmp_path, capsys)
    assert run_audit(tmp_path, rec, private, cla.GitHubError("boom")) == 1
    rec.write_text("garbage")
    assert run_audit(tmp_path, rec, private, comment()) == 1


# ---- GitHub client: pagination, retry, rate limit, large pull requests ------------------------
class Resp:
    def __init__(self, body, headers=None):
        self.body, self.headers = json.dumps(body).encode(), headers or {}

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", headers or email.message.Message(), None)


def client(sequence, sleeps, clock=1000.0, **kwargs):
    items = iter(sequence)

    def opener(request, timeout=None):
        item = next(items)
        if isinstance(item, Exception):
            raise item
        return item

    return cla.GitHub("t", opener=opener, sleep=sleeps.append, clock=lambda: clock, **kwargs)


def test_pagination_follows_links_and_stays_on_the_api():
    link = {"Link": '<https://api.github.com/x?page=2>; rel="next"'}
    gh = client([Resp([1, 2], link), Resp([3])], [])
    assert gh.paginate("/x", 5) == [1, 2, 3]
    evil = {"Link": '<https://evil.example/x>; rel="next"'}
    with pytest.raises(cla.GitHubError):
        client([Resp([1], evil)], []).paginate("/x", 5)
    forever = {"Link": '<https://api.github.com/x?page=2>; rel="next"'}
    with pytest.raises(cla.GitHubError, match="more pages"):
        client([Resp([1], forever)] * 5, []).paginate("/x", 2)


def test_transient_errors_are_retried_with_backoff_then_fail_closed():
    sleeps: list[float] = []
    assert client([http_error(503), http_error(502), Resp({"ok": 1})], sleeps).get("/x")[0] == {"ok": 1}
    assert sleeps == [1.0, 2.0]
    sleeps.clear()
    flaky = [urllib.error.URLError("down"), TimeoutError(), Resp({"ok": 2})]
    assert client(flaky, sleeps).get("/x")[0] == {"ok": 2}
    with pytest.raises(cla.GitHubError, match="after 4 attempts"):
        client([http_error(500)] * 4, []).get("/x")


def test_rate_limits_wait_briefly_but_never_hang_and_permission_errors_do_not_retry():
    sleeps: list[float] = []
    assert client([http_error(429, {"Retry-After": "3"}), Resp({"ok": 1})], sleeps).get("/x")[0] == {"ok": 1}
    assert sleeps == [3.0]
    far = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"}
    with pytest.raises(cla.GitHubError, match="rate limit"):
        client([http_error(403, far)], []).get("/x")
    soon = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1010"}
    waits: list[float] = []
    assert client([http_error(403, soon), Resp({"ok": 1})], waits).get("/x")[0] == {"ok": 1} and waits == [11.0]
    with pytest.raises(cla.GitHubError, match="HTTP 403"):
        client([http_error(403)], []).get("/x")
    with pytest.raises(cla.NotFound):
        client([http_error(404)], []).get("/x")


def pr_routes(commits, files, meta_commits=None, meta_files=None):
    return {
        f"/repos/{REPO}/pulls/7": {"user": user(ALICE), "commits": meta_commits if meta_commits is not None else len(commits),
                                   "changed_files": meta_files if meta_files is not None else len(files)},
        f"/repos/{REPO}/pulls/7/commits": commits,
        f"/repos/{REPO}/pulls/7/files": files,
    }


def raw_commit(login=ALICE, message="m"):
    return {"sha": "a" * 40, "commit": {"message": message, "author": {"email": "a@example.com"}},
            "author": user(login), "committer": user(WEB_FLOW)}


def test_fetch_normalises_commits_files_and_renames():
    files = [{"filename": "a.py"}, {"filename": "b.py", "previous_filename": "old.py"}]
    pr = cla.fetch_pull_request(FakeGH(pr_routes([raw_commit()], files)), REPO, 7)
    assert pr["unsupported"] == [] and pr["files"] == ["a.py", "b.py", "old.py"]
    assert pr["commits"][0]["author"]["login"] == "alice" and pr["author"]["id"] == 2002


def test_large_or_partial_pull_requests_are_unsupported_and_fail_closed():
    big = cla.fetch_pull_request(FakeGH(pr_routes([], [], meta_commits=300)), REPO, 7)
    assert any("exceed" in u for u in big["unsupported"])
    result = cla.evaluate(cla.Files(IN_FORCE, EXC, REGISTRY, AGREEMENT), big, lambda x: None, TODAY)
    assert not result.ok and "Unsupported pull request" in errors_text(result)
    partial = cla.fetch_pull_request(FakeGH(pr_routes([raw_commit()], [], meta_commits=2)), REPO, 7)
    assert any("1 of 2" in u for u in partial["unsupported"])
    many_files = cla.fetch_pull_request(FakeGH(pr_routes([raw_commit()], [], meta_files=3001)), REPO, 7)
    assert any("3001" in u for u in many_files["unsupported"])


def run_pr(tmp_path, gh, capsys, head=None):
    for name, text in ((cla.RECORD_PATH, IN_FORCE), (cla.EXCEPTIONS_PATH, EXC), (cla.AGREEMENT_PATH, AGREEMENT),
                       ("CONTRIBUTORS.md", REGISTRY)):
        target = tmp_path / "base" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    args = argparse.Namespace(repo=REPO, pr=7, base_dir=str(tmp_path / "base"), head_dir=head)
    code = cla.run_check_pr(args, gh=gh)
    return code, capsys.readouterr().out


def test_check_pr_passes_for_a_covered_pull_request(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    gh = FakeGH(pr_routes([raw_commit()], [{"filename": "README.md"}]))
    code, out = run_pr(tmp_path, gh, capsys)
    assert code == 0 and "**PASS**" in out


def test_check_pr_fails_closed_on_any_github_failure(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    routes = pr_routes([raw_commit()], [{"filename": "README.md"}])
    routes[f"/repos/{REPO}/pulls/7"] = cla.GitHubError("rate limit")
    code, out = run_pr(tmp_path, FakeGH(routes), capsys)
    assert code == 1 and "fails closed" in out
    legacy = pr_routes([raw_commit(message="x\n\nCo-authored-by: B <bob@users.noreply.github.com>")], [])
    legacy["/users/bob"] = cla.GitHubError("down")
    code, out = run_pr(tmp_path, FakeGH(legacy), capsys)
    assert code == 1 and "fails closed" in out


def test_github_resolver_maps_not_found_and_failures():
    resolve = cla.github_resolver(FakeGH({"/users/bob": {"id": 3003}, "/users/ghost": cla.NotFound("x"),
                                          "/users/odd": {"id": "x"}, "/users/down": cla.GitHubError("down")}))
    assert resolve("bob") == 3003 and resolve("ghost") is None and resolve("odd") is None
    with pytest.raises(cla.IdentityLookup):
        resolve("down")


def test_offline_check_reads_files_and_reports_unreadable_input(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    base = tmp_path / "base"
    for name, text in ((cla.RECORD_PATH, IN_FORCE), (cla.EXCEPTIONS_PATH, EXC), (cla.AGREEMENT_PATH, AGREEMENT),
                       ("CONTRIBUTORS.md", REGISTRY)):
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_text(text, encoding="utf-8")
    pr_file = tmp_path / "pr.json"
    pr_file.write_text(json.dumps(make_pr()))
    args = argparse.Namespace(pr_file=str(pr_file), base_dir=str(base), head_dir=None)
    assert cla.run_check(args) == 0 and "**PASS**" in capsys.readouterr().out
    pr_file.write_text(json.dumps(make_pr(BOB)))
    assert cla.run_check(args) == 1
    pr_file.write_text("{not json")
    assert cla.run_check(args) == 1 and "could not be read" in capsys.readouterr().out
    (base / cla.RECORD_PATH).unlink()
    pr_file.write_text(json.dumps(make_pr()))
    assert cla.run_check(args) == 1


def test_step_summary_receives_the_report(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert cla.finish(judge()) == 0
    assert "CLA acceptance check" in summary.read_text()


# ---- activation and the live ruleset ----------------------------------------------------------
RULES_PATH = f"/repos/{REPO}/rules/branches/main"


def rules(check=True, pinned=True, owners=True, approvals=1, base=True, strict=True):
    out = [{"type": "pull_request", "parameters": {"required_approving_review_count": approvals,
                                                    "require_code_owner_review": owners}}]
    if base:
        out += [{"type": "deletion"}, {"type": "non_fast_forward"}]
    contexts = [{"context": "CI result", "integration_id": 15368}]
    if check:
        contexts.append({"context": "CLA acceptance", **({"integration_id": 15368} if pinned else {})})
    out.append({"type": "required_status_checks",
                "parameters": {"required_status_checks": contexts, "strict_required_status_checks_policy": strict}})
    return out


def activation(tmp_path, live, rec=None):
    path = tmp_path / "rec.md"
    path.write_text(rec if rec is not None else in_force(), encoding="utf-8")
    return cla.activation_report(REPO, "main", path.read_text(), FakeGH({RULES_PATH: live}))


def test_activation_passes_only_when_the_live_ruleset_and_record_are_ready(tmp_path):
    lines, ok = activation(tmp_path, rules())
    assert ok and all(status == "PASS" for status, _ in lines)


@pytest.mark.parametrize("live", [
    [], rules(check=False), rules(pinned=False), rules(owners=False), rules(approvals=0), rules(base=False),
    rules(strict=False), cla.NotFound("rules"),
])
def test_missing_or_inactive_ruleset_blocks_activation(tmp_path, live):
    lines, ok = activation(tmp_path, live)
    assert not ok and any(status == "FAIL" for status, _ in lines)


def test_open_prerequisites_block_activation_even_with_a_perfect_ruleset(tmp_path):
    lines, ok = activation(tmp_path, rules(), rec=record())
    assert not ok and any(status == "BLOCKED" for status, _ in lines)
    assert not activation(tmp_path, rules(), rec="garbage")[1]


def test_activation_command_lists_manual_items_and_never_claims_more(tmp_path, capsys):
    path = tmp_path / "rec.md"
    path.write_text(record(), encoding="utf-8")
    args = argparse.Namespace(repo=REPO, branch="main", acceptances_file=str(path))
    assert cla.run_activation_check(args, gh=FakeGH({RULES_PATH: rules()})) == 1
    out = capsys.readouterr().out
    manual = [ln for ln in out.splitlines() if ln.startswith("MANUAL")]
    assert len(manual) == len(cla.MANUAL_ITEMS) and "cannot verify the MANUAL items" in out


def test_hash_agreement_ignores_line_ending_style(tmp_path, capsys):
    unix, windows = tmp_path / "a.md", tmp_path / "b.md"
    unix.write_text("x\ny\n", encoding="utf-8")
    windows.write_bytes(b"x\r\ny\r\n")
    assert cla.run_hash_agreement(argparse.Namespace(file=str(unix))) == 0
    first = capsys.readouterr().out
    assert cla.run_hash_agreement(argparse.Namespace(file=str(windows))) == 0
    assert capsys.readouterr().out == first and cla.run_hash_agreement(argparse.Namespace(file=str(tmp_path / "no"))) == 2


# ---- the files as shipped ---------------------------------------------------------------------
def shipped(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_shipped_record_and_exceptions_are_valid_and_consistent_with_the_agreement():
    terms, rows, errors = cla.parse_record(shipped("CLA-ACCEPTANCES.md"))
    # Rows are added over time (never removed), so assert they are well formed, not that none exist.
    assert errors == []
    assert len({r.account_id for r in rows}) == len(rows)  # one row per account
    assert all(r.evidence_id and len(r.evidence_sha) == 64 for r in rows)  # older versions stay valid rows
    agreement = shipped(cla.AGREEMENT_PATH)
    assert cla.agreement_header(agreement) == (terms.legal, terms.editorial)
    if terms.in_force:  # activation must come with every prerequisite and the frozen hash
        assert terms.gaps() == [] and cla.sha256_text(agreement) == terms.sha
    rules_, rule_errors = cla.parse_exceptions(shipped("CLA-EXCEPTIONS.md"))
    assert rule_errors == [] and {r.username for r in rules_} == {"dependabot[bot]", "web-flow", "-"}
    assert all(r.authorizer == "vchokshi" for r in rules_)
    assert [r.scope for r in rules_ if r.kind == "attribution"] == ["email:noreply@anthropic.com"]


def test_shipped_agreement_states_the_decisions():
    text = shipped(cla.AGREEMENT_PATH)
    template = re.search(r"```text\n(.+?)\n```", text, re.S).group(1)
    assert template == cla.statement("<VERSION>")
    for needle in ("Vihar Chokshi, an individual", "State of Arizona", "Maricopa County", "at least 18",
                   "Nothing in this agreement assigns", "Successor", "irrevocable", "renewed Acceptance",
                   "Editorial revision", "Withdrawal is prospective only", "Not reviewed by counsel",
                   "No lawyer has reviewed it"):
        assert needle in text, needle
    assert "[COUNSEL" not in text and "DECISION:" not in text  # no unresolved blanks in a frozen text
    assert "counsel-approved" not in text.replace("Not reviewed by counsel", "")


def test_shipped_state_refuses_an_unaccepted_external_pull_request_and_admits_the_founder():
    files = cla.Files(shipped("CLA-ACCEPTANCES.md"), shipped("CLA-EXCEPTIONS.md"), shipped("CONTRIBUTORS.md"),
                      shipped(cla.AGREEMENT_PATH))
    terms = cla.parse_record(files.base_record)[0]
    external = cla.evaluate(files, make_pr(BOB), lambda x: None, TODAY)
    assert not external.ok  # draft: "none is in force"; in force: "no recorded acceptance"
    assert "none is in force" in errors_text(external) or "no recorded acceptance" in errors_text(external)
    founders, _ = cla.founders_from(files.base_registry)
    founder = next(iter(founders.items()))
    assert cla.evaluate(files, make_pr(founder), lambda x: None, TODAY).ok
    assert cla.evaluate(files, make_pr(DEPENDABOT), lambda x: None, TODAY).ok
    report = cla.render(cla.evaluate(files, make_pr(BOB), lambda x: None, TODAY))
    assert ("not been reviewed by counsel" in report) == terms.review_waived


def test_waived_legal_review_is_allowed_only_explicitly_and_is_always_disclosed():
    waived = record(status="in-force", sha=cla.sha256_text(AGREEMENT), review="waived: founder decision, risk accepted",
                    approval="approved: founder", storage="verified: ok", ruleset="verified: ok")
    terms, _, errors = cla.parse_record(waived)
    assert errors == [] and terms.review_waived and terms.gaps() == []
    assert "not been reviewed by counsel" in cla.render(judge(rec=waived))
    assert "not been reviewed by counsel" not in cla.render(judge(rec=IN_FORCE))
    for bad in ("waived", "waived:", "skipped: yes", "pending"):
        text = waived.replace("waived: founder decision, risk accepted", bad)
        assert cla.parse_record(text)[2], bad  # bare or unknown values are invalid, pending blocks in-force


# ---- workflow, ruleset and ownership guardrails -----------------------------------------------
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def run_blocks(text: str) -> list[str]:
    blocks, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        m = re.match(r"^(\s*)run:\s*\|\s*$", lines[i])
        if m:
            indent, body, i = len(m.group(1)), [], i + 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                body.append(lines[i])
                i += 1
            blocks.append("\n".join(body))
        else:
            i += 1
    return blocks


def test_workflow_parses_and_uses_pull_request_target_on_main_only():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(workflow_text())
    triggers = data.get("on", data.get(True))
    assert list(triggers) == ["pull_request_target"] and triggers["pull_request_target"] == {"branches": ["main"]}
    assert data["name"] == "CLA acceptance" and data["jobs"]["cla"]["name"] == "CLA acceptance"


def test_the_required_job_cannot_be_skipped_into_a_pass():
    yaml = pytest.importorskip("yaml")
    job = yaml.safe_load(workflow_text())["jobs"]["cla"]
    assert "if" not in job and "needs" not in job and "continue-on-error" not in job
    assert all("if" not in step and "continue-on-error" not in step for step in job["steps"])
    text = workflow_text()
    assert "paths:" not in text and "paths-ignore:" not in text and "types:" not in text
    assert "|| true" not in text and "continue-on-error" not in text


def test_workflow_never_checks_out_or_runs_pull_request_code():
    text = workflow_text()
    assert text.count("actions/checkout@") == 1
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text and "persist-credentials: false" in text
    assert "pull_request.head" not in text
    assert text.count("refs/pull/") == 1  # fetched as data only, then read with `git show`
    for forbidden in ("pip install", "npm ", "make ", "bash FETCH_HEAD", "source ", "docker ", "git checkout",
                      "git merge", "python3 $RUNNER_TEMP", "./$"):
        assert forbidden not in text
    assert "python3 .github/scripts/cla_gate.py check-pr" in text


def test_workflow_has_least_privilege_and_no_secrets():
    text = workflow_text()
    assert "secrets." not in text
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)
    perms = re.findall(r"^\s+(\w[\w-]*): (read|write|none)\s*$", text, re.M)
    assert perms and all(level == "read" for _, level in perms)
    assert "id-token" not in text and "write" not in text.replace("tests/", "")


def test_workflow_passes_event_data_through_env_not_inline_expressions():
    blocks = run_blocks(workflow_text())
    assert blocks and all("${{" not in block for block in blocks)
    assert 'PR_NUMBER: ${{ github.event.pull_request.number }}' in workflow_text()
    assert "title" not in workflow_text().lower().replace("# ", "") or "pull_request.title" not in workflow_text()
    assert "pull_request.body" not in workflow_text() and "head_ref" not in workflow_text()


def test_workflow_actions_are_pinned_to_commit_shas():
    uses = re.findall(r"uses:\s*(\S+)", workflow_text())
    assert uses and all(re.fullmatch(r".+@[0-9a-f]{40}", u) for u in uses)


def test_ruleset_requires_the_check_pinned_to_actions_and_code_owner_review():
    data = json.loads(RULESET.read_text())
    assert data["enforcement"] == "active"
    pull = next(r for r in data["rules"] if r["type"] == "pull_request")["parameters"]
    assert pull["require_code_owner_review"] is True and pull["required_approving_review_count"] >= 1
    status_rule = next(r for r in data["rules"] if r["type"] == "required_status_checks")["parameters"]
    assert status_rule["strict_required_status_checks_policy"] is True  # freshness: stale successes cannot merge
    checks = status_rule["required_status_checks"]
    mine = [c for c in checks if c["context"] == "CLA acceptance"]
    assert mine == [{"context": "CLA acceptance", "integration_id": cla.ACTIONS_APP_ID}]
    assert {"CI result", "Contributor registry", "Dependency review"} <= {c["context"] for c in checks}


def test_every_protected_path_has_a_code_owner():
    lines = [ln.split() for ln in CODEOWNERS.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    patterns = {pattern: owners for pattern, *owners in lines}

    def owned(path):
        return any(owners == ["@vchokshi"] and (pat == "/" + path or (pat.endswith("/") and ("/" + path).startswith(pat)))
                   for pat, owners in patterns.items())

    missing = [p for p in sorted(cla.PROTECTED_PATHS) if not owned(p)]
    assert missing == []


def test_script_depends_only_on_the_standard_library_and_the_registry_script():
    imports = set(re.findall(r"(?m)^(?:from|import) (\w+)", SCRIPT.read_text()))
    assert imports <= {"argparse", "datetime", "hashlib", "json", "os", "re", "sys", "time", "urllib", "dataclasses",
                       "collections", "pathlib", "__future__", "contributor_registry"}


def test_untrusted_names_cannot_inject_workflow_commands():
    hostile = (5005, "bad\n::error::pwned`x")
    report = cla.render(judge(author=hostile, commits=[commit(hostile)]))
    assert "\n::error::" not in report and "`x" not in report.replace("`bad", "")


def test_documented_evidence_procedure_does_not_write_into_a_checkout():
    # Sage finding: the docs told the founder to write straight into the private repository's
    # working tree, which verify-comment correctly refuses. The documented path must be a staging
    # directory outside every checkout, followed by an explicit copy.
    for name in ("docs/governance/cla-acceptance.md", "docs/governance/cla-private-evidence.md"):
        for line in shipped(name).splitlines():
            if "--evidence-out" in line:
                target = line.split("--evidence-out")[1].split()[0].strip('"')
                assert "viharbot-cla-evidence" not in target, (name, line)
                assert not cla.inside_a_git_checkout(Path(target.replace("~", str(Path.home())))), (name, target)


def test_freshness_is_documented_with_its_limits():
    text = shipped("docs/governance/cla-acceptance.md")
    assert "Freshness" in text and "up to date" in text and "expiry" in text.lower()
