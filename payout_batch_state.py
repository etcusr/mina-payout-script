"""Read-only bridge from immutable epoch plans to the batch transaction journal.

The batch journal owns transaction nonces. Recipient projections are used only
when reading one epoch, never in the global ordinary-nonce inventory.
"""
import re

from payout_state import StateError, _canonical, _hash, _integer, _validate_plan


def is_batch_plan(plan):
    return plan.get('policy', {}).get('transport', 'payment') == 'zkapp_batch'


def validate_batch_policy(plan):
    policy = plan.get('policy', {})
    if not isinstance(policy, dict):
        raise StateError('payout policy must be an object')
    transport = policy.get('transport', 'payment')
    if transport not in ('payment', 'zkapp_batch'):
        raise StateError('unknown payout transport')
    if transport != 'zkapp_batch':
        return
    if plan.get('kind', 'payout') != 'payout':
        raise StateError('batch transport is only valid for epoch payouts')
    if 'reserve_advance_nano' in plan or 'reserve_advance_nano' in policy:
        raise StateError('epoch batch cannot use a reserve advance')
    size = _integer(policy.get('batch_size'), 'batch_size', 1)
    fee = _integer(policy.get('fee_nano'), 'batch fee_nano', 1)
    if size > 30:
        raise StateError('batch size cannot exceed 30')
    payments = plan['payments']
    addresses = [p['recipient'] for p in payments]
    if addresses != sorted(set(addresses)):
        raise StateError('epoch batch recipients must be unique and sorted')
    if payments and any(p.get('memo', '') != payments[0].get('memo', '') for p in payments):
        raise StateError('epoch batch payments must share one memo')
    for start in range(0, len(payments), size):
        chunk = payments[start:start + size]
        share, remainder = divmod(fee, len(chunk))
        if [p['fee_nano'] for p in chunk] != [share + (i < remainder) for i in range(len(chunk))]:
            raise StateError('epoch batch allocated fees differ from canonical batch fee shares')
    if payments:
        from batch_accounting import canonical_plan
        canonical_plan(dict(_batch_fields(plan), operation='epoch_validation'))


def _batch_fields(plan):
    return {
        **{key: plan[key] for key in ('wallet', 'network', 'era')},
        'memo': plan['payments'][0].get('memo', ''),
        'fee_nano': plan['policy']['fee_nano'], 'batch_size': plan['policy']['batch_size'],
        'recipients': [{key: p[key] for key in ('recipient', 'amount_nano')}
                       for p in plan['payments']],
    }


def batch_plan(plan, digest):
    """Derive the only acceptable batch operation for an ordinary epoch digest."""
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise StateError('invalid payout plan digest')
    if _hash(_validate_plan(plan).encode()) != digest:
        raise StateError('payout plan digest does not match immutable content')
    if not is_batch_plan(plan):
        raise StateError('payout plan does not select batch transport')
    if not plan['payments']:
        raise StateError('empty payout epoch does not need a batch operation')
    from batch_accounting import canonical_plan
    return canonical_plan({
        'payout_plan': digest, 'operation': 'payout_' + digest,
        **_batch_fields(plan),
    })


def linked_digest(plan, digest):
    return _hash(_canonical(batch_plan(plan, digest)).encode())


def validate_link(state_dir, linked):
    """Resolve only immutable ordinary content; never recurse into projections."""
    from payout_state import StateStore
    ordinary = StateStore(state_dir, readonly=True)
    digest = linked['payout_plan']
    expected = batch_plan(ordinary.get_plan(digest), digest)
    if _canonical(linked) != _canonical(expected):
        raise StateError('linked batch differs from immutable payout plan')
    return ordinary.batch_link(digest)


def projected_intents(store, digest, plan=None):
    plan = store.get_plan(digest) if plan is None else plan
    if not is_batch_plan(plan):
        raise StateError('recipient projections require a batch payout plan')
    if not plan['payments']:
        return []
    from batch_accounting import BatchStore, _present
    batch_digest = linked_digest(plan, digest)
    marker = store.batch_link(digest)
    batch_store = BatchStore(store.root, create=False) if _present(store.root) else None
    linked = dict(batch_store.list_plans(plan['wallet'], plan['network'])) if batch_store else {}
    if batch_digest not in linked:
        if marker is not None or store.settlement(digest) is not None:
            raise StateError('payout is missing its durably linked batch journal; restore a verified backup')
        return []
    # Snapshot validates exact receipts before any terminal projections can be
    # accepted for settlement, carry or owner income.
    batch_store.snapshot(plan['wallet'], plan['network'])
    result = []
    for row in batch_store.rows(batch_digest):
        if row['status'] == 'planned':
            continue
        cancelled = row['status'] == 'cancelled'
        status = ('failed' if cancelled else row['status'])
        if status in ('prepared', 'unknown'):
            status = 'unknown'
        evidence = dict(row['proof'] or {},
                        kind='zkapp_batch_cancelled' if cancelled else 'zkapp_batch',
                        batch_plan=batch_digest, batch_index=row['index'])
        if cancelled:
            evidence['status'] = 'cancelled'
        start = row['index'] * plan['policy']['batch_size']
        for offset, original in enumerate(plan['payments'][start:start + plan['policy']['batch_size']]):
            payment_index = start + offset
            payment = dict(original)
            if cancelled:
                payment['fee_nano'] = 0
            result.append({'id': f'batch:{batch_digest}:{payment_index}',
                           'plan_digest': digest, 'payment_index': payment_index,
                           'batch_index': row['index'], 'wallet': plan['wallet'],
                           'network': plan['network'], 'nonce': row['nonce'],
                           'status': status, 'tx_hash': row['tx_hash'],
                           'payment': payment, 'evidence': evidence})
    return result
