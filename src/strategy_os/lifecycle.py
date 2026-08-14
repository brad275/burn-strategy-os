"""Explicit project and cycle lifecycle rules."""

from typing import Dict, FrozenSet

from .errors import InvalidTransitionError
from .models import CycleStatus, ProjectStatus


PROJECT_TRANSITIONS: Dict[ProjectStatus, FrozenSet[ProjectStatus]] = {
    ProjectStatus.DRAFT: frozenset({ProjectStatus.RUNNING, ProjectStatus.ARCHIVED}),
    ProjectStatus.RUNNING: frozenset({ProjectStatus.REVIEW, ProjectStatus.ARCHIVED}),
    ProjectStatus.REVIEW: frozenset({ProjectStatus.RUNNING, ProjectStatus.APPROVED, ProjectStatus.ARCHIVED}),
    ProjectStatus.APPROVED: frozenset({ProjectStatus.ARCHIVED}),
    ProjectStatus.ARCHIVED: frozenset(),
}

CYCLE_TRANSITIONS: Dict[CycleStatus, FrozenSet[CycleStatus]] = {
    CycleStatus.QUEUED: frozenset({CycleStatus.RUNNING, CycleStatus.FAILED}),
    CycleStatus.RUNNING: frozenset({CycleStatus.RECONCILING, CycleStatus.FAILED}),
    CycleStatus.RECONCILING: frozenset({CycleStatus.REVIEW, CycleStatus.FAILED}),
    CycleStatus.REVIEW: frozenset({CycleStatus.RUNNING, CycleStatus.COMPLETED, CycleStatus.FAILED}),
    CycleStatus.COMPLETED: frozenset({CycleStatus.RUNNING, CycleStatus.SUPERSEDED}),
    CycleStatus.FAILED: frozenset({CycleStatus.RUNNING}),
    CycleStatus.SUPERSEDED: frozenset(),
}


def require_project_transition(current: ProjectStatus, target: ProjectStatus) -> None:
    if target not in PROJECT_TRANSITIONS[current]:
        raise InvalidTransitionError("project transition %s -> %s is not permitted" % (current.value, target.value))


def require_cycle_transition(current: CycleStatus, target: CycleStatus) -> None:
    if target not in CYCLE_TRANSITIONS[current]:
        raise InvalidTransitionError("cycle transition %s -> %s is not permitted" % (current.value, target.value))
