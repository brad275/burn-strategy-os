import unittest

from strategy_os import CycleStatus, InvalidTransitionError, ProjectStatus
from strategy_os.lifecycle import require_cycle_transition, require_project_transition


class LifecycleTests(unittest.TestCase):
    def test_valid_cycle_path(self):
        path = [
            CycleStatus.QUEUED,
            CycleStatus.RUNNING,
            CycleStatus.RECONCILING,
            CycleStatus.REVIEW,
            CycleStatus.COMPLETED,
            CycleStatus.SUPERSEDED,
        ]
        for current, target in zip(path, path[1:]):
            require_cycle_transition(current, target)

    def test_completed_cycle_cannot_run_again(self):
        with self.assertRaises(InvalidTransitionError):
            require_cycle_transition(CycleStatus.COMPLETED, CycleStatus.RUNNING)

    def test_approved_project_cannot_return_to_draft(self):
        with self.assertRaises(InvalidTransitionError):
            require_project_transition(ProjectStatus.APPROVED, ProjectStatus.DRAFT)


if __name__ == "__main__":
    unittest.main()
