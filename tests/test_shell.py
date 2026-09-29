"""Run the bats suites for the shell tools.

The tools under scripts/ are bash, and bats is what tests bash.
tests/shell/*.bats is one file per tool, and this module runs each one as a
parametrized case so that pytest stays the one command a developer types.  A
missing bats is a skip and not a failure: the shell tests are the only thing
it runs, and there is nothing here to see without it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SUITES = sorted((HERE / "shell").glob("*.bats"))

BATS = shutil.which("bats")


