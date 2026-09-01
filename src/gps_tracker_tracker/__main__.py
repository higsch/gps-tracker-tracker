"""Allow `python -m gps_tracker_tracker`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
