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
            or
        cat tmux_state.csv | python tmux_state.py restore
"""

import argparse
import contextlib
import csv
import enum
import os

import shlex
import subprocess
import sys
from typing import ClassVar

from pydantic import BaseModel

RECORD = "record"
RESTORE = "restore"

FOREGROUND_COMMAND_INDEX = -1  # selects last child process; ignores suspended jobs
TMUX_LIST_FORMAT_SEP = r"\x1f"  # unit separator char, use instead of something possibly used in the tmux list-panes output


q = shlex.quote


class IOMode(enum.Enum):
    """Enum to handle user-specified IO mode (input or output)"""

    READ_INPUT = "read_input"
    WRITE_OUTPUT = "write_output"


class Pane(BaseModel):
    """Tmux pane info"""

    session: str
    window_index: int
    window_name: str
    pane_id: str  # format is e.g. "%25". The prefix % is stripped in __lt__ for numeric sorting
    cwd: str
    ppid: str
    command: str = ""

    # Maps each model field name (except `command`) to its tmux format token.
    # This is the single source of truth linking Pane fields to tmux output.
    TMUX_FORMAT_TOKENS: ClassVar[dict[str, str]] = {
        "session": "#S",
        "window_index": "#I",
        "window_name": "#W",
        "pane_id": "#D",
        "cwd": "#{pane_current_path}",
        "ppid": "#{pane_pid}",
        # `command` is excluded — added later from ps output
    }

    @classmethod
    def list_command(cls) -> list[str]:
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

    @classmethod
    def from_csv_row(cls, row: list[str]) -> "Pane":
        """Create an instance from a parsed CSV row (list of field values)."""
        names = list(cls.model_fields.keys())
        if len(names) != len(row):
            ValueError("Name and Value lists have different lengths. Quitting")
        return cls.model_validate(dict(zip(names, row)))

    @classmethod
    def from_tmux_row(cls, row: str) -> "Pane":
        """Create an instance from a tmux list-panes output row (no command field)."""
        names = [f for f in cls.model_fields if f in cls.TMUX_FORMAT_TOKENS]
        values = row.split(TMUX_LIST_FORMAT_SEP)
        model_data = dict(zip(names, values))
        model_data["command"] = command_from_ppid(ppid=model_data["ppid"])
        return cls.model_validate(model_data)

    def in_same_window(self, other: "Pane") -> bool:
        """Determine if these two panes are in the same window (to trigger a split)"""
        return self.window_index == other.window_index and self.session == other.session

    @property
    def sort_tuple(self) -> tuple:
        """
        The tuple to sort Pane by: pane ID (int, with leading '%' stripped),
        then window index (int)
        """
        return int(self.pane_id[1:]), int(self.window_index)

    def __lt__(self, other) -> bool:
        """Comparator for sort"""
        return self.sort_tuple < other.sort_tuple


def sort_by_session_appearance_order(panes: list[Pane]) -> list[Pane]:
    """
    Sort panes into the order reported by PREFIX-w navigation tree (preserving
    'tmux list-panes' reported order): order of session creation, then order
    of pane creation:
    """
    session_order = {}
    for i, pane in enumerate(sorted(panes)):
        session_order.setdefault(pane.session, i)
    return sorted(panes, key=lambda p: (session_order[p.session], p))


def get_panes_from_file(lines) -> list[Pane]:
    """Parse Pane objects from an iterable of lines (file, stdin, etc.)."""
    reader = csv.reader(line for line in lines if not line.startswith("#"))
    panes = [Pane.from_csv_row(row) for row in reader if row]
    if not panes:
        raise ValueError("No state found while parsing tmux state")
    return sort_by_session_appearance_order(panes)


def generate_commands(panes: list[Pane]) -> list[str]:
    """Create a set of tmux commands from a list of Pane objects"""
    commands = ["set -e"] + [
        f"tmux has-session -t {q(s)} 2> /dev/null && "
        f'echo "session {q(s)} already exists, quitting" && exit 1'
        for s in set(p.session for p in panes)
    ]

    sessions_created = set()
    for ipane, pane in enumerate(panes):

        # Create a new session if it doesn't already exist and detach. If the
        # session already exists, have add a bash command to a) warn the user,
        # and b) exit before creating any new / duplicate sessions anything.
        #
        # Manually name the first sessions's inital window
        #
        # The pane should be split if its window index is different from
        # the previous pane's (this relies on the Pane class's sorting __lt__).
        # The first pane ever can't be a split pane, and a split pane doesn't
        # rename the window, so an if/else/if/else is used here.
        #
        is_first_pane_in_session = pane.session not in sessions_created
        if is_first_pane_in_session:
            sessions_created.add(pane.session)
            command = f"new-session -s {q(pane.session)} -n {q(pane.window_name)} -d"
        else:
            # If this pane will be part of an existing window, split that
            # window to create the pane. Otherwise, create a new window (which
            # will contain the pane).
            #
            if pane.in_same_window(panes[ipane - 1]):
                command = f"split-window -t {q(pane.i_sw)} -h "
            else:
                command = f"new-window -t {q(pane.session)}: -n {q(pane.window_name)}"

        commands.append(f"tmux {command} -c {q(pane.cwd)}")
        if pane.command:
            commands.append(
                f"tmux send-keys -t {q(pane.i_sw)} -l {q(pane.command)} \\; "
                f"send-keys -t {q(pane.i_sw)} Enter"
            )

    commands.append(f"tmux attach -t {q(panes[0].session)}")

    return commands


def command_from_ppid(ppid: str, command_index: int = FOREGROUND_COMMAND_INDEX) -> str:
    """
    Get the command corresponding to the PPID from #{pane_pid} in the tmux list-p

    Grab only the command_index-th PID output if there are multiple PIDs for this pane,
    e.g. one or more suspended jobs in the pane
    """
    ps_command = ["ps", "-hoargs", "--ppid", ppid]
    ps_output = subprocess.run(ps_command, capture_output=True, check=False, text=True)
    cmds = ps_output.stdout.rstrip().split("\n")
    return cmds[command_index] if cmds else ""


def list_tmux_panes() -> list[Pane]:
    """Record current state into a list of Pane objects"""
    panes = []
    output = subprocess.run(Pane.list_command(), capture_output=True, text=True)
    for row in output.stdout.rstrip().split("\n"):
        pane = Pane.from_tmux_row(row)
        panes.append(pane)
    return panes


@contextlib.contextmanager
def open_input_output(path=None, io_mode: IOMode = IOMode.READ_INPUT):
    """Open either a file or STDIN/STDOUT for input/output"""
    if path is None:
        yield sys.stdin if io_mode == IOMode.READ_INPUT else sys.stdout
    else:
        mode_arg = "r" if io_mode == IOMode.READ_INPUT else "w"
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
    """
    Read the state file and generate the recreate-the-state output, or read
    such output and create a script to make the session.
    """
    args = parse_args()
    state_file = os.path.expanduser(args.state_file) if args.state_file else None

    if args.mode == RECORD:
        panes = list_tmux_panes()
        with open_input_output(state_file, IOMode.WRITE_OUTPUT) as file:
            csv.writer(file).writerows([p.model_dump().values() for p in panes])

    elif args.mode == RESTORE:
        with open_input_output(state_file, IOMode.READ_INPUT) as file:
            panes = get_panes_from_file(file)

        for command in generate_commands(panes):
            print(command)
    else:
        raise ValueError(f"mode {args.mode} not allowed")


if __name__ == "__main__":
    main()
