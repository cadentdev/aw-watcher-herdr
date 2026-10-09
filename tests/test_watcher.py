import json
import os
import tempfile
import unittest
from unittest import mock

from aw_watcher_herdr import watcher

FIXTURE = os.path.join(os.path.dirname(__file__), "snapshot.json")


def load_snapshot():
    with open(FIXTURE) as f:
        return json.load(f)["result"]["snapshot"]


class ProjectNameTest(unittest.TestCase):
    def test_uses_git_root_name(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "widget")
            os.makedirs(os.path.join(repo, ".git"))
            os.makedirs(os.path.join(repo, "docs", "api"))
            self.assertEqual(watcher.project_name(os.path.join(repo, "docs", "api")), "widget")

    def test_falls_back_to_directory_name(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(watcher.project_name(root), os.path.basename(root))


@mock.patch.object(watcher, "project_name", side_effect=lambda cwd: os.path.basename(cwd))
class SnapshotParsingTest(unittest.TestCase):
    def test_focus_data_for_agent_pane(self, _):
        self.assertEqual(
            watcher.focus_data(load_snapshot()),
            {
                "workspace": "work",
                "tab": "agents",
                "cwd": "/home/me/src/widget/docs",
                "project": "docs",
                "agent": "claude",
                "title": "Write the docs",
            },
        )

    def test_focus_data_for_plain_pane(self, _):
        snap = load_snapshot()
        snap["focused_pane_id"] = "w1:p1"
        self.assertEqual(
            watcher.focus_data(snap),
            {"workspace": "work", "tab": "shell", "cwd": "/tmp", "project": "tmp"},
        )

    def test_focus_data_without_focus(self, _):
        snap = load_snapshot()
        snap["focused_pane_id"] = None
        self.assertIsNone(watcher.focus_data(snap))

    def test_active_agents_skips_idle(self, _):
        active = watcher.active_agents(load_snapshot())
        self.assertEqual(list(active), ["w1:p3"])
        self.assertEqual(active["w1:p3"]["agent"], "codex")
        self.assertEqual(active["w1:p3"]["status"], "working")
        self.assertEqual(active["w1:p3"]["title"], "Fix flaky test")


class SpanTrackerTest(unittest.TestCase):
    A = {"agent": "claude", "status": "working"}
    B = {"agent": "claude", "status": "blocked"}

    def test_span_closes_when_state_changes(self):
        t = watcher.SpanTracker()
        self.assertEqual(t.update(0, {"p": self.A}), [])
        self.assertEqual(t.update(5, {"p": self.A}), [])
        closed = t.update(10, {"p": self.B})
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["duration"], 10)
        self.assertEqual(closed[0]["data"], self.A)
        self.assertEqual(t.flush(12)[0]["data"], self.B)

    def test_span_closes_when_agent_stops(self):
        t = watcher.SpanTracker()
        t.update(0, {"p": self.A, "q": self.B})
        closed = t.update(5, {"q": self.B})
        self.assertEqual([e["data"] for e in closed], [self.A])

    def test_long_spans_are_split(self):
        t = watcher.SpanTracker(max_span=60)
        t.update(0, {"p": self.A})
        closed = t.update(60, {"p": self.A})
        self.assertEqual(closed[0]["duration"], 60)
        self.assertIn("p", t.open)
        self.assertEqual(t.open["p"][0], 60)


if __name__ == "__main__":
    unittest.main()
