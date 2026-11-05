#!/usr/bin/env python3
"""Calculate exact immutable closed-epoch plans or read-only current previews."""
import sys
from payout_cli import main
if __name__=='__main__': raise SystemExit(main(['plan']+sys.argv[1:]))
