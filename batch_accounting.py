"""Separate durable batch ledger. Mutations require the shared payout wallet lock.

A claim commits ``unknown`` before the sole network submission attempt. No state
transition makes a claimed transaction sendable again. Read bridges never create
or migrate files, and the ordinary payout journal is never written here.
"""
import contextlib
import json
import os
import re
from pathlib import Path
import sqlite3

from payout_calculation import validate_address
from payout_state import StateError, _canonical, _hash, _integer, _text, _private_directory, _sync_directory

FILENAME = 'batch-mainnet.sqlite3'
TERMINAL = ('confirmed', 'failed')
UNSIGNED = ('planned', 'cancelled')


def canonical_plan(plan):
    if not isinstance(plan, dict):
        raise StateError('batch plan must be an object')
    keys = {'wallet', 'network', 'era', 'operation', 'memo', 'fee_nano', 'batch_size', 'recipients'}
    if set(plan) - (keys | {'batches','reserve_advance_nano','payout_plan'}) or not keys <= set(plan):
        raise StateError('batch plan fields differ from canonical schema')
    result = {key: plan[key] for key in keys}
    if 'payout_plan' in plan:
        if not isinstance(plan['payout_plan'], str) or not re.fullmatch(r'[0-9a-f]{64}', plan['payout_plan']):
            raise StateError('invalid linked payout plan digest')
        if 'reserve_advance_nano' in plan:
            raise StateError('linked epoch batch cannot use a reserve advance')
        result['payout_plan'] = plan['payout_plan']
    for key in ('wallet', 'network', 'era', 'operation'):
        _text(result[key], key)
    if not isinstance(result['memo'], str) or len(result['memo'].encode('utf-8')) > 32:
        raise StateError('batch memo must contain at most 32 UTF-8 bytes')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', result['operation']):
        raise StateError('batch operation must contain 1-80 ASCII letters, digits, underscores or hyphens')
    try: validate_address(result['wallet'])
    except ValueError as exc: raise StateError(str(exc)) from exc
    _integer(result['fee_nano'], 'fee_nano', 1)
    _integer(result['batch_size'], 'batch_size', 1)
    if result['batch_size'] > 30:
        raise StateError('batch size cannot exceed 30')
    if not isinstance(result['recipients'], list) or not result['recipients']:
        raise StateError('batch recipients must be a nonempty list')
    seen = {result['wallet']}
    for recipient in result['recipients']:
        if not isinstance(recipient, dict) or set(recipient) != {'recipient', 'amount_nano'}:
            raise StateError('invalid batch recipient')
        try: validate_address(recipient['recipient'])
        except ValueError as exc: raise StateError(str(exc)) from exc
        if recipient['recipient'] in seen:
            raise StateError('batch recipients must be unique and different from sender')
        seen.add(recipient['recipient'])
        _integer(recipient['amount_nano'], 'amount_nano', 1)
    size = result['batch_size']
    result['batches'] = [{'index': i // size, 'recipients': result['recipients'][i:i + size]}
                         for i in range(0, len(result['recipients']), size)]
    cost = sum(row['amount_nano'] for row in result['recipients']) + len(result['batches']) * result['fee_nano']
    if cost >= 2**64:
        raise StateError('batch amount plus fees exceeds UInt64')
    if 'reserve_advance_nano' in plan:
        advance=plan['reserve_advance_nano']
        _integer(advance,'reserve_advance_nano',1)
        if advance>cost:
            raise StateError('reserve advance cannot exceed this operation cost')
        result['reserve_advance_nano']=advance
    if 'batches' in plan and _canonical(plan['batches']) != _canonical(result['batches']):
        raise StateError('batch splitting differs from canonical recipients')
    return json.loads(_canonical(result))


def _intent_hash(digest, index, nonce, signed, tx_hash):
    return _hash(_canonical({'plan': digest, 'index': index, 'nonce': nonce,
                             'signed': signed, 'tx_hash': tx_hash}).encode())


def _matches(proof, expected):
    # Canonical JSON comparison rejects bool/int equality and nested type drift.
    return _canonical({key: proof.get(key) for key in expected}) == _canonical(expected)


def _present(state_dir):
    path = Path(state_dir) / FILENAME
    if Path(state_dir).is_symlink() or path.is_symlink():
        raise StateError('batch state must not be a symbolic link')
    return path.exists()


def _empty():
    return {'spent_nano': 0, 'reserved_nano': 0, 'known_nonces': [], 'unfinished': []}


class BatchStore:
    def __init__(self, state_dir, *, create=True):
        self.root = Path(state_dir)
        self.path = self.root / FILENAME
        if self.root.is_symlink() or self.path.is_symlink():
            raise StateError('batch state must not be a symbolic link')
        if not self.path.exists():
            if not create:
                raise StateError('batch ledger does not exist')
            _private_directory(self.root)
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                with self._connection(write=True) as db:
                    db.executescript('''
                    CREATE TABLE metadata(version INTEGER NOT NULL);
                    INSERT INTO metadata VALUES (1);
                    CREATE TABLE plans(digest TEXT PRIMARY KEY, wallet TEXT NOT NULL,
                      network TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
                      UNIQUE(wallet,network,operation));
                    CREATE TABLE batches(plan_id TEXT NOT NULL REFERENCES plans(digest),
                      batch_index INTEGER NOT NULL, wallet TEXT NOT NULL, network TEXT NOT NULL,
                      status TEXT NOT NULL CHECK(status IN ('planned','prepared','unknown','confirmed','failed','cancelled')),
                      nonce INTEGER, signed TEXT, signed_digest TEXT, tx_hash TEXT, proof TEXT,
                      PRIMARY KEY(plan_id,batch_index), UNIQUE(wallet,network,nonce),
                      UNIQUE(wallet,network,tx_hash));
                    ''')
                _sync_directory(self.root)
        with self._connection() as db:
            versions = db.execute('SELECT version FROM metadata').fetchall()
            if versions != [(1,)]:
                raise StateError('unsupported batch ledger version')

    @contextlib.contextmanager
    def _connection(self, *, write=False):
        db = sqlite3.connect(self.path.resolve().as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=10)
        try:
            if write:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _plan(self, db, digest):
        row = db.execute('SELECT payload,wallet,network,operation FROM plans WHERE digest=?', (digest,)).fetchone()
        if row is None:
            raise StateError('unknown batch plan')
        payload, wallet, network, operation = row
        plan = json.loads(payload)
        if _hash(payload.encode()) != digest or canonical_plan(plan) != plan or (plan['wallet'], plan['network'], plan['operation']) != (wallet, network, operation):
            raise StateError('batch plan checksum or identity mismatch')
        if 'payout_plan' in plan:
            from payout_batch_state import validate_link
            marker = validate_link(self.root, plan)
            if marker is None and db.execute("SELECT 1 FROM batches WHERE plan_id=? AND status<>'planned' LIMIT 1", (digest,)).fetchone():
                raise StateError('started epoch batch is missing its ordinary journal link marker')
            if marker is not None and db.execute('SELECT 1 FROM batches WHERE plan_id=? AND nonce IS NOT NULL AND nonce<>?+batch_index LIMIT 1',
                                                 (digest, marker['initial_nonce'])).fetchone():
                raise StateError('batch nonce differs from its durable ordinary journal link')
        return plan

    def create_plan(self, plan):
        plan = canonical_plan(plan)
        marker = None
        if 'payout_plan' in plan:
            from payout_batch_state import validate_link
            marker = validate_link(self.root, plan)
        payload = _canonical(plan)
        digest = _hash(payload.encode())
        with self._connection(write=True) as db:
            old = db.execute('SELECT digest FROM plans WHERE wallet=? AND network=? AND operation=?',
                             (plan['wallet'], plan['network'], plan['operation'])).fetchone()
            if old:
                if old[0] != digest or self._plan(db, digest) != plan:
                    raise StateError('batch operation already exists with different immutable content')
                return digest
            if marker is not None:
                raise StateError('durably linked batch plan is missing; restore a verified backup instead of recreating it')
            if db.execute("SELECT 1 FROM batches WHERE wallet=? AND status NOT IN ('confirmed','failed','cancelled') LIMIT 1", (plan['wallet'],)).fetchone():
                raise StateError('wallet has an unfinished batch operation')
            db.execute('INSERT INTO plans VALUES (?,?,?,?,?)', (digest, plan['wallet'], plan['network'], plan['operation'], payload))
            db.executemany('INSERT INTO batches(plan_id,batch_index,wallet,network,status) VALUES (?,?,?,?,?)',
                           [(digest, batch['index'], plan['wallet'], plan['network'], 'planned') for batch in plan['batches']])
        return digest

    def get_plan(self, digest):
        with self._connection() as db:
            return self._plan(db, digest)

    def list_plans(self, wallet=None, network=None):
        with self._connection() as db:
            ids = [row[0] for row in db.execute('SELECT digest FROM plans WHERE (? IS NULL OR wallet=?) AND (? IS NULL OR network=?) ORDER BY rowid', (wallet, wallet, network, network))]
            return [(digest, self._plan(db, digest)) for digest in ids]

    def rows(self, digest):
        with self._connection() as db:
            plan = self._plan(db, digest)
            rows = db.execute('SELECT batch_index,status,nonce,signed,signed_digest,tx_hash,proof,wallet,network FROM batches WHERE plan_id=? ORDER BY batch_index', (digest,)).fetchall()
        if [r[0] for r in rows] != list(range(len(plan['batches']))):
            raise StateError('batch ledger rows missing or unexpected')
        result = []
        for index, status, nonce, signed, signed_digest, tx_hash, proof, wallet, network in rows:
            if (wallet, network) != (plan['wallet'], plan['network']):
                raise StateError('batch intent identity mismatch')
            if status not in UNSIGNED and (type(nonce) is not int or not signed or not tx_hash or _intent_hash(digest, index, nonce, signed, tx_hash) != signed_digest):
                raise StateError('batch signed intent checksum mismatch')
            if status in UNSIGNED and any(v is not None for v in (nonce, signed, signed_digest, tx_hash, proof)):
                raise StateError('planned batch unexpectedly has signed intent')
            result.append({'index': index, 'status': status, 'nonce': nonce, 'signed': json.loads(signed) if signed else None,
                           'tx_hash': tx_hash, 'proof': json.loads(proof) if proof else None})
        return result

    def cancel_unsigned_after_failure(self, digest):
        """Release only never-signed suffix after a proven included failure."""
        rows=self.rows(digest)
        self.snapshot(self.get_plan(digest)['wallet'],self.get_plan(digest)['network'])
        failed=[r['index'] for r in rows if r['status']=='failed']
        if not failed:
            raise StateError('cancellation requires a confirmed failed batch')
        with self._connection(write=True) as db:
            db.execute("UPDATE batches SET status='cancelled' WHERE plan_id=? AND batch_index>? AND status='planned' AND nonce IS NULL AND signed IS NULL AND tx_hash IS NULL",(digest,min(failed)))

    def prepare(self, digest, index, nonce, signed, tx_hash):
        _integer(index, 'batch index'); _integer(nonce, 'nonce'); _text(tx_hash, 'tx_hash')
        if isinstance(signed, str):
            try: signed = json.loads(signed)
            except ValueError as exc: raise StateError('signed transaction must be JSON') from exc
        if not isinstance(signed, dict) or not signed:
            raise StateError('signed transaction must be a nonempty object')
        if nonce >= 2**32: raise StateError('batch nonce exceeds UInt32')
        payload = _canonical(signed)
        with self._connection(write=True) as db:
            plan = self._plan(db, digest)
            if 'payout_plan' in plan:
                from payout_batch_state import validate_link
                marker = validate_link(self.root, plan)
                if marker is None:
                    raise StateError('epoch batch must be linked in the ordinary journal before signing')
                if nonce != marker['initial_nonce'] + index:
                    raise StateError('batch nonce differs from its durable ordinary journal link')
            row = db.execute('SELECT status,nonce,signed,tx_hash FROM batches WHERE plan_id=? AND batch_index=?', (digest, index)).fetchone()
            if row is None: raise StateError('unknown batch index')
            if row[0] != 'planned':
                if row[1:] != (nonce, payload, tx_hash): raise StateError('signed batch intent is immutable')
                return
            if db.execute("SELECT 1 FROM batches WHERE plan_id=? AND batch_index<? AND status NOT IN ('confirmed','failed')", (digest, index)).fetchone():
                raise StateError('previous batch must be resolved first')
            if 'payout_plan' in plan and db.execute("SELECT 1 FROM batches WHERE plan_id=? AND batch_index<? AND status='failed'", (digest, index)).fetchone():
                raise StateError('failed epoch batch requires cancellation of the unsigned suffix')
            try:
                db.execute("UPDATE batches SET status='prepared',nonce=?,signed=?,signed_digest=?,tx_hash=? WHERE plan_id=? AND batch_index=?", (nonce, payload, _intent_hash(digest, index, nonce, payload, tx_hash), tx_hash, digest, index))
            except sqlite3.IntegrityError as exc:
                raise StateError('batch nonce already reserved') from exc

    def claim(self, digest, index):
        _integer(index, 'batch index')
        # Validate immutable content before authorizing a network attempt.
        self.rows(digest)
        with self._connection(write=True) as db:
            return db.execute("UPDATE batches SET status='unknown' WHERE plan_id=? AND batch_index=? AND status='prepared'", (digest, index)).rowcount == 1

    def expected(self, digest, index):
        plan = self.get_plan(digest)
        _integer(index, 'batch index')
        rows = self.rows(digest)
        if index >= len(rows): raise StateError('unknown batch index')
        row = rows[index]
        return {**{key: plan[key] for key in ('wallet','network','era','memo','fee_nano')},
                'nonce': row['nonce'], 'tx_hash': row['tx_hash'], 'recipients': plan['batches'][index]['recipients']}

    def observe(self, digest, index, proof, *, required_depth=3):
        expected = self.expected(digest, index)
        _integer(required_depth, 'required_depth', 3)
        if not isinstance(proof, dict): raise StateError('batch proof must be an object')
        status = {'applied': 'confirmed', 'failed': 'failed'}.get(proof.get('status'))
        if status not in TERMINAL: return False
        if not _matches(proof, expected):
            raise StateError('batch proof differs from immutable intent')
        if proof.get('on_best_chain') is not True or type(proof.get('depth')) is not int or proof['depth'] < required_depth:
            raise StateError('batch proof lacks authoritative confirmation depth')
        with self._connection(write=True) as db:
            row = db.execute('SELECT status FROM batches WHERE plan_id=? AND batch_index=?', (digest, index)).fetchone()
            if row[0] in TERMINAL:
                if row[0] != status: raise StateError('batch terminal outcome changed')
                return False
            if row[0] != 'unknown': raise StateError('only a claimed batch can be settled')
            db.execute('UPDATE batches SET status=?,proof=? WHERE plan_id=? AND batch_index=?', (status, _canonical(proof), digest, index))
        return True

    def snapshot(self, wallet, network, *, owner_only=False):
        result = _empty()
        for digest, plan in self.list_plans(wallet, network):
            unfinished = False
            failed_before = False
            for row in self.rows(digest):
                if row['status']=='cancelled':
                    if not failed_before:
                        raise StateError('cancelled batch requires a proven earlier failed batch')
                    continue
                cost = plan['fee_nano'] + sum(r['amount_nano'] for r in plan['batches'][row['index']]['recipients'])
                if row['status'] in TERMINAL:
                    proof = row['proof']
                    if not proof or proof.get('status') != ('applied' if row['status'] == 'confirmed' else 'failed') or not _matches(proof, self.expected(digest, row['index'])) or proof.get('on_best_chain') is not True or type(proof.get('depth')) is not int or proof['depth'] < 3:
                        raise StateError('batch terminal proof is missing or inconsistent')
                    if row['status'] == 'failed':
                        failed_before = True
                    if not owner_only or 'payout_plan' not in plan:
                        result['spent_nano'] += cost if row['status'] == 'confirmed' else plan['fee_nano']
                else:
                    if not owner_only or 'payout_plan' not in plan:
                        result['reserved_nano'] += cost
                    unfinished = True
                if row['nonce'] is not None:
                    result['known_nonces'].append(row['nonce'])
            if unfinished: result['unfinished'].append(digest)
        result['known_nonces'].sort()
        return result


def readonly_snapshot(state_dir, wallet, network, *, owner_only=False):
    if not _present(state_dir): return _empty()
    return BatchStore(state_dir, create=False).snapshot(wallet, network, owner_only=owner_only)


def verify_history(state_dir, config, wallet, network, *, required_depth=3, exclude=None):
    """Read-only fresh proof bridge. Pending operations block ordinary sends."""
    _integer(required_depth, 'required_depth', 3)
    store = BatchStore(state_dir, create=False) if _present(state_dir) else None
    snapshot = store.snapshot(wallet, network) if store else _empty()
    plans = store.list_plans(wallet, network) if store else []
    # A missing batch file must not erase an epoch's settlement obligations.
    # This inventory reads immutable ordinary plans without projecting intents.
    from payout_state import StateStore
    from payout_batch_state import is_batch_plan, linked_digest
    ordinary_path = Path(state_dir) / 'journal.sqlite3'
    ordinary = StateStore(state_dir, readonly=True) if ordinary_path.exists() or ordinary_path.is_symlink() else None
    if ordinary:
        known = {digest for digest, _ in plans}
        for payout_digest, payout in ordinary.list_plans(wallet, network):
            if not is_batch_plan(payout) or not payout['payments']:
                continue
            batch_digest = linked_digest(payout, payout_digest)
            marker = ordinary.batch_link(payout_digest)
            if marker is not None and batch_digest not in known:
                raise StateError('payout is missing its durably linked batch journal; restore a verified backup')
            if exclude in (payout_digest, batch_digest):
                continue
            if ordinary.settlement(payout_digest) is None:
                raise StateError('Предыдущая пакетная выплата не сверена (not settled).')
            if batch_digest not in known:
                raise StateError('settled payout is missing its linked batch journal')
    included = [(digest, plan) for digest, plan in plans
                if digest != exclude and (exclude is None or plan.get('payout_plan') != exclude)]
    if set(snapshot['unfinished']) & {digest for digest, _ in included}:
        raise StateError('Незавершённая пакетная операция блокирует обычные выплаты и вывод.')
    import batch_archive
    for digest, plan in included:
        if 'payout_plan' in plan:
            if ordinary.settlement(plan['payout_plan']) is None:
                raise StateError('Предыдущая пакетная выплата не сверена (not settled).')
        for row in store.rows(digest):
            if row['status']=='cancelled':
                continue
            proof = batch_archive.verify(config, store.expected(digest, row['index']), required_depth=required_depth)
            if not isinstance(proof, dict) or proof.get('status') != ('applied' if row['status'] == 'confirmed' else 'failed') or proof.get('on_best_chain') is not True or type(proof.get('depth')) is not int or proof['depth'] < required_depth:
                raise StateError('Свежая цепочка не подтверждает предыдущую пакетную операцию.')
            if not _matches(proof, store.expected(digest, row['index'])):
                raise StateError('Свежая пакетная квитанция не совпадает с исходной операцией.')
