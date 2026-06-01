"""Regression checks for task priority defaults."""

from __future__ import annotations

import unittest

from core.task import PRIORITY_IDLE, PRIORITY_NORMAL, PRIORITY_URGENT, Task
from core.task_types.task_agent import Task_agent


class TestTaskPriorityDefaults(unittest.TestCase):
    """Validate the default priority scale used by queued tasks."""

    def test_priority_constants_keep_normal_aligned_with_queue_state(self) -> None:
        """Keeps default task priority aligned with queue-state default while preserving an urgent tier."""
        self.assertEqual(PRIORITY_URGENT, 0)
        self.assertEqual(PRIORITY_NORMAL, 5)
        self.assertEqual(PRIORITY_IDLE, 20)

    def test_base_task_defaults_to_normal_priority(self) -> None:
        """Creates tasks with the aligned normal default priority."""
        self.assertEqual(Task().priority, 5)
        self.assertEqual(Task_agent(payload={}, stream=False).priority, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)