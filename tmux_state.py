"""
Mode RECORD:
    Create a tmux state file, recording the current tmux session(s).

Mode RESTORE:
    Read a tmux_state.txt file and generate a script to recreate the tmux
    session(s) for which the state files was recorded.

Usage
    Either
        python tmux_state.py record > tmux_state.csv
            or
        python tmux_state.py record tmux_state.csv
    or
        python tmux_state.py restore tmux_state.csv
        cat tmux_state.csv | python tmux_state.py record
"""

import argparse
import contextlib
import csv
import os
import subprocess
import sys
from typing import ClassVar

from pydantic import BaseModel

OUTPUT_COMMAND_INDEX = -1  # ?
SPLIT_CHAR = ";"
RECORD = "record"
RESTORE = "restore"


class Pane(BaseModel):
    """
    Lists of this object should be sorted by session_creation_time, window_index, pane_index
    """
    session_creation_time: str # linux epoch seconds
    session: str
    window_index: int
    pane_index: int
    window_name: str
    cwd: str
    ppid: int
    command: str

    # Class variables to list the panes. Note that the list format
    # (list_command[2]) # must exactly correspond to Pane's instance variables
    # (.sessions, .window_index, etc.) and be in the same order
    list_command: ClassVar[list[str]] = [
        "list-panes",
        "-aF",
        "#{session_created};#S;#I;#P;#W;#{pane_current_path};#{pane_pid}",
    ]
    ppid_index: ClassVar[int] = -1

    @property
    def i_sw(self):
        return f"{self.session}:{self.window_index}"

    @property
    def i_swp(self):
        return f"{self.session}:{self.window_index}.{self.pane_index}"

    @classmethod
    def from_row(cls, row: str) -> "Pane":
        names = Pane.model_fields.keys()
        values = list(csv.reader([row]))[0]
        assert len(names) == len(values)
        model_data = {nv[0]: nv[1] for nv in zip(names, values)}
        return cls.model_validate(model_data)


def get_panes_from_file(lines) -> list:
    """Parse Pane objects from an iterable of lines (file, stdin, etc.)."""
    pane_rows = [r.rstrip("\n") for r in lines if not r.startswith("#")]
    panes = [Pane.from_row(row) for row in pane_rows if row]
    if not panes:
        raise ValueError("No state found while parsing tmux state")
    return sorted(panes, key=lambda p: (p.session_creation_time, int(p.window_index), int(p.pane_index)))


def generate_commands(panes: list[Pane]):
    sessions_created = set()
    commands = []
    for ipane, pane in enumerate(panes):

        # Create a new session and detach (for now) if it doesn't already
        # exist. If it does, have the tmux restore script exit with a warning
        # Manually name the first sessions's inital window
        #
        # The pane should be split if its window index is different from
        # the previous pane's (this relies on the sorting of the panes list).
        # The first pane ever can't be a split pane, and a split pane doesn't rename
        # the window, so an if/else/if/else is used here.
        #
        is_first_pane_in_session = pane.session not in sessions_created
        if is_first_pane_in_session:
            warning = f"session '{pane.session}' already exists, quitting"
            commands.append(f'has-session -t "{pane.session}" 2> /dev/null && echo "{warning}" && exit 1')
            sessions_created.add(pane.session)

            command = f'new-session -s "{pane.session}" -n "{pane.window_name}" -d -c "{pane.cwd}"'
            t_arg = f'-t "{pane.i_sw}"'
        else:
            # If this pane is in a new window, create that window (this also
            # the pane). If this pane is part of an existing window, split that
            # window to create the pane:
            #
            is_split_pane = (
                pane.window_index == panes[ipane - 1].window_index
                and pane.session == panes[ipane - 1].session
            )
            if is_split_pane:
                command = f'split-window -t "{pane.i_sw}" -h -c "{pane.cwd}"'
                t_arg = f'-t "{pane.i_swp}"'
            else:
                command = f'new-window -t "{pane.session}:" -n "{pane.window_name}" -c "{pane.cwd}"'
                t_arg = f'-t "{pane.i_sw}"'

        commands.append(command)
        if pane.command:
            commands.append(f'send-keys {t_arg} "{pane.command}" C-m')

    commands.append(f'attach -t "{panes[0].session}"')

    return commands


def command_from_ppid(ppid: str, command_index: int = OUTPUT_COMMAND_INDEX) -> str:
    """
    Get the command corresponding to the PPID from #{pane_pid} in the tmux list-p

    Grab only the command_index-th PID output if there are multiple PIDs for this pane,
    e.g. one or more suspended jobs in the pane
    """
    ps_command = ["ps", "-hoargs", "--ppid", ppid]
    ps_output = subprocess.run(ps_command, capture_output=True, check=False)
    commands = ps_output.stdout.decode("utf-8").rstrip().split("\n")
    command = commands[command_index]
    return command


def list_tmux_panes() -> list[str]:
    """Record current state"""
    list_panes_cmd = ["tmux"] + Pane.list_command
    list_panes_output = subprocess.run(list_panes_cmd, capture_output=True, check=False)
    list_panes_output_rows = (
        list_panes_output.stdout.decode("utf-8").rstrip().split("\n")
    )
    panes = []
    for pane_row in list_panes_output_rows:
        pane = pane_row.rstrip().split(SPLIT_CHAR)
        pane_command = command_from_ppid(ppid=pane[Pane.ppid_index])
        pane.append(pane_command)
        panes.append(pane)
    return panes


@contextlib.contextmanager
def open_output(path=None):
    """
    Opens either a file or stdout for writing.
    Used to ensure that CSV module usage is concise.
    """
    if path:
        with open(path, "w") as f:
            yield f
    else:
        yield sys.stdout


@contextlib.contextmanager
def open_input(path=None):
    """
    Opens either a file or stdin for reading.
    Used to ensure that CSV module usage is concise.
    """
    if path:
        with open(path, encoding="utf-8") as f:
            yield f
    else:
        yield sys.stdin


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record or restore tmux session state.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "mode",
        choices=[RECORD, RESTORE],
        nargs="?",
        default=RECORD,
        help="Whether to record the current tmux state or restore from a state file (default: %(default)s)",
    )
    parser.add_argument(
        "state_file",
        nargs="?",
        default=None,
        help="Path to the tmux state file (default: stdout for record, stdin for restore)",
    )
    return parser.parse_args()


def main() -> None:
    """Read the state file and generate the recreate-the-state bash script."""
    args = parse_args()
    state_file = os.path.expanduser(args.state_file) if args.state_file else None

    if args.mode == RECORD:
        tmux_panes = list_tmux_panes()
        with open_output(state_file) as file:
            csv.writer(file).writerows(tmux_panes)

    if args.mode == RESTORE:
        with open_input(state_file) as file:
            panes = get_panes_from_file(file)
        print("set -e")
        for command in generate_commands(panes):
            if "new-session" in command:
                print(f"echo starting session: {command}")
                print("sleep 1") # so that session_creation_time values are different for each session (ensures correct sort)
            print(f"tmux {command}")


if __name__ == "__main__":
    main()
