"""Fresh, read-only health evidence for payout operations.

No cached node or database state is used. The authoritative tip is always the
producer's captured best chain; archive height alone never selects a branch.
"""
import json
import re
import time
import urllib.error
import urllib.request


class PreflightError(RuntimeError):
    pass


class PreflightConnectionError(PreflightError):
    """Transport unavailable; a later fresh proof read may be retried."""


def _integer(value, label):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        raise PreflightError('Invalid ' + label)
    return int(value)


def _graphql(config, query, variables=None):
    url = '{}://{}:{}/graphql'.format(config.get('GRAPHQL_SCHEME', 'http'),
        config.get('GRAPHQL_HOST', '127.0.0.1'), config.get('GRAPHQL_PORT', 3085))
    payload = {'query': query}
    if variables: payload['variables'] = variables
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                    headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=float(config.get('PREFLIGHT_TIMEOUT_SECONDS', 30))) as response:
        body = json.load(response)
    if body.get('errors') or not isinstance(body.get('data'), dict):
        raise PreflightError('Daemon returned invalid GraphQL health evidence')
    return body['data']


def _connect(config):
    import psycopg2
    return psycopg2.connect(config['ARCHIVE_DB_URL'], connect_timeout=int(config.get('PREFLIGHT_CONNECT_TIMEOUT_SECONDS', 10)))


def _rows(connection, query, params=None):
    # RealDictCursor is selected lazily so command help and local fakes need no PG driver.
    try:
        import psycopg2.extras
        kwargs = {'cursor_factory': psycopg2.extras.RealDictCursor}
    except ImportError:
        kwargs = {}
    with connection.cursor(**kwargs) as cursor:
        cursor.execute(query, params or {})
        return cursor.fetchall()


NODE_QUERY = '''{ syncStatus daemonStatus { chainId blockProductionKeys blockchainLength
  consensusConfiguration { k slotsPerEpoch } }
  bestChain(maxLength: 32) { stateHash protocolState { consensusState { blockchainLength epoch } } } }'''
ACCOUNT_QUERY = '''query($publicKey: PublicKey!) {
  account(publicKey: $publicKey) { balance { total liquid } nonce inferredNonce }
  pooledUserCommands(publicKey: $publicKey) { id hash nonce from to amount fee memo isDelegation }
}'''


def check(config, epoch=None, require_wallet=False, *, min_height=None):
    """Return exact, bounded health evidence or raise a sanitized PreflightError.

    Caller must refresh immediately before wallet mutation. ``best_chain`` is the
    archive ancestry captured at ``tip_hash``, ordered tip first; depths count
    descendants. ``epoch_first_height`` is an era-qualified query floor.
    """
    started = time.monotonic()
    connection = None
    try:
        expected = config.get('EXPECTED_CHAIN_ID')
        validator = config.get('VALIDATOR_ADDRESS')
        if not expected or not validator or not config.get('ARCHIVE_DB_URL'):
            raise PreflightError('Configure EXPECTED_CHAIN_ID, VALIDATOR_ADDRESS and ARCHIVE_DB_URL before health checks')
        if epoch is not None: epoch = _integer(epoch, 'epoch')
        node = _graphql(config, NODE_QUERY)
        status = node['daemonStatus']
        if node.get('syncStatus') != 'SYNCED': raise PreflightError('Producer is not SYNCED')
        if status.get('chainId') != expected: raise PreflightError('Producer chain ID does not match EXPECTED_CHAIN_ID')
        keys = status.get('blockProductionKeys') or []
        if validator not in keys: raise PreflightError('Expected validator is not a producer block-production key')
        snapshot = node.get('bestChain') or []
        if not snapshot: raise PreflightError('Producer best chain is empty')
        tip = max(snapshot, key=lambda b: _integer(b['protocolState']['consensusState']['blockchainLength'], 'tip height'))
        tip_hash = tip['stateHash']
        consensus = tip['protocolState']['consensusState']
        tip_height = _integer(consensus['blockchainLength'], 'tip height')
        if min_height is not None:
            min_height=_integer(min_height,'historical proof floor')
            if not 1<=min_height<=tip_height: raise PreflightError('Historical proof floor is outside producer chain')
        current_epoch = _integer(consensus['epoch'], 'current epoch')
        spe = _integer(status['consensusConfiguration']['slotsPerEpoch'], 'slots per epoch')
        if not spe: raise PreflightError('Slots per epoch must be positive')
        if config.get('SLOTS_PER_EPOCH') and _integer(config['SLOTS_PER_EPOCH'], 'configured slots per epoch') != spe:
            raise PreflightError('Configured slots per epoch disagree with producer')
        connection = _connect(config)
        connection.set_session(readonly=True, autocommit=False, isolation_level='REPEATABLE READ')
        timeout = int(float(config.get('PREFLIGHT_SQL_TIMEOUT_SECONDS', 30))*1000)
        if timeout <= 0: raise PreflightError('SQL timeout must be positive')
        _rows(connection, "SELECT set_config('statement_timeout', %(timeout)s, true)", {'timeout': str(timeout)})
        # Scan the whole canonical history, not only the payout epoch. Aggregate
        # checks bound returned data while statement_timeout bounds server work.
        audits = _rows(connection, '''SELECT protocol_version_id AS era, MIN(height) AS first_height,
          MAX(height) AS last_height, COUNT(*) AS n, COUNT(DISTINCT height) AS unique_heights
          FROM blocks WHERE chain_status='canonical' GROUP BY protocol_version_id''')
        if not audits: raise PreflightError('Archive has no canonical history; complete archive verification is required')
        for audit in audits:
            if audit['n'] != audit['unique_heights']: raise PreflightError('Archive has duplicate canonical heights')
            if audit['unique_heights'] != audit['last_height']-audit['first_height']+1:
                raise PreflightError('Archive canonical history has missing heights')
        issues = _rows(connection, '''SELECT COUNT(*) AS n FROM blocks b LEFT JOIN blocks p ON p.id=b.parent_id
          WHERE b.chain_status='canonical' AND b.height>1 AND
          (p.id IS NULL OR p.height<>b.height-1 OR p.state_hash<>b.parent_hash OR p.chain_status<>'canonical' OR
           (p.protocol_version_id=b.protocol_version_id AND b.global_slot_since_hard_fork<=p.global_slot_since_hard_fork))''')[0]['n']
        if issues: raise PreflightError('Archive canonical history has broken parent links or status')
        duplicates = _rows(connection, '''SELECT COUNT(*) AS n FROM
          (SELECT state_hash FROM blocks GROUP BY state_hash HAVING COUNT(*)>1) duplicate_hashes''')[0]['n']
        if duplicates: raise PreflightError('Archive has duplicate state hashes')
        stale = _rows(connection, '''SELECT COUNT(*) AS n FROM blocks b JOIN
          (SELECT protocol_version_id,MAX(height) AS h FROM blocks WHERE chain_status='canonical'
           GROUP BY protocol_version_id) c ON c.protocol_version_id=b.protocol_version_id
          WHERE b.chain_status='pending' AND b.height<=c.h''')[0]['n']
        if stale: raise PreflightError('Archive has unresolved pending blocks below its canonical frontier')
        head = _rows(connection, 'SELECT * FROM blocks WHERE state_hash=%(hash)s', {'hash': tip_hash})
        if len(head) != 1 or head[0]['height'] != tip_height or head[0]['chain_status'] == 'orphaned':
            raise PreflightError('Producer best tip is missing, mismatched or orphaned in archive; retry after archive catches up')
        if head[0]['global_slot_since_hard_fork']//spe != current_epoch:
            raise PreflightError('Producer epoch disagrees with archive tip slot')
        era = head[0]['protocol_version_id']
        if config.get('PROTOCOL_VERSION_ID') and int(config['PROTOCOL_VERSION_ID']) != era:
            raise PreflightError('Configured protocol era disagrees with live producer')
        era_start = _rows(connection, 'SELECT MIN(height) AS h FROM blocks WHERE protocol_version_id=%(era)s', {'era': era})[0]['h']
        finality_k = _integer(status['consensusConfiguration']['k'], 'producer finality K')
        if not finality_k: raise PreflightError('Producer finality K must be positive')
        allowance = _integer(config.get('PREFLIGHT_CANONICAL_LAG_ALLOWANCE_BLOCKS', 0), 'canonical lag allowance')
        current_audit = next((a for a in audits if a['era']==era), None)
        # MIN(height) alone cannot distinguish a fresh era from truncation.
        # Require a slot-zero boundary linked into verified older canonical
        # history, and include that parent in the authoritative ancestry walk.
        if current_audit is None:
            boundary = _rows(connection, '''SELECT b.global_slot_since_hard_fork AS slot,
              b.parent_hash AS expected_parent_hash,p.state_hash AS parent_hash,
              p.height AS parent_height,p.protocol_version_id AS parent_era,p.chain_status AS parent_status
              FROM blocks b LEFT JOIN blocks p ON p.id=b.parent_id
              WHERE b.protocol_version_id=%(era)s AND b.height=%(height)s''',
              {'era': era, 'height': era_start})
            if (era_start <= 1 or len(boundary)!=1 or boundary[0]['slot']!=0
                or boundary[0]['parent_height']!=era_start-1 or boundary[0]['parent_era']==era
                or boundary[0]['parent_status']!='canonical'
                or boundary[0]['expected_parent_hash']!=boundary[0]['parent_hash']):
                raise PreflightError('Fresh protocol era boundary is unverified; restore complete canonical archive history')
        # Once the verified new era ages past K, absent/stalled classification
        # fails independently from the lower payout-confirmation policy.
        frontier = current_audit['last_height'] if current_audit else era_start-1
        canonical_lag = tip_height-frontier
        if canonical_lag < 0 or canonical_lag > finality_k+allowance:
            raise PreflightError('Archive canonical frontier is stalled beyond producer finality K; inspect archive classification')
        epoch_first = None
        if epoch is not None:
            epoch_first = _rows(connection, '''SELECT MIN(height) AS h FROM blocks
              WHERE protocol_version_id=%(era)s AND global_slot_since_hard_fork >= %(start)s
              AND global_slot_since_hard_fork < %(end)s''', {'era': era, 'start': epoch*spe, 'end': (epoch+1)*spe})[0]['h']
            if epoch_first is None: raise PreflightError('Target epoch is absent from the current archive protocol era')
        floor = max(1, (epoch_first-1) if epoch_first is not None else (era_start-1 if current_audit is None else frontier))
        if min_height is not None: floor=min(floor,min_height)
        chain = _rows(connection, '''WITH RECURSIVE ancestry AS (
          SELECT id,state_hash,parent_id,parent_hash,height,protocol_version_id,chain_status,
                 global_slot_since_hard_fork,0 AS depth FROM blocks WHERE state_hash=%(hash)s
          UNION ALL
          SELECT p.id,p.state_hash,p.parent_id,p.parent_hash,p.height,p.protocol_version_id,p.chain_status,
                 p.global_slot_since_hard_fork,a.depth+1 FROM blocks p JOIN ancestry a ON p.id=a.parent_id
          WHERE a.height>%(floor)s AND a.depth<%(max_depth)s)
          SELECT * FROM ancestry ORDER BY depth''', {'hash': tip_hash, 'floor': floor, 'max_depth': tip_height-floor+1})
        if not chain or chain[-1]['height'] != floor:
            raise PreflightError('Archive best-chain ancestry is incomplete')
        for child, parent in zip(chain, chain[1:]):
            if child['height'] != parent['height']+1 or child['parent_hash'] != parent['state_hash'] or parent['chain_status'] == 'orphaned' or (child['protocol_version_id']==parent['protocol_version_id'] and child['global_slot_since_hard_fork']<=parent['global_slot_since_hard_fork']):
                raise PreflightError('Archive best-chain ancestry has broken parent links or status')
        by_hash = {b['state_hash']: b for b in chain}
        # Snapshot may include older blocks below the epoch-specific floor;
        # validate their archive membership independently without assuming order.
        for block in snapshot:
            rows = _rows(connection, 'SELECT height,chain_status FROM blocks WHERE state_hash=%(hash)s', {'hash': block['stateHash']})
            height = _integer(block['protocolState']['consensusState']['blockchainLength'], 'snapshot height')
            if len(rows)!=1 or rows[0]['height']!=height or rows[0]['chain_status']=='orphaned':
                raise PreflightError('Producer chain snapshot is missing or inconsistent in archive')
            if height >= floor and block['stateHash'] not in by_hash:
                raise PreflightError('Producer snapshot does not match archive best-chain ancestry')
        if epoch is not None:
            epoch_blocks = [b for b in chain if b['protocol_version_id']==era and b['global_slot_since_hard_fork']//spe==epoch]
            if not epoch_blocks: raise PreflightError('Target epoch has no blocks on producer best chain')
            epoch_first = min(b['height'] for b in epoch_blocks)
            if require_wallet and epoch >= current_epoch:
                raise PreflightError('Payout epoch is not closed on the producer best chain')
        account, pending = None, 0
        if require_wallet:
            wallet = config.get('SEND_FROM_ADDRESS')
            if not wallet: raise PreflightError('SEND_FROM_ADDRESS is required')
            data = _graphql(config, ACCOUNT_QUERY, {'publicKey': wallet})
            a = data.get('account')
            if not a: raise PreflightError('Payout account is absent from producer')
            account = {'balance_nano': _integer(a['balance']['total'], 'account balance'),
                       'liquid_nano': _integer(a['balance']['liquid'], 'liquid balance'),
                       'nonce': _integer(a['nonce'], 'account nonce'),
                       'inferred_nonce': _integer(a['inferredNonce'], 'inferred nonce')}
            outgoing = [p for p in data['pooledUserCommands'] if p['from']==wallet]
            nonces = []
            for payment in outgoing:
                nonces.append(_integer(payment['nonce'], 'mempool nonce'))
                pending += _integer(payment['fee'], 'mempool fee')
                if payment['isDelegation'] is not True: pending += _integer(payment['amount'], 'mempool amount')
            nonce, inferred = account['nonce'], account['inferred_nonce']
            if inferred < nonce or sorted(nonces) != list(range(nonce, inferred)):
                raise PreflightError('Account nonce and outgoing mempool disagree; pending commitments are uncertain')
        elapsed = time.monotonic()-started
        if elapsed > float(config.get('PREFLIGHT_MAX_SECONDS', 90)):
            raise PreflightError('Health snapshot became stale; retry before wallet mutation')
        return {'ok': True, 'network': expected, 'era': era, 'tip_hash': tip_hash, 'checked_epoch': epoch,
                'tip_height': tip_height, 'current_epoch': current_epoch, 'slots_per_epoch': spe,
                'producer_keys': list(keys), 'finality_k': finality_k,
                'canonical_frontier_height': current_audit['last_height'] if current_audit else None,
                'canonical_lag_blocks': canonical_lag, 'best_chain': chain, 'epoch_first_height': epoch_first,
                'epoch_closed': epoch is not None and epoch < current_epoch,
                'account': account, 'mempool_pending_nano': pending,
                'pooled_commands': outgoing if require_wallet else [], 'elapsed_seconds': elapsed}
    except PreflightError:
        raise
    except (urllib.error.URLError,TimeoutError,ConnectionError) as error:
        # HTTP responses and invalid evidence are not connection outages.
        # Never expose endpoint URLs or the driver's underlying error text.
        if isinstance(error,urllib.error.HTTPError):
            raise PreflightError('Daemon HTTP health request failed; verify service configuration') from None
        raise PreflightConnectionError('Связь с нодой прервана; свежая проверка не завершена.') from None
    except Exception as error:
        # DSNs, URLs and underlying driver diagnostics may contain credentials.
        raise PreflightError('Health check failed ({}); verify daemon/archive connectivity and schema'.format(type(error).__name__)) from None
    finally:
        if connection is not None: connection.close()
