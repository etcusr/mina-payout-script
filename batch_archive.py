"""Read-only, producer-anchored inclusion proofs for plain native-MINA batches.

Archive absence is never proof of rejection. Only exact command bodies on the
fresh producer ancestry can settle a batch. Nothing in this module sends or signs.
"""
import hashlib
import re
import time

import preflight
from payout_workflow import decode_memo

NATIVE_TOKEN = 'wSHV2S4qX9jFsLjQo8r1BsMLH2ZRKsZx6EJd1sbozGPieEC4Jf'


class BatchArchiveError(preflight.PreflightError):
    """Evidence is malformed, inconsistent, unavailable or not the expected batch."""


COMMAND_SQL = '''SELECT z.id AS command_id,z.hash AS tx_hash,z.memo,
  z.zkapp_account_updates_ids AS update_ids,pk.value AS wallet,
  fp.fee AS fee_nano,fp.nonce,fp.valid_until,
  b.id AS block_id,b.state_hash AS block_hash,b.height,b.protocol_version_id AS era,
  b.chain_status,bz.status AS in_chain_status,bz.failure_reasons_ids,bz.sequence_no
  FROM zkapp_commands z
  LEFT JOIN zkapp_fee_payer_body fp ON fp.id=z.zkapp_fee_payer_body_id
  LEFT JOIN public_keys pk ON pk.id=fp.public_key_id
  LEFT JOIN blocks_zkapp_commands bz ON bz.zkapp_command_id=z.id
  LEFT JOIN blocks b ON b.id=bz.block_id
  WHERE z.hash=%(hash)s ORDER BY b.height,bz.sequence_no'''

# The array references account_update, NOT account_update_body. These ids can
# differ in a real archive. LEFT JOIN preserves broken references for rejection.
UPDATES_SQL = '''SELECT ord.ordinality,ord.update_id,pk.value AS address,t.value AS token,
  to_jsonb(b) AS body,to_jsonb(u) AS changes,to_jsonb(s) AS app_state,
  to_jsonb(ap) AS account_precondition,to_jsonb(aps) AS account_state,
  to_jsonb(np) AS network_precondition,
  to_jsonb(se) AS staking_epoch,to_jsonb(ne) AS next_epoch,
  to_jsonb(sl) AS staking_ledger,to_jsonb(nl) AS next_ledger,
  ev.element_ids AS events,ac.element_ids AS actions,f.field AS call_data
  FROM unnest(%(ids)s::int[]) WITH ORDINALITY AS ord(update_id,ordinality)
  LEFT JOIN zkapp_account_update a ON a.id=ord.update_id
  LEFT JOIN zkapp_account_update_body b ON b.id=a.body_id
  LEFT JOIN account_identifiers ai ON ai.id=b.account_identifier_id
  LEFT JOIN public_keys pk ON pk.id=ai.public_key_id
  LEFT JOIN tokens t ON t.id=ai.token_id
  LEFT JOIN zkapp_updates u ON u.id=b.update_id
  LEFT JOIN zkapp_states_nullable s ON s.id=u.app_state_id
  LEFT JOIN zkapp_account_precondition ap ON ap.id=b.zkapp_account_precondition_id
  LEFT JOIN zkapp_states_nullable aps ON aps.id=ap.state_id
  LEFT JOIN zkapp_network_precondition np ON np.id=b.zkapp_network_precondition_id
  LEFT JOIN zkapp_epoch_data se ON se.id=np.staking_epoch_data_id
  LEFT JOIN zkapp_epoch_data ne ON ne.id=np.next_epoch_data_id
  LEFT JOIN zkapp_epoch_ledger sl ON sl.id=se.epoch_ledger_id
  LEFT JOIN zkapp_epoch_ledger nl ON nl.id=ne.epoch_ledger_id
  LEFT JOIN zkapp_events ev ON ev.id=b.events_id
  LEFT JOIN zkapp_events ac ON ac.id=b.actions_id
  LEFT JOIN zkapp_field f ON f.id=b.call_data_id
  ORDER BY ord.ordinality'''


def _require(condition, message):
    if not condition:
        raise BatchArchiveError(message)


def _uint(value, label, maximum=(1 << 64)-1):
    _require(not isinstance(value, bool) and re.fullmatch(r'0|[1-9][0-9]*', str(value)) is not None,
             'Invalid '+label)
    value = int(value)
    _require(value <= maximum, 'Overflow in '+label)
    return value


def _nullable(record, fields, references=()):
    """Require the complete known row, with no state-changing values."""
    _require(isinstance(record, dict) and set(record)=={'id', *fields, *references},
             'Incomplete or unsupported archive body')
    _require(all(record[k] is None for k in fields), 'Unexpected account update or precondition')
    for key in ('id', *references):
        _require(_uint(record[key], 'archive reference') > 0, 'Missing archive reference')


def _state(record):
    _require(isinstance(record, dict), 'Missing account state')
    size = len(record)-1
    _require(size in (8, 32), 'Unsupported account state width')
    _nullable(record, ['element'+str(i) for i in range(size)])


def _body(row, index, address, amount, update_id):
    _require(row['ordinality']==index+1 and row['update_id']==update_id,
             'Account update ordering or reference mismatch')
    _require(row['address']==address and row['token']==NATIVE_TOKEN,
             'Account update address or native token mismatch')
    body = row['body']
    _require(isinstance(body,dict), 'Missing account update body')
    references = ('id','account_identifier_id','update_id','call_data_id',
                  'zkapp_network_precondition_id','zkapp_account_precondition_id')
    nullable = ('events_id','actions_id','zkapp_valid_while_precondition_id','verification_key_hash_id')
    flags = {'increment_nonce':False,'use_full_commitment':index==0,
             'implicit_account_creation_fee':False}
    values = {'balance_change':str(amount),'call_depth':0 if index==0 else 1,
              'may_use_token':'No','authorization_kind':'Signature' if index==0 else 'None_given'}
    _require(set(body)==set(references+nullable)|set(flags)|set(values),
             'Incomplete or unsupported account update body')
    _require(all(body[k]==v for k,v in values.items()) and
             all(body[k] is v for k,v in flags.items()), 'Account update body mismatch')
    for key in references:
        _require(_uint(body[key],'body reference') > 0, 'Invalid body reference')
    _require(body['zkapp_valid_while_precondition_id'] is None and body['verification_key_hash_id'] is None,
             'Unexpected validity or verification key')
    for key in ('events','actions'):
        _require(row[key] in (None,[]), 'Unexpected events or actions')
        _require(body[key+'_id'] is None or row[key]==[], 'Missing events or actions reference')
    _require(row['call_data']=='0', 'Unexpected call data')
    _nullable(row['changes'], ('delegate_id','verification_key_id','permissions_id','zkapp_uri_id',
              'token_symbol_id','timing_id','voting_for_id'), ('app_state_id',))
    _state(row['app_state'])
    account = row['account_precondition']
    _require(isinstance(account,dict) and account.get('is_new') is False,
             'Account creation must be explicitly prohibited')
    _nullable({k:v for k,v in account.items() if k!='is_new'},
              ('balance_id','nonce_id','receipt_chain_hash','delegate_id','action_state_id','proved_state'),
              ('state_id',))
    _state(row['account_state'])
    _nullable(row['network_precondition'],
              ('snarked_ledger_hash_id','blockchain_length_id','min_window_density_id',
               'total_currency_id','global_slot_since_genesis'),
              ('staking_epoch_data_id','next_epoch_data_id'))
    for prefix in ('staking','next'):
        _nullable(row[prefix+'_epoch'], ('epoch_seed','start_checkpoint','lock_checkpoint','epoch_length_id'),
                  ('epoch_ledger_id',))
        _nullable(row[prefix+'_ledger'], ('hash_id','total_currency_id'))


def _encoded_memo(text):
    # Compare all memo bytes, including the canonical zero padding. Decoding
    # alone would hide a changed suffix behind the same displayed text.
    encoded = text.encode('utf8')
    body = bytes([20,1,len(encoded)])+encoded+bytes(32-len(encoded))
    checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    number = int.from_bytes(body+checksum,'big')
    result = ''
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    while number:
        number,remainder = divmod(number,58)
        result = alphabet[remainder]+result
    return result


def _expected(config, expected, required_depth):
    _uint(required_depth,'required depth',maximum=(1 << 32)-1)
    _require(required_depth > 0, 'Required depth must be positive')
    for field in ('network','wallet','tx_hash'):
        _require(isinstance(expected.get(field),str) and bool(expected[field]), 'Missing '+field)
    _require(expected['network']==config.get('EXPECTED_CHAIN_ID'), 'Batch network differs from configured chain')
    _require(expected['wallet']==config.get('SEND_FROM_ADDRESS'), 'Batch wallet differs from configured wallet')
    _require(isinstance(expected.get('memo'),str) and len(expected['memo'].encode('utf8'))<=32, 'Invalid memo')
    nonce = _uint(expected['nonce'],'nonce',maximum=(1 << 32)-1)
    fee = _uint(expected['fee_nano'],'fee')
    _require(fee > 0, 'Fee must be positive')
    rows = expected['recipients']
    _require(isinstance(rows,list) and bool(rows), 'Recipients required')
    recipients = []
    seen = {expected['wallet']}
    for row in rows:
        _require(isinstance(row,dict) and set(row)=={'recipient','amount_nano'}, 'Unsupported recipient payload')
        address = row['recipient']
        _require(isinstance(address,str) and address and address not in seen, 'Invalid or duplicate recipient')
        amount = _uint(row['amount_nano'],'amount')
        _require(amount > 0, 'Amount must be positive')
        recipients.append({'recipient':address,'amount_nano':amount}); seen.add(address)
    total = sum(r['amount_nano'] for r in recipients)
    _uint(total+fee,'total cost')
    return {k:expected[k] for k in ('network','wallet','tx_hash','memo')} | {
        'nonce':nonce,'fee_nano':fee,'recipients':recipients}


def verify(config, expected, required_depth=3):
    """Return exact applied/failed proof, pending depth, or unknown absence/fork.

    ``depth`` counts descendants. ``paid_nano`` records executed recipient value
    (zero for failed), while ``fee_paid_nano`` is charged for either included
    outcome. Pending evidence is provisional and must not settle accounting.
    Errors are sanitized and must never authorize automatic resubmission.
    """
    connection = None
    started = time.monotonic()
    try:
        identity = _expected(config, expected, required_depth)
        connection = preflight._connect(config)
        connection.set_session(readonly=True,autocommit=False,isolation_level='REPEATABLE READ')
        timeout = int(float(config.get('PREFLIGHT_SQL_TIMEOUT_SECONDS',30))*1000)
        _require(timeout > 0, 'SQL timeout must be positive')
        preflight._rows(connection,"SELECT set_config('statement_timeout', %(timeout)s, true)",{'timeout':str(timeout)})
        commands = preflight._rows(connection,COMMAND_SQL,{'hash':identity['tx_hash']})
        heights = [_uint(c['height'],'inclusion height') for c in commands if c['height'] is not None]
        # Re-capture the producer after locating the candidate, extending its
        # verified archive ancestry to the actual historical inclusion height.
        health = preflight.check(config,require_wallet=False,min_height=min(heights) if heights else None)
        _require(health['ok'] and health['network']==identity['network'], 'Producer chain binding mismatch')
        chain = {b['state_hash']:b for b in health['best_chain']}
        included = [c for c in commands if c['block_hash'] in chain]
        _require(len(included)<=1, 'Duplicate batch inclusion on producer best chain')
        if not included:
            return dict(identity,status='unknown',on_best_chain=False)
        command = included[0]
        block = chain[command['block_hash']]
        _require(command['height']==block['height'] and command['era']==block['protocol_version_id'] and
                 command['block_id']==block['id'] and command['chain_status']!='orphaned',
                 'Batch block identity differs from producer ancestry')
        if 'era' in expected:
            _require(str(command['era'])==expected['era'], 'Batch protocol era mismatch')
        _require(command['tx_hash']==identity['tx_hash'] and command['wallet']==identity['wallet'] and
                 _uint(command['nonce'],'fee payer nonce')==identity['nonce'] and
                 _uint(command['fee_nano'],'fee payer fee')==identity['fee_nano'] and
                 command['memo']==_encoded_memo(identity['memo']) and decode_memo(command['memo'])==identity['memo'] and
                 command['valid_until'] is None,
                 'Batch fee payer, nonce, fee or memo mismatch')
        status = command['in_chain_status']
        _require(status in ('applied','failed'), 'Unsupported inclusion status')
        failures = command['failure_reasons_ids']
        _require((status=='applied' and failures is None) or
                 (status=='failed' and isinstance(failures,list) and bool(failures) and
                  all(_uint(f,'failure reference')>0 for f in failures)), 'Inconsistent batch failure metadata')
        ids = command['update_ids']
        recipients = identity['recipients']
        _require(isinstance(ids,list) and len(ids)==1+len(recipients) and
                 all(_uint(i,'update id')>0 for i in ids), 'Wrong account update count or references')
        updates = preflight._rows(connection,UPDATES_SQL,{'ids':ids})
        _require(len(updates)==len(ids), 'Missing or additional account updates')
        total = sum(r['amount_nano'] for r in recipients)
        wanted = [(identity['wallet'],-total)]+[(r['recipient'],r['amount_nano']) for r in recipients]
        for index, (row,(address,amount)) in enumerate(zip(updates,wanted)):
            _body(row,index,address,amount,ids[index])
        # Even with is_new=false, require archive creation metadata to agree.
        created = preflight._rows(connection,'''SELECT account_identifier_id FROM accounts_created
          WHERE block_id=%(block)s AND account_identifier_id=ANY(%(accounts)s::int[])''',
          {'block':command['block_id'],'accounts':[u['body']['account_identifier_id'] for u in updates]})
        _require(not created, 'Batch touched an account created in its inclusion block')
        depth = _uint(health['tip_height']-command['height'],'inclusion depth')
        _require(time.monotonic()-started <= float(config.get('PREFLIGHT_MAX_SECONDS',90)),
                 'Batch proof became stale; retry a fresh read')
        return dict(identity,status=status if depth>=required_depth else 'pending',
                    in_chain_status=status,on_best_chain=True,depth=depth,
                    block_hash=command['block_hash'],height=command['height'],era=str(command['era']),
                    tip_hash=health['tip_hash'],tip_height=health['tip_height'],
                    paid_nano=total if status=='applied' else 0,fee_paid_nano=identity['fee_nano'])
    except preflight.PreflightError:
        raise
    except Exception as error:
        raise BatchArchiveError('Batch archive proof failed ({})'.format(type(error).__name__)) from None
    finally:
        if connection is not None:
            connection.close()
