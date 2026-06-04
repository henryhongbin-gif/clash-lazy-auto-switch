#!/usr/bin/env python3
"""Compatibility wrapper for the old single-file entrypoint.

The maintained implementation lives in `clash_auto_switch`.
Run:

    python3 -m clash_auto_switch.main
"""

from clash_auto_switch.main import main


if __name__ == "__main__":
    raise SystemExit(main())

