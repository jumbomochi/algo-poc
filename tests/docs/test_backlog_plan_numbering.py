"""Drift guard for the readiness backlog plan's story identifiers.

The plan doc used to carry its own global `Story N` numbering, which drifted out
of sync with the Jira board (P3-08 / KAN-48). The Jira key is now the identifier
of record and the `P<phase>-<n>` label its readable position. This module parses
the doc and asserts it stays internally consistent: heading format, per-phase
counts, label contiguity, key-set completeness, dependency resolvability, and
heading/map title agreement.

Deliberately a static parse — no network, no Jira credentials — so it runs in CI.
Blocker-set equality against the live Jira board cannot be checked here; that is
verified at authoring time and pasted into the PR as evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOC = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-08-12-readiness-closure-story-backlog.md"
)

HEADING_RE = re.compile(r"^### (P[123]-\d{2}) \((KAN-\d+)\): (.+)$")
MAP_ROW_RE = re.compile(r"^\| (\d+|—) \| (P[123]-\d{2}|—) \| (KAN-\d+) \| (.+) \|$")
LABEL_RE = re.compile(r"\bP[123]-\d{2}\b")
RANGE_RE = re.compile(r"\b(P[123])-(\d{2})\.\.(P[123])-(\d{2})\b")

EXPECTED_PHASE_COUNTS = {"P1": 19, "P2": 16, "P3": 8}
EXPECTED_KEYS = {f"KAN-{n}" for n in range(4, 45)} - {"KAN-45", "KAN-46"} | {
    "KAN-47",
    "KAN-48",
}
ABSENT_KEYS = {"KAN-45", "KAN-46", "KAN-49"}
NEW_ENTRY_KEYS = {"KAN-42", "KAN-43", "KAN-44", "KAN-47", "KAN-48"}


def _expand(text: str) -> set[str]:
    """All P-labels in `text`, with `Pn-aa..Pn-bb` ranges expanded inclusively."""
    labels: set[str] = set()
    for phase, lo, phase_hi, hi in RANGE_RE.findall(text):
        assert phase == phase_hi, f"cross-phase range in {text!r}"
        labels.update(f"{phase}-{n:02d}" for n in range(int(lo), int(hi) + 1))
    labels.update(LABEL_RE.findall(text))
    return labels


class Doc:
    """Parsed view of the plan doc."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines()

        self.headings: list[tuple[int, str, str, str]] = []
        for i, line in enumerate(self.lines):
            m = HEADING_RE.match(line)
            if m:
                self.headings.append((i, *m.groups()))

        self.map_rows = [
            MAP_ROW_RE.match(line).groups()  # type: ignore[union-attr]
            for line in self.lines
            if MAP_ROW_RE.match(line)
        ]

        self.phase_of_heading: dict[str, str] = {}
        phase = None
        for line in self.lines:
            if line.startswith("## Phase "):
                phase = f"P{line.split()[2]}"
            m = HEADING_RE.match(line)
            if m:
                assert phase is not None, f"heading before any phase: {line!r}"
                self.phase_of_heading[m.group(1)] = phase

        self.bodies: dict[str, list[str]] = {}
        starts = [i for i, _, _, _ in self.headings]
        for n, (i, _label, key, _title) in enumerate(self.headings):
            end = starts[n + 1] if n + 1 < len(starts) else len(self.lines)
            body = self.lines[i + 1 : end]
            stop = next(
                (j for j, line in enumerate(body) if line.startswith(("## ", "---"))),
                len(body),
            )
            self.bodies[key] = body[:stop]

        self.graph = self._graph_block()

    def _graph_block(self) -> str:
        anchor = self.lines.index("Dependency graph (P-labels):")
        fences = [
            i for i, line in enumerate(self.lines[anchor:], anchor) if line.strip() == "```"
        ]
        assert len(fences) >= 2, "dependency graph is not a fenced code block"
        return "\n".join(self.lines[fences[0] + 1 : fences[1]])

    def depends_on(self, key: str) -> str:
        for line in self.bodies[key]:
            if "**Depends on:**" in line:
                return line.split("**Depends on:**", 1)[1].split("|")[0].strip()
        raise AssertionError(f"{key} has no **Depends on:** field")


@pytest.fixture(scope="module")
def doc() -> Doc:
    return Doc(DOC.read_text())


def test_every_story_heading_carries_a_plabel_and_a_jira_key(doc: Doc) -> None:
    """AC1: heading format is `### P1-13 (KAN-16): title`, and no `Story N` survives."""
    bad = [
        line
        for line in doc.lines
        if line.startswith("### ") and not HEADING_RE.match(line)
    ]
    assert bad == []
    assert [line for line in doc.lines if line.startswith("### Story ")] == []


def test_phase_counts(doc: Doc) -> None:
    """AC2: 43 stories — 19 in Phase 1, 16 in Phase 2, 8 in Phase 3."""
    counts: dict[str, int] = {}
    for label, phase in doc.phase_of_heading.items():
        assert label.startswith(phase), f"{label} sits under {phase}"
        counts[phase] = counts.get(phase, 0) + 1
    assert counts == EXPECTED_PHASE_COUNTS
    assert len(doc.headings) == sum(EXPECTED_PHASE_COUNTS.values()) == 43


def test_plabels_are_contiguous_and_appear_once_as_heading_and_once_in_the_map(
    doc: Doc,
) -> None:
    """AC3: P1-01..P1-19, P2-01..P2-16, P3-01..P3-08, each used exactly once each place."""
    expected = {
        f"{phase}-{n:02d}"
        for phase, count in EXPECTED_PHASE_COUNTS.items()
        for n in range(1, count + 1)
    }
    heading_labels = [label for _, label, _, _ in doc.headings]
    assert sorted(heading_labels) == sorted(expected)
    assert len(set(heading_labels)) == len(heading_labels), "duplicate heading label"

    map_labels = [label for _, label, _, _ in doc.map_rows if label != "—"]
    assert sorted(map_labels) == sorted(expected)
    assert len(set(map_labels)) == len(map_labels), "duplicate map label"


def test_jira_key_sets(doc: Doc) -> None:
    """AC4: the 43 keys appear once as a heading and once in the map; absent keys map-only."""
    heading_keys = [key for _, _, key, _ in doc.headings]
    assert sorted(heading_keys) == sorted(EXPECTED_KEYS)
    assert len(set(heading_keys)) == len(heading_keys), "duplicate heading key"

    map_keys = [key for _, _, key, _ in doc.map_rows]
    assert sorted(map_keys) == sorted(EXPECTED_KEYS | ABSENT_KEYS)
    for key in ABSENT_KEYS:
        assert key not in heading_keys, f"{key} is absent and must not have a heading"


def test_no_stale_story_numbering_survives(doc: Doc) -> None:
    """AC5: no `Story 13` / `Stories 2, 3` / `Story-19` references anywhere."""
    stale = re.findall(r".{0,30}Stor(?:y|ies)[ -]\d.{0,30}", doc.text)
    assert stale == []


def test_depends_on_values_are_resolvable_plabel_lists(doc: Doc) -> None:
    """AC6: every `Depends on:` is `—` or P-labels that each resolve to a heading."""
    known = {label for _, label, _, _ in doc.headings}
    for _, label, key, _ in doc.headings:
        value = doc.depends_on(key)
        if value == "—":
            continue
        for token in value.split(","):
            token = token.strip()
            assert re.fullmatch(
                r"P[123]-\d{2}(\.\.P[123]-\d{2})?", token
            ), f"{label} ({key}) has a non-P-label dependency: {token!r}"
        deps = _expand(value)
        assert deps <= known, f"{label} ({key}) depends on unknown {sorted(deps - known)}"
        assert label not in deps, f"{label} depends on itself"


def test_identifier_map_has_a_row_per_story_plus_the_absent_keys(doc: Doc) -> None:
    """AC8: 46 body rows — 43 stories (5 with `—` for the old number) + 3 absent keys."""
    assert len(doc.map_rows) == 46
    jira_only = [old for old, label, _, _ in doc.map_rows if label != "—" and old == "—"]
    assert len(jira_only) == 5
    old_numbers = sorted(int(old) for old, _, _, _ in doc.map_rows if old != "—")
    assert old_numbers == list(range(1, 39))


@pytest.mark.parametrize("key", sorted(NEW_ENTRY_KEYS))
def test_new_entries_carry_the_required_fields(doc: Doc, key: str) -> None:
    """AC9: the five Jira-only stories carry Size, Lane, Depends on, Source, Files, ACs."""
    assert key in doc.bodies, f"{key} has no story heading in the doc"
    body = "\n".join(doc.bodies[key])
    for field in ("**Size:**", "**Lane:**", "**Depends on:**", "**Source:**", "**Files:**"):
        assert field in body, f"{key} is missing {field}"
    files_block = body.split("**Files:**", 1)[1]
    assert files_block.lstrip().startswith("-"), f"{key} has no Files list"
    assert len(re.findall(r"^- \[ \] ", body, re.M)) >= 2, f"{key} has < 2 criteria"


def test_every_referenced_plabel_resolves_to_a_heading(doc: Doc) -> None:
    """AC10a: no dangling P-label anywhere in the doc."""
    known = {label for _, label, _, _ in doc.headings}
    referenced = _expand(doc.text)
    assert referenced <= known, f"dangling P-labels: {sorted(referenced - known)}"


def test_dependency_graph_covers_every_story(doc: Doc) -> None:
    """AC10b: every heading's P-label appears in the dependency-graph block."""
    known = {label for _, label, _, _ in doc.headings}
    missing = known - _expand(doc.graph)
    assert missing == set(), f"absent from the dependency graph: {sorted(missing)}"


def test_heading_titles_match_the_identifier_map(doc: Doc) -> None:
    """AC10c: each heading's title is byte-identical to that key's map title."""
    map_titles = {key: title for _, _, key, title in doc.map_rows}
    unmapped = [key for _, _, key, _ in doc.headings if key not in map_titles]
    assert unmapped == [], f"headings with no identifier-map row: {unmapped}"
    mismatched = {
        key: (title, map_titles[key])
        for _, _, key, title in doc.headings
        if title != map_titles[key]
    }
    assert mismatched == {}
