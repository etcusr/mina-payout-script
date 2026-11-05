#!/usr/bin/env python3
"""Submit or resume immutable payout plans; uncertainty never permits retry."""
import sys
from payout_cli import main
if __name__=='__main__': raise SystemExit(main(['send','--complete']+sys.argv[1:]))
