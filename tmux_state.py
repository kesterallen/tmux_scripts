"""
Mode RECORD:
    Create a tmux state file, recording the current tmux session(s).

Mode RESTORE:
    Read a tmux_state.json file and generate a script to recreate the tmux
    session(s) for which the state files was recorded.

Usage
    Either
        python tmux_state.py record > tmux_state.json
            or
        python tmux_state.py record tmux_state.json
    or
        python tmux_state.py restore tmux_state.json
            or
        cat tmux_state.json | python tmux_state.py restore
"""

import argparse
from collections import defaultdict
import contextlib
import enum
import io
import json
import os
import shlex
import subprocess
import sys
from typing import ClassVar

from pydantic import BaseModel

RECORD = "record"
RESTORE = "restore"

# unit separator char, use instead of something possibly used in the tmux
# list-panes output
TMUX_LIST_FORMAT_SEP = r"\x1f"


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
    pane_pid: str
    processes: list[str] = []

    # Maps each model field name (except .processes) to its tmux format token.
    # This is the single source of truth linking Pane fields to tmux output.
    TMUX_FORMAT_TOKENS: ClassVar[dict[str, str]] = {
        "session": "#S",
        "window_index": "#I",
        "window_name": "#W",
        "pane_id": "#D",
        "cwd": "#{pane_current_path}",
        "pane_pid": "#{pane_pid}",  # "PID of first process in pane" (man tmux)
        # processes is excluded — added later from ps output
    }

    @classmethod
    def tmux_list_panes_command(cls) -> list[str]:
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
    def from_tmux_row(cls, row: str, processes: dict[str: list]) -> "Pane":
        """Create an instance from a tmux list-panes output row."""
        names = [f for f in cls.model_fields if f in cls.TMUX_FORMAT_TOKENS]
        values = row.split(TMUX_LIST_FORMAT_SEP)
        model_data = dict(zip(names, values))
        if model_data["pane_pid"] in processes:
            pane_processes = processes[model_data["pane_pid"]]
            model_data["processes"] = pane_processes
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
    Sort panes into the order of the PREFIX-w navigation tree:
        order of session creation, then
        order of pane creation in that session
    """
    session_order = {}
    for i, pane in enumerate(sorted(panes)):
        session_order.setdefault(pane.session, i)
    return sorted(panes, key=lambda p: (session_order[p.session], p))


def get_panes_from_file(file: io.TextIOWrapper) -> list[Pane]:
    """Create Pane objects from an iterable of lines (file, stdin, etc.)."""
    content = "".join(file)
    raw = json.loads(content)
    panes = [Pane.model_validate(item) for item in raw]
    if not panes:
        raise ValueError("No state found while parsing tmux state")
    return sort_by_session_appearance_order(panes)


def generate_tmux_commands(panes: list[Pane]) -> list[str]:
    """Create a set of tmux commands from a list of Pane objects"""
    # Initially, check that these sessions don't already exist (prevents this
    # script from creating duplicates of running sessions):
    commands = ["set -e"] + [
        f"tmux has-session -t {q(s)} 2> /dev/null && "
        f'echo "session {q(s)} already exists, quitting" && exit 1'
        for s in set(p.session for p in panes)
    ]

    sessions_created = set()
    for ipane, pane in enumerate(panes):

        # If this pane's session doesn't exist yet, create it and manually name
        # its first window.  Detach so future sessions don't nest inside this
        # one.
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
        if pane.processes:
            for process in pane.processes:
                commands.append(
                    f"tmux send-keys -t {q(pane.i_sw)} -l {q(process)} \\; "
                    f"send-keys -t {q(pane.i_sw)} Enter"
                )

    commands.append(f"tmux attach -t {q(panes[0].session)}")

    return commands


def list_processes() -> dict[str: list]:
    """
    Get the commands running in all panes. Map them from pane.pane_pid (from
    #{pane_pid} in the tmux list-p, the pid of the pane's bash session, which
    will be the parent PID -- ppid -- of any commands running in the pane's
    bash session) to STATE/COMMAND.
    """
    # TODO map the pane.pane_id to the dict of processes? would need panes from calling method
    ps_command = ["ps", "-hostat,ppid,args"]
    ps_output = subprocess.run(ps_command, capture_output=True, check=False, text=True)
    pses = [c.split(maxsplit=2) for c in ps_output.stdout.rstrip().split("\n")]

    processes = defaultdict(list)
    for stat, ppid, cmd in pses:
        # Add a "&" suffix to background the command when it's replayed if it's
        # backgrounded here (from stat):
        if "+" not in stat:
            cmd += " &"
        processes[ppid].append(cmd)

    return dict(processes)  # strips defaultdict extras


def list_tmux_panes() -> list[Pane]:
    """
    Record current state from "tmux list-panes" (from
    Pane.tmux_list_panes_command) into a list of Pane objects
    """
    output = subprocess.run(
        Pane.tmux_list_panes_command(), capture_output=True, check=True, text=True
    )
    rows = output.stdout.rstrip().split("\n")
    processes = list_processes()
    panes = [Pane.from_tmux_row(r, processes) for r in rows]
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
            json.dump([p.model_dump() for p in panes], file, indent=4)

    elif args.mode == RESTORE:
        with open_input_output(state_file, IOMode.READ_INPUT) as file:
            panes = get_panes_from_file(file)
        print("\n".join(generate_tmux_commands(panes)))
    else:
        raise ValueError(f"mode {args.mode} not allowed")


if __name__ == "__main__":
    main()
