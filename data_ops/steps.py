"""Per-version schema steps. Additive changes need no step.

Kinds: rename, retype, split, derive, default, drop.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Step:
    kind: str  # rename | retype | split | derive | default | drop
    table: str
    source: str = ""
    target: str = ""
    note: str = ""


@dataclass
class VersionSpec:
    version: str
    steps: list[Step] = field(default_factory=list)


# Registry keyed by the version the BACKUP was written under.
# When loading an older file into FORMAT_VERSION, apply every spec newer than
# the file's version, in order.
REGISTRY: list[VersionSpec] = [
    VersionSpec(
        version="2026-04",
        steps=[
            # Example derive already performed historically: account.account_status from is_active.
            Step(
                kind="derive",
                table="account",
                source="is_active",
                target="account_status",
                note="account_status = 'active' if is_active else 'inactive'",
            ),
        ],
    ),
]


def steps_from(source_version: str, target_version: str) -> list[Step]:
    """Steps needed to bring source_version data up to target_version."""
    applying = False
    out: list[Step] = []
    for spec in REGISTRY:
        if spec.version == source_version:
            applying = True
            continue
        if applying:
            out.extend(spec.steps)
        if spec.version == target_version:
            break
    return out


def covered_unknown_columns(steps: list[Step]) -> set[tuple[str, str]]:
    """(table, column) pairs a step is allowed to consume/drop/rename away."""
    covered = set()
    for s in steps:
        if s.kind in ("rename", "drop", "split") and s.source:
            covered.add((s.table, s.source))
        if s.kind == "retype" and s.target:
            covered.add((s.table, s.target))
    return covered
