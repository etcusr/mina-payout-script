"""Payout redirects: sending a delegator's reward somewhere else.

A reward does not have to go to the address that holds the stake. Two cases
this covers:

  1. You stake from several of your own addresses and want everything landing
     on one, instead of collecting from all of them later.
  2. An account is shared, and the reward has to be split between people.

The share of the pool is still calculated from the STAKING address - only the
destination of the transfer changes.

Configured entirely in config.yml, nothing here needs editing:

    PAYOUT_REDIRECTS:
      # everything to one address
      B62qMyStakingOne...: B62qMyMainWallet...
      B62qMyStakingTwo...: B62qMyMainWallet...

      # split between two people
      B62qSharedAccount...:
        B62qPartner...:      50
        B62qMyMainWallet...: 50

Shares are normalised, so 50/50, 0.5/0.5 and 1/1 all mean the same thing.
"""


def parse(raw, warn=None):
    """Normalise the config block into {src: [(dst, fraction), ...]}.

    Accepts either a bare destination string or a {destination: share} mapping.
    Shares are rescaled to fractions summing to 1.

    `warn` is an optional callable for reporting bad entries.
    """
    parsed = {}
    for src, target in (raw or {}).items():
        src = str(src).strip()
        if not src:
            continue

        if isinstance(target, dict):
            parts = []
            for dst, share in target.items():
                dst = str(dst).strip()
                try:
                    share = float(share)
                except (TypeError, ValueError):
                    continue
                if dst and share > 0:
                    parts.append((dst, share))
            total = sum(s for _, s in parts)
            if not parts or total <= 0:
                if warn:
                    warn(f"redirect for {src} has no valid shares, ignoring")
                continue
            parsed[src] = [(d, s / total) for d, s in parts]
        else:
            dst = str(target).strip()
            if dst:
                parsed[src] = [(dst, 1.0)]
    return parsed


def split(total_amount, targets):
    """Divide `total_amount` across [(dst, fraction), ...].

    The last destination absorbs the remainder, so the parts always add back up
    to the original amount exactly - no nanomina lost or invented to rounding.

    Returns [(dst, amount), ...].
    """
    out, assigned = [], 0.0
    for i, (dst, fraction) in enumerate(targets):
        amount = (total_amount - assigned) if i == len(targets) - 1 \
            else total_amount * fraction
        assigned += amount
        out.append((dst, amount))
    return out


def describe(parsed):
    """Human-readable lines describing the active redirects."""
    lines = []
    for src, targets in parsed.items():
        lines.append(f"{src}")
        for dst, fraction in targets:
            lines.append(f"    -> {dst}  {fraction * 100:g}%")
    return lines
