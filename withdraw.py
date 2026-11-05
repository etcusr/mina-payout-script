#!/usr/bin/env python3
"""Withdraw an exact amount after reserving all known obligations."""
import sys
from payout_cli import main
if __name__=='__main__': raise SystemExit(main(['withdraw','--complete']+sys.argv[1:]))
