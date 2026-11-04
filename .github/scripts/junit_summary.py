#!/usr/bin/env python3
"""Summarize JUnit XML into counts and a Markdown line. Reads real results; invents nothing."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def counts(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in total:
            total[key] += int(suite.get(key, 0))
    total["passed"] = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    return total


def unexpected_skips(path: Path, allowed: str) -> list[str]:
    """Skipped test cases whose skip message does not match `allowed` (a regular expression)."""
    root = ET.parse(path).getroot()
    pattern = re.compile(allowed)
    found = []
    for case in root.iter("testcase"):
        for skip in case.findall("skipped"):
            message = skip.get("message", "")
            if not pattern.search(message):
                found.append(f"{case.get('classname', '')}::{case.get('name', '')}: {message}")
    return found


def markdown(name: str, result: dict[str, int]) -> str:
    status = "failed" if result["failures"] or result["errors"] else "passed"
    return (f"| {name} | {status} | {result['passed']} passed, {result['failures']} failed, "
            f"{result['errors']} errors, {result['skipped']} skipped (of {result['tests']}) |")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path)
    parser.add_argument("--name", default="Tests")
    parser.add_argument("--fail-on-skipped", action="store_true",
                        help="exit non-zero if any test was skipped (proves the runner really ran)")
    parser.add_argument("--allow-skip", metavar="REGEX",
                        help="with --fail-on-skipped, tolerate skips whose message matches this "
                             "regular expression (for example the opt-in live checks)")
    args = parser.parse_args(argv)
    result = counts(args.xml)
    print(markdown(args.name, result))
    if result["failures"] or result["errors"]:
        return 1
    if args.fail_on_skipped and args.allow_skip and result["skipped"]:
        unexpected = unexpected_skips(args.xml, args.allow_skip)
        for line in unexpected:
            print(f"unexpected skip: {line}", file=sys.stderr)
        return 1 if unexpected else 0
    if args.fail_on_skipped and result["skipped"]:
        print(f"{result['skipped']} test(s) were skipped", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
