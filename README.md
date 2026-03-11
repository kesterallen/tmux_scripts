# tmux_scripts
Script and cronjobs to record the state of a tmux session(s) and recreate it.

## Usage
Add the crontab line from crontab.txt to your crontab, and then tmux_state.py
can generate a script to recreate a recorded tmux session.

Record tmux state with `python tmux_state.py record` or make a scripts to
restore a set of tmux sessions with `python tmux_state.py restore`.

Set up the cronjob in crontab.txt to do this automatically.
