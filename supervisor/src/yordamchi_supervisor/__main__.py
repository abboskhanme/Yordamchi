"""`python -I -m yordamchi_supervisor` (the LaunchAgent's ProgramArguments, SPEC §9.1), or `… --selftest`."""

import sys

from yordamchi_supervisor.main import main

sys.exit(main(argv=sys.argv[1:]))
