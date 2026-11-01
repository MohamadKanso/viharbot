import pytest

from pixel_core_helpers import all_specs, payload_for
from viharbot.pixel.models import DeckManifest, SlideBatch, SlideStub
from viharbot.pixel.registry import get_layout_spec
from viharbot.pixel.validate import errors, validate_batch, validate_manifest, validate_payload

ROADMAP = "LAYOUT_20_MULTI_YEAR_ROADMAP"
RULE_LAYOUT = next(s.layout_id for s in all_specs() if s.layout_id.startswith("LAYOUT_23"))
BADGE_LAYOUT = next(s.layout_id for s in all_specs() if s.badge_tokens)


def spec(layout_id):
    return get_layout_spec(layout_id)


def messages(issues, severity=None):
    return [i.message for i in issues if severity is None or i.severity == severity]


def with_token(layout_id, name, text, **kw):
    p = payload_for(spec(layout_id))
    return p.model_copy(update={"tokens": {**p.tokens, name: text}, **kw})


@pytest.mark.parametrize("s", all_specs(), ids=lambda s: s.layout_id)
def test_placeholder_payload_has_no_errors(s):
    assert errors(validate_payload(payload_for(s))) == []


def test_undeclared_missing_and_empty_tokens():
    p = payload_for(spec(RULE_LAYOUT))
    tokens = dict(p.tokens)
    removed = tokens.pop("SLIDE_TAG")
    tokens["BOGUS"] = "x"
    tokens["SLIDE_TITLE"] = "   "
    msgs = messages(validate_payload(p.model_copy(update={"tokens": tokens})))
    assert any("undeclared tokens" in m and "BOGUS" in m for m in msgs)
    assert any("missing tokens" in m and "SLIDE_TAG" in m for m in msgs)
    assert any("SLIDE_TITLE is empty" in m for m in msgs)
    assert removed


@pytest.mark.parametrize("text", ["a {{X}} b", "oops }}"])
def test_token_delimiters_rejected(text):
    msgs = messages(validate_payload(with_token(RULE_LAYOUT, "SLIDE_TITLE", text)))
    assert any("SLIDE_TITLE contains a token delimiter" in m for m in msgs)


def test_word_limits_default_and_rule():
    fifteen = " ".join(["w"] * 15)
    assert errors(validate_payload(with_token(RULE_LAYOUT, "SLIDE_TITLE", fifteen))) == []
    msgs = messages(validate_payload(with_token(RULE_LAYOUT, "SLIDE_TITLE", fifteen + " w")))
    assert "SLIDE_TITLE exceeds 15 words" in msgs
    thirty = " ".join(["w"] * 30)
    assert errors(validate_payload(with_token(RULE_LAYOUT, "RULE", thirty))) == []
    assert "RULE exceeds 30 words" in messages(validate_payload(with_token(RULE_LAYOUT, "RULE", thirty + " w")))


@pytest.mark.parametrize("text", [
    "The platform is compliant", "Vendors are certified", "We are in compliance with NIST",
    "The design achieves compliance", "It is fully compliant", "Systems were authorised",
])
def test_forbidden_phrases_are_errors(text):
    msgs = messages(validate_payload(with_token(RULE_LAYOUT, "SLIDE_TITLE", text)))
    assert any("compliance or certification claim" in m for m in msgs)


def test_negated_compliance_is_allowed():
    assert errors(validate_payload(with_token(RULE_LAYOUT, "SLIDE_TITLE", "The estate is not compliant"))) == []


def test_implemented_requires_evidence():
    p = payload_for(spec(RULE_LAYOUT), claim_status="implemented")
    assert any("requires evidence_refs" in m for m in messages(validate_payload(p)))
    assert errors(validate_payload(p.model_copy(update={"evidence_refs": ["e1"]}))) == []


def test_unknown_layout_short_circuits():
    p = payload_for(spec(RULE_LAYOUT), layout_id="LAYOUT_99")
    assert messages(validate_payload(p)) == ["unknown layout LAYOUT_99"]


def test_layout_differs_from_stub():
    p = payload_for(spec(RULE_LAYOUT))
    stub = SlideStub(slide_id=p.slide_id, layout_id=BADGE_LAYOUT, title="t", purpose="p")
    assert any("differs from planned" in m for m in messages(validate_payload(p, stub)))


def test_badge_overrides_missing_and_undeclared():
    s = spec(BADGE_LAYOUT)
    p = payload_for(s)
    overrides = dict(p.status_overrides)
    gone = next(iter(overrides))
    del overrides[gone]
    overrides["NOT_A_BADGE"] = "LOW"
    msgs = messages(validate_payload(p.model_copy(update={"status_overrides": overrides})))
    assert any("missing badge overrides" in m and gone in m for m in msgs)
    assert any("undeclared badge overrides" in m and "NOT_A_BADGE" in m for m in msgs)


def roadmap(**figures):
    base = {f"HORIZON_{i}_{k}": "$1,000" for i in (1, 2, 3) for k in ("CAPEX", "OPEX")}
    base["TOTAL_INVESTMENT"] = "$6,000"
    base.update(figures)
    p = payload_for(spec(ROADMAP))
    return p.model_copy(update={"tokens": {**p.tokens, **base}})


def test_roadmap_matching_total_passes():
    assert validate_payload(roadmap()) == []


def test_roadmap_mismatch_is_warning_not_error():
    issues = validate_payload(roadmap(TOTAL_INVESTMENT="$9,000"))
    assert errors(issues) == []
    assert len(issues) == 1 and issues[0].severity == "warning"
    assert "does not equal" in issues[0].message


@pytest.mark.parametrize("field", ["TOTAL_INVESTMENT", "HORIZON_2_OPEX"])
def test_roadmap_non_numeric_warns_not_checked(field):
    issues = validate_payload(roadmap(**{field: "TBD"}))
    assert errors(issues) == []
    assert any("totals not checked" in m for m in messages(issues, "warning"))


def test_roadmap_money_formats():
    p = roadmap(HORIZON_1_CAPEX="1.5M", HORIZON_1_OPEX="500K", HORIZON_2_CAPEX="2,000,000",
                HORIZON_2_OPEX="$0", HORIZON_3_CAPEX="0.5b", HORIZON_3_OPEX="0",
                TOTAL_INVESTMENT="$500M")
    # 1.5M + 0.5M + 2M + 0 + 500M + 0 = 504M
    assert any("does not equal" in m for m in messages(validate_payload(p)))
    assert validate_payload(p.model_copy(update={"tokens": {**p.tokens, "TOTAL_INVESTMENT": "504M"}})) == []


def test_roadmap_total_within_tolerance():
    assert validate_payload(roadmap(TOTAL_INVESTMENT="$6,020")) == []


def stub_for(p, evidence=()):
    return SlideStub(slide_id=p.slide_id, layout_id=p.layout_id, title="t", purpose="p",
                     evidence_refs=list(evidence))


def two_payloads():
    a = payload_for(spec(RULE_LAYOUT), slide_id="a")
    b = payload_for(spec(BADGE_LAYOUT), slide_id="b")
    return a, b


def test_batch_happy_path():
    a, b = two_payloads()
    assert validate_batch(SlideBatch(slides=[a, b]), [stub_for(a), stub_for(b)]) == []
    assert validate_batch(SlideBatch(slides=[a, b])) == []


def test_batch_duplicates_unplanned_and_missing():
    a, b = two_payloads()
    msgs = messages(validate_batch(SlideBatch(slides=[a, a]), [stub_for(a)]))
    assert "duplicate slide_id in batch" in msgs
    msgs = messages(validate_batch(SlideBatch(slides=[a]), [stub_for(b)]))
    assert any("unplanned slides" in m for m in msgs)
    assert any("missing slides" in m for m in msgs)


def test_batch_evidence_must_be_in_stub():
    a, _ = two_payloads()
    a = a.model_copy(update={"evidence_refs": ["e2"]})
    msgs = messages(validate_batch(SlideBatch(slides=[a]), [stub_for(a, ["e1"])]))
    assert "evidence_refs not in the planned stub" in msgs
    assert validate_batch(SlideBatch(slides=[a]), [stub_for(a, ["e1", "e2"])]) == []


def manifest(*stubs):
    return DeckManifest(deck_id="d", sections=[
        {"section_id": "s", "title": "S", "theme": "dark", "slides": list(stubs)}])


def mstub(sid="1", layout=RULE_LAYOUT, **kw):
    return SlideStub(slide_id=sid, layout_id=layout, title="t", purpose="p", **kw)


def test_manifest_valid_and_empty():
    assert validate_manifest(manifest(mstub(evidence_refs=["e"], frameworks=["nist_csf_2"])), {"e"}) == []
    issues = validate_manifest(DeckManifest(deck_id="d", sections=[]), set())
    assert messages(issues) == ["manifest has no slides"] and issues[0].severity == "error"


def test_manifest_errors():
    m = manifest(mstub("1", evidence_refs=["x"], frameworks=["bogus"]), mstub("1", layout="LAYOUT_99"))
    msgs = messages(errors(validate_manifest(m, set())))
    assert "duplicate slide_id" in msgs
    assert any("unknown layout LAYOUT_99" in x for x in msgs)
    assert any("unknown framework key bogus" in x for x in msgs)
    assert any("evidence_ref x was not supplied" in x for x in msgs)


def test_manifest_stub_without_evidence_is_warning():
    issues = validate_manifest(manifest(mstub()), set())
    assert errors(issues) == []
    assert [i.severity for i in issues] == ["warning"]


def test_errors_filters_warnings():
    issues = validate_payload(roadmap(TOTAL_INVESTMENT="$9,000"))
    assert issues and errors(issues) == []
