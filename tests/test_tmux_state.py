import unittest
from unittest.mock import patch, MagicMock
from tmux_state import list_tmux_panes, Pane, TMUX_LIST_FORMAT_SEP

pane_fields = ["session", "window_index", "window_name", "pane_id", "cwd", "ppid"]

class TestTmuxState(unittest.TestCase):
    @patch('subprocess.run')
    def test_list_tmux_panes(self, mock_subprocess):
        # Mock tmux list-panes output
        outputs = [
            [ "session1", 0, "window1", "%123", "/home/user", "12345",],
            [ "session1", 1, "window1", "%124", "/home/user", "12346",],
        ]
        mock_outputs = [ TMUX_LIST_FORMAT_SEP.join([str(e) for e in row]) for row in outputs ]

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
