import json
import os
import unittest
from unittest.mock import patch, MagicMock
from tmux_state import list_tmux_panes, Pane, TMUX_LIST_FORMAT_SEP

pane_fields = ["session", "window_index", "window_name", "pane_id", "cwd", "pane_pid", "processes"]

FIXTURES_PATH = os.path.join(os.path.dirname(__file__), "test_tmux_state_fixtures.json")

class TestTmuxState(unittest.TestCase):
    @patch('tmux_state.list_processes')
    @patch('subprocess.run')
    def test_list_tmux_panes(self, mock_subprocess, mock_list_processes):
        # Mock list_processes to return empty dict (no running processes)
        mock_list_processes.return_value = {}

        # Load exemplar pane data from fixtures file
        with open(FIXTURES_PATH) as f:
            fixture_panes = json.load(f)
        outputs = [[p[field] for field in pane_fields] for p in fixture_panes]
        mock_outputs = [TMUX_LIST_FORMAT_SEP.join([str(e) for e in row]) for row in outputs]

        for i, mock_output in enumerate(mock_outputs):
            mock_subprocess.return_value = MagicMock(stdout=mock_output)

            panes = list_tmux_panes()

            # Validate the parsed panes
            self.assertEqual(len(panes), 1)
            pane = panes[0]
            self.assertIsInstance(pane, Pane)
            for ifield, field in enumerate(pane_fields):
                self.assertEqual(getattr(pane, field), outputs[i][ifield])


if __name__ == '__main__':
    unittest.main()
