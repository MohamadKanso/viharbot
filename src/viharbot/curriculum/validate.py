"""Traceability and prerequisite validation. Pure functions over data; nothing is fetched."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from .models import Curriculum, Lesson


class ApprovedArtifact(BaseModel):
    """One instructor artifact as a human-approved source knows it (commit and version pinned)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str
    status: str  # only "approved" counts
    commit: str
    version: str


@dataclass(frozen=True)
class Finding:
    # Codes: no_instructor_reference, missing_artifact, not_approved, commit_mismatch,
    # version_mismatch, unknown_prerequisite, self_prerequisite, prerequisite_cycle,
    # duplicate_lesson, unknown_track_lesson.
    code: str
    subject: str
    detail: str


@dataclass(frozen=True)
class Report:
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


def _index(artifacts: Mapping[str, ApprovedArtifact] | Iterable[ApprovedArtifact]
           ) -> Mapping[str, ApprovedArtifact]:
    if isinstance(artifacts, Mapping):
        return artifacts
    return {a.artifact_id: a for a in artifacts}


def validate_traceability(
    lesson: Lesson,
    approved_instructor_artifacts: Mapping[str, ApprovedArtifact] | Iterable[ApprovedArtifact],
) -> Report:
    known = _index(approved_instructor_artifacts)
    out: list[Finding] = []
    sid = lesson.lesson_id
    if not lesson.instructor_references:
        out.append(Finding("no_instructor_reference", sid, "lesson cites no instructor artifact"))
    for ref in lesson.instructor_references:
        art = known.get(ref.artifact_id)
        if art is None:
            out.append(Finding("missing_artifact", sid,
                               f"'{ref.artifact_id}' is not in the instructor artifact set"))
            continue
        if art.status != "approved":
            out.append(Finding("not_approved", sid,
                               f"'{ref.artifact_id}' has status '{art.status}', not 'approved'"))
        if art.commit != ref.commit:
            out.append(Finding("commit_mismatch", sid,
                               f"'{ref.artifact_id}' cites commit {ref.commit[:7]}, "
                               f"approved commit is {art.commit[:7]}"))
        if art.version != ref.version:
            out.append(Finding("version_mismatch", sid,
                               f"'{ref.artifact_id}' cites version '{ref.version}', "
                               f"approved version is '{art.version}'"))
    return Report(tuple(out))


def validate_curriculum_traceability(
    lessons: Iterable[Lesson],
    approved: Mapping[str, ApprovedArtifact] | Iterable[ApprovedArtifact],
) -> Report:
    index = _index(approved)
    findings: list[Finding] = []
    for lesson in lessons:
        findings.extend(validate_traceability(lesson, index).findings)
    return Report(tuple(findings))


def lessons_without_reference(lessons: Iterable[Lesson]) -> tuple[str, ...]:
    return tuple(x.lesson_id for x in lessons if not x.instructor_references)


def validate_prerequisites(lessons: Iterable[Lesson]) -> Report:
    out: list[Finding] = []
    graph: dict[str, tuple[str, ...]] = {}
    for lesson in lessons:
        if lesson.lesson_id in graph:
            out.append(Finding("duplicate_lesson", lesson.lesson_id, "lesson id repeated"))
        graph[lesson.lesson_id] = lesson.prerequisites
    for lid, prereqs in graph.items():
        for p in prereqs:
            if p == lid:
                out.append(Finding("self_prerequisite", lid, "lesson requires itself"))
            elif p not in graph:
                out.append(Finding("unknown_prerequisite", lid, f"unknown prerequisite '{p}'"))
    # Iterative three-colour DFS (1 = on the current path, 2 = done); no recursion limit risk.
    state: dict[str, int] = {}
    for root in graph:
        if state.get(root):
            continue
        state[root] = 1
        path = [root]
        stack = [iter(graph[root])]
        while stack:
            nxt = next(stack[-1], None)
            if nxt is None:
                state[path.pop()] = 2
                stack.pop()
            elif nxt in graph and nxt != path[-1]:
                if state.get(nxt) == 1:
                    cycle = [*path[path.index(nxt):], nxt]
                    out.append(Finding("prerequisite_cycle", nxt, " -> ".join(cycle)))
                elif not state.get(nxt):
                    state[nxt] = 1
                    path.append(nxt)
                    stack.append(iter(graph[nxt]))
    return Report(tuple(out))


def validate_track_lessons(curriculum: Curriculum, lessons: Iterable[Lesson]) -> Report:
    ids = {x.lesson_id for x in lessons}
    return Report(tuple(
        Finding("unknown_track_lesson", t.track_id, f"unknown lesson '{lid}'")
        for t in curriculum.tracks for lid in t.lesson_ids if lid not in ids))
