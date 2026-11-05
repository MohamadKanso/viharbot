"""The CI helper scripts read real reports and must never invent numbers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / ".github" / "scripts"
pytestmark = pytest.mark.skipif(not SCRIPTS.exists(), reason=".github/scripts is not in this environment")


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


JUNIT = """<testsuites><testsuite name="pytest" tests="10" failures="1" errors="0" skipped="2">
</testsuite><testsuite name="more" tests="5" failures="0" errors="1" skipped="0"></testsuite></testsuites>"""
COVERAGE = '<coverage lines-valid="200" lines-covered="199" line-rate="0.995"></coverage>'


def test_junit_counts_come_from_the_report(tmp_path):
    junit = load("junit_summary")
    path = tmp_path / "r.xml"
    path.write_text(JUNIT)
    result = junit.counts(path)
    assert result == {"tests": 15, "failures": 1, "errors": 1, "skipped": 2, "passed": 11}
    assert "failed" in junit.markdown("Python", result) and "11 passed" in junit.markdown("Python", result)


def test_junit_exit_status_reflects_failures_and_optionally_skips(tmp_path):
    junit = load("junit_summary")
    clean = tmp_path / "c.xml"
    clean.write_text('<testsuite tests="3" failures="0" errors="0" skipped="1"></testsuite>')
    assert junit.main([str(clean)]) == 0
    assert junit.main([str(clean), "--fail-on-skipped"]) == 1
    bad = tmp_path / "b.xml"
    bad.write_text(JUNIT)
    assert junit.main([str(bad)]) == 1


def test_junit_allow_skip_tolerates_only_the_named_skips(tmp_path, capsys):
    junit = load("junit_summary")
    allowed = r"^set RUN_LITELLM_"

    def report(*messages):
        cases = "".join(f'<testcase classname="t" name="n{i}"><skipped message="{m}"/></testcase>'
                        for i, m in enumerate(messages))
        path = tmp_path / f"r{len(messages)}{abs(hash(messages))}.xml"
        path.write_text(f'<testsuite tests="{len(messages) + 1}" failures="0" errors="0" '
                        f'skipped="{len(messages)}">{cases}</testsuite>')
        return str(path)

    live = "set RUN_LITELLM_INFERENCE=1 to make a real model call"
    assert junit.main([report(live, live), "--fail-on-skipped", "--allow-skip", allowed]) == 0
    # a Node skip is not an opt-in live check, so it fails the run and is named on stderr
    assert junit.main([report(live, "Node.js is not installed"), "--fail-on-skipped",
                       "--allow-skip", allowed]) == 1
    assert "unexpected skip: t::n1: Node.js is not installed" in capsys.readouterr().err
    # a skip with no message cannot be vouched for
    assert junit.main([report(""), "--fail-on-skipped", "--allow-skip", allowed]) == 1
    # without --fail-on-skipped the option changes nothing
    assert junit.main([report("Node.js is not installed"), "--allow-skip", allowed]) == 0


def test_coverage_is_rounded_down_and_never_overstated(tmp_path):
    cov = load("coverage_summary")
    path = tmp_path / "coverage.xml"
    path.write_text(COVERAGE)
    assert cov.measured(path)["percent"] == pytest.approx(99.5)
    assert cov.badge(99.5)["message"] == "99%" and cov.badge(99.999)["message"] == "99%"


@pytest.mark.parametrize("percent,color", [(95, "brightgreen"), (85, "green"), (75, "yellowgreen"),
                                           (65, "yellow"), (55, "orange"), (10, "red")])
def test_badge_color_follows_the_measured_value(percent, color):
    assert load("coverage_summary").badge(percent)["color"] == color


def test_badge_json_is_a_shields_endpoint_document(tmp_path):
    cov = load("coverage_summary")
    xml, out = tmp_path / "coverage.xml", tmp_path / "coverage.json"
    xml.write_text(COVERAGE)
    assert cov.main([str(xml), "--badge-json", str(out)]) == 0
    document = json.loads(out.read_text())
    assert document["schemaVersion"] == 1 and document["label"] == "coverage"
    assert set(document) == {"schemaVersion", "label", "message", "color", "cacheSeconds"}


def test_an_empty_report_is_an_error_not_a_zero_or_a_hundred(tmp_path):
    cov = load("coverage_summary")
    path = tmp_path / "coverage.xml"
    path.write_text('<coverage lines-valid="0" lines-covered="0"></coverage>')
    with pytest.raises(ValueError):
        cov.measured(path)
