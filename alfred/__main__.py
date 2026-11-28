"""`python -m alfred ...`, as an old LaunchAgent or launcher runs it: the `consiglieri` command.  # rename-compat"""

import sys

from consiglieri.cli import main

if __name__ == "__main__":
    sys.exit(main())
