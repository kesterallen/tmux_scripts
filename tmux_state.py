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

RECORD = "record"
RESTORE = "restore"

FOREGROUND_COMMAND_INDEX = -1  # selects last child process; ignores suspended jobs
TMUX_LIST_FORMAT_SEP = "|||"


READ_INPUT = "read_input"
WRITE_OUTPUT = "write_output"


class Pane(BaseModel):
    """Tmux pane info"""

    session_creation_time: str  # linux epoch seconds
    session: str
    window_index: int
    pane_index: int
    window_name: str
    cwd: str
    ppid: str
    command: str

    # Maps each model field name (except `command`) to its tmux format token.
    # This is the single source of truth linking Pane fields to tmux output.
    TMUX_FORMAT_TOKENS: ClassVar[dict[str, str]] = {
        "session_creation_time": "#{session_created}",
        "session": "#S",
        "window_index": "#I",
        "pane_index": "#P",
        "window_name": "#W",
        "cwd": "#{pane_current_path}",
        "ppid": "#{pane_pid}",
        # `command` is excluded — added later from ps output
    }

    @classmethod
    def get_list_command(cls) -> list[str]:
        """Build the tmux list-panes command, deriving field order from model_fields."""
        fmt = TMUX_LIST_FORMAT_SEP.join(
            cls.TMUX_FORMAT_TOKENS[f]
            for f in cls.model_fields
            if f in cls.TMUX_FORMAT_TOKENS
        )
        return ["tmux", "list-panes", "-aF", fmt]

    @property
    def i_sw(self) -> str:
        """syntactic sugar for the -t argument"""
        return f"{self.session}:{self.window_index}"

    @property
    def i_swp(self) -> str:
        """syntactic sugar for the -t argument for split panes"""
        return f"{self.session}:{self.window_index}.{self.pane_index}"

    @classmethod
    def from_csv_row(cls, row: str) -> "Pane":
        """Create an instance from a comma-delimited CSV row (saved state file)."""
        names = cls.model_fields.keys()
        values = list(csv.reader([row]))[0]
        command_not_specified = len(names) == len(values) + 1
        if command_not_specified:
            values.append("")
        model_data = {nv[0]: nv[1] for nv in zip(names, values)}
        return cls.model_validate(model_data)

    @classmethod
    def from_tmux_row(cls, row: str) -> "Pane":
        """Create an instance from a tmux list-panes output row (no command field)."""
        names = [f for f in cls.model_fields if f in cls.TMUX_FORMAT_TOKENS]
        values = row.split(TMUX_LIST_FORMAT_SEP)
        model_data = {nv[0]: nv[1] for nv in zip(names, values)}
        model_data["command"] = ""
        return cls.model_validate(model_data)

    def __lt__(self, other) -> bool:
        """Comparator for sort"""
        return (
            int(self.session_creation_time),
            int(self.window_index),
            int(self.pane_index),
        ) < (
            int(other.session_creation_time),
            int(other.window_index),
            int(other.pane_index),
        )


def get_panes_from_file(lines) -> list[Pane]:
    """Parse Pane objects from an iterable of lines (file, stdin, etc.)."""
    pane_rows = [r.rstrip("\n") for r in lines if not r.startswith("#")]
    panes = [Pane.from_csv_row(row) for row in pane_rows if row]
    if not panes:
        raise ValueError("No state found while parsing tmux state")
    return sorted(panes)


def generate_commands(panes: list[Pane]) -> list[str]:
    """Create a set of tmux commands from a list of Pane objects"""
    sessions_created = set()
    commands = []
    for ipane, pane in enumerate(panes):

        # Create a new session and detach (for now) if it doesn't already
        # exist. If it does, have the tmux restore script exit with a warning
        # Manually name the first sessions's inital window
        #
        # The pane should be split if its window index is different from
        # the previous pane's (this relies on the sorting of the panes list).
        # The first pane ever can't be a split pane, and a split pane doesn't
        # rename the window, so an if/else/if/else is used here.
        #
        is_first_pane_in_session = pane.session not in sessions_created
        if is_first_pane_in_session:
            warning = f"session '{pane.session}' already exists, quitting"
            commands.append(
                f'tmux has-session -t "{pane.session}" 2> /dev/null '
                f'&& echo "{warning}" && exit 1'
            )
            sessions_created.add(pane.session)

            # Sleep 1: so that session_creation_time values are different for each
            # session (ensures sessions are sorted by creation time)
            commands.append("sleep 1")
            command = f'tmux new-session -s "{pane.session}" -n "{pane.window_name}" -d'
            t_arg = f'-t "{pane.i_sw}"'
        else:
            # If this pane is in a new window, create that window (this also
            # the pane). If this pane is part of an existing window, split that
            # window to create the pane:
            #
            prev = panes[ipane - 1]
            is_split_pane = (
                pane.window_index == prev.window_index and pane.session == prev.session
            )
            if is_split_pane:
                command = f'tmux split-window -t "{pane.i_sw}" -h '
                t_arg = f'-t "{pane.i_swp}"'
            else:
                command = (
                    f'tmux new-window -t "{pane.session}:" -n "{pane.window_name}"'
                )
                t_arg = f'-t "{pane.i_sw}"'

        commands.append(command + f' -c "{pane.cwd}"')
        if pane.command:
            commands.append(f'tmux send-keys {t_arg} "{pane.command}" C-m')

    commands.append(f'tmux attach -t "{panes[0].session}"')

    return commands


def command_from_ppid(ppid: str, command_index: int = FOREGROUND_COMMAND_INDEX) -> str:
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


def list_tmux_panes() -> list[Pane]:
    """Record current state"""
    panes = []
    output = subprocess.run(Pane.get_list_command(), capture_output=True, text=True)
    for row in output.stdout.rstrip().split("\n"):
        pane = Pane.from_tmux_row(row)
        pane.command = command_from_ppid(ppid=pane.ppid)
        panes.append(pane)
    return panes


@contextlib.contextmanager
def open_input_output(path=None, mode: str = READ_INPUT):
    """Open either a file or STDIN/STDOUT for input/output"""
    if path is None:
        yield sys.stdin if mode == READ_INPUT else sys.stdout
    else:
        mode_arg = "r" if mode == READ_INPUT else "w"
        with open(path, mode=mode_arg, encoding="utf-8") as f:
            yield f


def parse_args() -> argparse.Namespace:
    """read in args: mode / state file"""
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
        help="Record the current tmux state or restore one (default: %(default)s)",
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
        panes = list_tmux_panes()
        with open_input_output(state_file, WRITE_OUTPUT) as file:
            csv.writer(file).writerows([p.model_dump().values() for p in panes])

    elif args.mode == RESTORE:
        with open_input_output(state_file, READ_INPUT) as file:
            panes = get_panes_from_file(file)

        print("set -e")
        for command in generate_commands(panes):
            print(command)
    else:
        raise ValueError(f"mode {args.mode} not allowed")


if __name__ == "__main__":
    main()
