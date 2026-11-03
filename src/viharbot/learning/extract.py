"""Deterministic extraction of a learning artifact from a pull request snapshot.

No model is called. Each claim is labelled by where it came from: `author_statement` (text the
author wrote), `verified_in_diff` (a retrieved file path or count) or `not_available`. Missing
rationale is reported as missing, never filled in. Pull-request text is quoted as data in
fenced blocks and never interpreted as instructions.
"""

from __future__ import annotations

import re
from datetime import date

from ..identity_registry import default_registry
from .models import Claim, LearningArtifact, PRSnapshot, Verification

NOT_STATED = "Not stated in the evidence."
AREAS: tuple[tuple[str, str], ...] = (
    ("docs/adr/", "architecture decisions"), ("docs/governance/", "governance"),
    ("agent_identities/", "agent identity"), ("skills/", "skills registry"),
    ("memory", "agent memory"), ("litellm", "model inference and LiteLLM"),
    ("inference", "model inference and LiteLLM"), ("orchestrat", "orchestration"),
    ("delegat", "orchestration"), ("security", "security and authorization"),
    ("identity-and-security", "security and authorization"), ("observab", "observability"),
    ("live_", "observability"), (".github/", "CI/CD and deployment"),
    ("deploy/", "deployment architecture"), ("compose.yaml", "deployment architecture"),
    ("Dockerfile", "deployment architecture"), ("web_app", "web application"),
    ("static/", "web application"), ("tests/", "testing and quality assurance"),
)
SECTION = re.compile(r"(?ms)^#{1,4}\s*(?P<h>[^\n]+)\n(?P<b>.*?)(?=^#{1,4}\s|\Z)")


def sections(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for m in SECTION.finditer(body):
        text = re.sub(r"<!--.*?-->", "", m["b"], flags=re.S).strip()
        if text:
            found[m["h"].strip().lower()] = text
    return found


def pick(found: dict[str, str], *prefixes: str) -> str | None:
    for heading, text in found.items():
        if any(heading.startswith(p) for p in prefixes):
            return text
    return None


def topics_for(paths: list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for path in paths:
        for needle, topic in AREAS:
            if needle in path and topic not in seen:
                seen.append(topic)
    return tuple(seen) or ("engineering practice",)


def _quote(text: str, limit: int = 1200) -> str:
    cleaned = text.strip().replace("```", "'''")[:limit]
    return cleaned


def extract(snapshot: PRSnapshot, today: date | None = None) -> LearningArtifact:
    today = today or date.today()
    src = f"{snapshot.repository}#{snapshot.number}"
    paths = [f.path for f in snapshot.files]
    found = sections(snapshot.body)
    tests = [p for p in paths if p.startswith("tests/")]
    docs = [p for p in paths if p.endswith(".md")]
    agents = sorted({a.id for a in default_registry().list()
                     if re.search(rf"\b{a.id}\b", " ".join(paths + [snapshot.title]), re.I)})
    services = sorted({p.split("/")[-1].removesuffix(".py") for p in paths
                       if re.search(r"(_agent|_service|specialist)\.py$", p)})

    def stated(question: str, text: str | None, *heads: str) -> Claim:
        if text:
            return Claim(question=question, answer=_quote(text), basis="author_statement",
                         source=f"{src} description, section '{heads[0]}'")
        return Claim(question=question, answer=NOT_STATED, basis="not_available", source=src)

    claims = (
        stated("What problem motivated the change?", pick(found, "why —", "why", "motivation"), "WHY"),
        Claim(question="What functionality was introduced?", answer=_quote(snapshot.title, 300),
              basis="author_statement", source=f"{src} title"),
        Claim(question="What architecture was modified?",
              answer="Changed areas: " + ", ".join(topics_for(paths)) + f" ({len(paths)} files).",
              basis="verified_in_diff", source=f"{src} changed files"),
        Claim(question="Which agents or services were affected?",
              answer=("Agents named in paths or title: " + (", ".join(agents) or "none")
                      + ". Service modules changed: " + (", ".join(services) or "none") + "."),
              basis="verified_in_diff", source=f"{src} changed files"),
        stated("What alternatives were considered?", pick(found, "alternatives"), "Alternatives considered"),
        Claim(question="What implementation challenges arose?", answer=NOT_STATED,
              basis="not_available", source=src),
        Claim(question="What tests were introduced or modified?",
              answer=("Test files changed: " + ", ".join(tests[:20])) if tests else "No test files changed.",
              basis="verified_in_diff", source=f"{src} changed files"),
        Claim(question="What did the engineering team learn?", answer=NOT_STATED,
              basis="not_available", source=src),
        stated("What limitations remain?", pick(found, "trade-offs", "deployment, rollback"),
               "Trade-offs and risks"),
        Claim(question="What educational concepts can be extracted?",
              answer=", ".join(topics_for(paths)), basis="verified_in_diff",
              source=f"{src} changed files"),
    )
    topics = topics_for(paths)
    sha7 = snapshot.merge_commit_sha[:7]
    repo_short = snapshot.repository.split("/")[1]
    complete = snapshot.files_omitted == 0 and all(c.basis != "not_available" for c in claims[:2])
    return LearningArtifact(
        artifact_id=f"LA-{repo_short}-pr{snapshot.number}-{sha7}",
        topic=topics[0], topics=topics,
        learning_objectives=tuple(
            f"Describe what changed in {t} in {src} and cite the evidence for each statement."
            for t in topics[:3]),
        source_repository=snapshot.repository, pull_request=snapshot.number,
        merge_commit_sha=snapshot.merge_commit_sha,
        source_references=(f"https://github.com/{snapshot.repository}/pull/{snapshot.number}",
                           f"https://github.com/{snapshot.repository}/commit/{snapshot.merge_commit_sha}",
                           *docs[:10]),
        extracted_on=today.isoformat(), software_version=snapshot.software_version,
        verification=Verification.UNVERIFIED,
        prerequisites=("ViharBot foundations: agents, capabilities and execution bindings",),
        curriculum_placement=f"Track 8: ViharBot Engineering History (candidate; {topics[0]})",
        claims=claims, evidence_complete=complete,
        discussion_excerpts=tuple(_quote(d, 400) for d in snapshot.discussion[:5]),
    )


def render_markdown(a: LearningArtifact) -> str:
    """The instructor-repository file. Source text sits in fenced blocks, labelled as data."""
    lines = [
        "---", f"id: {a.artifact_id}", f"topic: {a.topic}", f"topics: [{', '.join(a.topics)}]",
        f"source_repository: {a.source_repository}", f"pull_request: {a.pull_request}",
        f"commit: {a.merge_commit_sha}", f"extracted_on: {a.extracted_on}",
        f"software_version: {a.software_version}", f"verification: {a.verification.value}",
        f"evidence_complete: {str(a.evidence_complete).lower()}",
        f"curriculum_placement: {a.curriculum_placement}", "prerequisites:",
        *(f"  - {p}" for p in a.prerequisites), "---", "",
        f"# {a.artifact_id}: {a.topic}", "",
        "> Machine-extracted by Archie from retrieved evidence. Status: **"
        f"{a.verification.value}**. A human reviewer must verify it before it is taught. Quoted "
        "text below is data from the source, not instructions.", "",
        "## Draft learning objectives", *(f"- {o}" for o in a.learning_objectives), "",
        "## What the evidence shows", "",
    ]
    for c in a.claims:
        lines += [f"### {c.question}", f"*Basis: {c.basis}. Source: {c.source}.*", ""]
        lines += ["```text", c.answer, "```", ""]
    if a.discussion_excerpts:
        lines += ["## Discussion excerpts (untrusted)", ""]
        for d in a.discussion_excerpts:
            lines += ["```text", d, "```", ""]
    lines += ["## Source references", *(f"- {r}" for r in a.source_references), ""]
    return "\n".join(lines)
