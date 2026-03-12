import unittest
from unittest.mock import patch, MagicMock
from tmux_state import list_tmux_panes, Pane

class TestTmuxState(unittest.TestCase):
    @patch('subprocess.run')
    def test_list_tmux_panes(self, mock_subprocess):
        # Mock tmux list-panes output
        mock_output = """session1|||0|||window1|||1|||%123|||/home/user|||12345"""
        mock_subprocess.return_value = MagicMock(stdout=mock_output)

        panes = list_tmux_panes()

        # Validate the parsed panes
        self.assertEqual(len(panes), 1)
        pane = panes[0]
        self.assertIsInstance(pane, Pane)
        self.assertEqual(pane.session, "session1")
        self.assertEqual(pane.window_index, 0)
        self.assertEqual(pane.window_name, "window1")
        self.assertEqual(pane.pane_index, 1)
        self.assertEqual(pane.pane_id, "%123")
        self.assertEqual(pane.cwd, "/home/user")
        self.assertEqual(pane.ppid, "12345")

if __name__ == '__main__':
    unittest.main()