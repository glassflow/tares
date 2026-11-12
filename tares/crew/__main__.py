"""`python -m tares.crew <args>`: the same as `tares factory <args>`, minus the set-up `tares factory`
does first (the workspace address and key from `tares login`). The environment is os.environ as it is."""
import os
import sys

from .dispatch import run

run(sys.argv[1:], dict(os.environ))
