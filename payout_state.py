"""Private, durable payout journal. This module never submits transactions.

Wallet operations must hold wallet_lock across preflight, intent, external
mutation, receipt, and ledger reconciliation. SQLite transactions additionally
serialize reservations and enforce nonce and plan-row uniqueness.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal


class StateError(ValueError):
    pass


class WalletLocked(StateError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _json_default(value):
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError('unsupported value in durable state')


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False, default=_json_default)
    except (TypeError, ValueError) as exc:
        raise StateError('state must be canonical JSON without floats or nonfinite values') from exc


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise StateError('state directory must not be a symbolic link')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _claim_directory(path):
    """Exclusively own a new destination before any write or cleanup."""
    path = Path(path)
    path.mkdir(mode=0o700, exist_ok=False)
    _sync_directory(path.parent)
    return path


def _sync_directory(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path, value):
    """Replace a JSON file using fsync + rename; never expose a truncated file."""
    path = Path(path)
    payload = (_canonical(value) + '\n').encode('utf-8')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def wallet_lock(root, wallet, *, timeout=0):
    """Process-wide advisory wallet lock; all write commands share this root."""
    if not isinstance(wallet, str) or not wallet:
        raise StateError('wallet identity is required')
    root = _private_directory(root)
    directory = _private_directory(root / 'locks')
    filename = _hash(wallet.encode('utf-8')) + '.lock'
    fd = os.open(directory / filename, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise WalletLocked('wallet is locked by another operation') from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise StateError(label + ' must be an integer >= ' + str(minimum))
    return value


def _text(value, label):
    if not isinstance(value, str) or not value:
        raise StateError(label + ' is required')
    return value


def _validate_plan(plan):
    if not isinstance(plan, dict):
        raise StateError('plan must be an object')
    for key in ('wallet', 'network', 'era'):
        _text(plan.get(key), key)
    _integer(plan.get('epoch'), 'epoch')
    kind = plan.get('kind', 'payout')
    if kind not in ('payout', 'withdrawal'):
        raise StateError('plan kind must be payout or withdrawal')
    if kind == 'withdrawal':
        try:
            uuid.UUID(_text(plan.get('operation_id'), 'withdrawal operation_id'))
        except (ValueError, TypeError, AttributeError) as exc:
            raise StateError('withdrawal operation_id must be a UUID string') from exc
    _integer(plan.get('min_confirmations', 20), 'min_confirmations', 1)
    if not isinstance(plan.get('payments'), list) or (kind == 'withdrawal' and not plan['payments']):
        raise StateError('plan requires payments')
    for payment in plan['payments']:
        if not isinstance(payment, dict):
            raise StateError('invalid plan payment')
        _text(payment.get('recipient'), 'recipient')
        _integer(payment.get('amount_nano'), 'amount_nano', 1)
        _integer(payment.get('fee_nano'), 'fee_nano')
        if not isinstance(payment.get('memo', ''), str):
            raise StateError('memo must be text')
    if 'valid_until_slot' in plan:
        _integer(plan['valid_until_slot'], 'valid_until_slot')
    # Float metadata can change fingerprints across languages; require exact inputs.
    def reject_float(value):
        if isinstance(value, float):
            raise StateError('plan must use exact decimal strings, never floats')
        if isinstance(value, dict):
            for item in value.values():
                reject_float(item)
        if isinstance(value, list):
            for item in value:
                reject_float(item)
    reject_float(plan)
    return _canonical(plan)


_SCHEMA = '''
CREATE TABLE metadata (version INTEGER NOT NULL);
INSERT INTO metadata VALUES (2);
CREATE TABLE plans (digest TEXT PRIMARY KEY, payload TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE accounting_runs (
 wallet TEXT NOT NULL, network TEXT NOT NULL, era TEXT NOT NULL, kind TEXT NOT NULL, operation_key TEXT NOT NULL,
 plan_digest TEXT NOT NULL REFERENCES plans(digest), PRIMARY KEY(wallet,network,era,kind,operation_key));
CREATE TABLE intents (
 id INTEGER PRIMARY KEY, plan_digest TEXT NOT NULL REFERENCES plans(digest),
 payment_index INTEGER NOT NULL, wallet TEXT NOT NULL, network TEXT NOT NULL, nonce INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('reserved','unknown','submitted','confirmed','failed')),
 tx_hash TEXT, evidence TEXT, note TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
 UNIQUE(plan_digest,payment_index), UNIQUE(network,wallet,nonce));
CREATE TABLE events (id INTEGER PRIMARY KEY, intent_id INTEGER NOT NULL REFERENCES intents(id),
 status TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE settlements (plan_digest TEXT PRIMARY KEY REFERENCES plans(digest), balances TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE historical (digest TEXT PRIMARY KEY, source_name TEXT NOT NULL,
 payload TEXT NOT NULL, metadata TEXT NOT NULL, created TEXT NOT NULL);
'''


class StateStore:
    """Journal opener. create=True authorizes first initialization only."""
    def __init__(self, root, *, create=False):
        self.root = Path(root)
        self.path = self.root / 'journal.sqlite3'
        if not self.path.exists() and not create:
            raise StateError('journal is missing; initialize explicitly or restore a verified backup')
        _private_directory(self.root)
        fresh = False
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                fresh = True
            except FileExistsError:
                pass
        if self.path.is_symlink():
            raise StateError('journal must not be a symbolic link')
        os.chmod(self.path, 0o600)
        try:
            with self._connection() as connection:
                if fresh:
                    connection.executescript(_SCHEMA)
                    _sync_directory(self.root)
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise StateError('journal integrity check failed')
                version = connection.execute('SELECT version FROM metadata').fetchall()
                if len(version) != 1 or version[0][0] not in (2,3):
                    raise StateError('unsupported journal version')
                for table in ('plans', 'accounting_runs', 'intents', 'events', 'historical', 'settlements'):
                    connection.execute('SELECT * FROM ' + table + ' LIMIT 0')
                if version[0][0]==2:
                    from owner_income import TABLES
                    connection.execute('BEGIN IMMEDIATE')
                    for statement in TABLES: connection.execute(statement)
                    connection.execute('UPDATE metadata SET version=3')
                for table in ('owner_accounts','owner_earnings','owner_withdrawals'):
                    connection.execute('SELECT * FROM '+table+' LIMIT 0')
        except sqlite3.Error as exc:
            raise StateError('journal is corrupt or incomplete; restore a verified backup') from exc

    @contextlib.contextmanager
    def _connection(self):
        connection = sqlite3.connect('file:' + str(self.path.resolve()) + '?mode=rw', uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA journal_mode=DELETE')
            with connection:
                yield connection
        finally:
            connection.close()

    def create_plan(self, plan):
        payload = _validate_plan(plan)
        digest = _hash(payload.encode('utf-8'))
        with self._connection() as connection:
            connection.execute('INSERT OR IGNORE INTO plans VALUES (?,?,?)', (digest, payload, _now()))
        self.get_plan(digest)
        return digest

    def get_plan(self, digest):
        with self._connection() as connection:
            row = connection.execute('SELECT payload FROM plans WHERE digest=?', (digest,)).fetchone()
        if row is None:
            raise StateError('unknown payout plan')
        if _hash(row['payload'].encode('utf-8')) != digest:
            raise StateError('immutable plan checksum mismatch')
        try:
            result = json.loads(row['payload'])
            _validate_plan(result)
            return result
        except (ValueError, TypeError) as exc:
            raise StateError('invalid immutable plan') from exc

    def list_plans(self, wallet=None, network=None):
        with self._connection() as connection:
            digests = [r[0] for r in connection.execute('SELECT digest FROM plans ORDER BY created,digest')]
        return [(d, p) for d in digests for p in [self.get_plan(d)]
                if (wallet is None or p['wallet'] == wallet) and (network is None or p['network'] == network)]

    def bind_plan(self, digest):
        """Bind even a dust-only accounting round, before any external effect."""
        plan = self.get_plan(digest)
        kind = plan.get('kind', 'payout')
        key = str(plan['epoch']) if kind == 'payout' else uuid.UUID(plan['operation_id']).hex
        args = (plan['wallet'], plan['network'], plan['era'], kind, key)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT OR IGNORE INTO accounting_runs VALUES (?,?,?,?,?,?)', args + (digest,))
            actual = connection.execute('SELECT plan_digest FROM accounting_runs WHERE wallet=? AND network=? AND era=? AND kind=? AND operation_key=?', args).fetchone()[0]
            if actual != digest:
                raise StateError('accounting epoch is already bound to a different immutable plan')

    def settlement(self, digest):
        with self._connection() as connection:
            row = connection.execute('SELECT balances FROM settlements WHERE plan_digest=?', (digest,)).fetchone()
        return json.loads(row[0]) if row else None

    def settle(self, digest, balances):
        """Persist resulting signed carry; caller proves fresh chain under wallet lock."""
        if not isinstance(balances, dict) or any(not isinstance(a, str) or not a or type(v) is not int for a,v in balances.items()):
            raise StateError('settlement requires integer nanomina balances')
        plan = self.get_plan(digest)
        self.bind_plan(digest)
        intents = self.list_intents(plan_digest=digest)
        if len(intents) != len(plan['payments']) or any(i['status'] not in ('confirmed','failed') for i in intents):
            raise StateError('cannot settle unresolved payments')
        payload = _canonical(balances)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            old = connection.execute('SELECT balances FROM settlements WHERE plan_digest=?', (digest,)).fetchone()
            if old and old[0] != payload:
                raise StateError('settlement is immutable')
            connection.execute('INSERT OR IGNORE INTO settlements VALUES (?,?,?)', (digest,payload,_now()))
        return balances

    def settled_plans(self, wallet, network):
        """Settlements in durable accounting order, including immutable plans."""
        with self._connection() as connection:
            digests = [r[0] for r in connection.execute('SELECT plan_digest FROM settlements ORDER BY rowid')]
        return [(d,p,self.settlement(d)) for d in digests for p in [self.get_plan(d)]
                if p['wallet']==wallet and p['network']==network]

    def carry_balances(self, wallet, network):
        with self._connection() as connection:
            rows = connection.execute('SELECT s.balances,p.payload FROM settlements s JOIN plans p ON p.digest=s.plan_digest ORDER BY s.rowid DESC').fetchall()
        for row in rows:
            plan = json.loads(row['payload'])
            if plan['wallet'] == wallet and plan['network'] == network and plan.get('kind','payout') == 'payout':
                return json.loads(row['balances'])
        return {}

    def reserve_intent(self, plan_digest, payment_index, nonce):
        plan = self.get_plan(plan_digest)
        _integer(payment_index, 'payment_index')
        _integer(nonce, 'nonce')
        if payment_index >= len(plan['payments']):
            raise StateError('payment index outside plan')
        at = _now()
        try:
            with self._connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                if connection.execute("""SELECT 1 FROM intents WHERE wallet=? AND network=?
                        AND (status='unknown' OR (status='reserved' AND plan_digest<>?)) LIMIT 1""",
                                      (plan['wallet'], plan['network'], plan_digest)).fetchone():
                    raise StateError('wallet has an uncertain or unsubmitted operation; reconcile before reserving another plan')
                kind = plan.get('kind', 'payout')
                operation_key = str(plan['epoch']) if kind == 'payout' else uuid.UUID(plan['operation_id']).hex
                run_key = (plan['wallet'], plan['network'], plan['era'], kind, operation_key)
                connection.execute('INSERT OR IGNORE INTO accounting_runs VALUES (?,?,?,?,?,?)', run_key + (plan_digest,))
                run = connection.execute('SELECT plan_digest FROM accounting_runs WHERE wallet=? AND network=? AND era=? AND kind=? AND operation_key=?',
                                         run_key).fetchone()
                if run['plan_digest'] != plan_digest:
                    raise StateError('accounting epoch is already bound to a different immutable plan')
                cursor = connection.execute('''INSERT INTO intents
                    (plan_digest,payment_index,wallet,network,nonce,status,created,updated)
                    VALUES (?,?,?,?,?,'reserved',?,?)''',
                    (plan_digest, payment_index, plan['wallet'], plan['network'], nonce, at, at))
                identity = cursor.lastrowid
                connection.execute('INSERT INTO events(intent_id,status,detail,created) VALUES (?,?,?,?)',
                                   (identity, 'reserved', '{}', at))
        except sqlite3.IntegrityError as exc:
            raise StateError('payment already reserved or wallet nonce already used; reconcile without retry') from exc
        return self.get_intent(identity)

    def get_intent(self, identity):
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM intents WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise StateError('unknown payment intent')
        result = dict(row)
        result['evidence'] = json.loads(result['evidence']) if result['evidence'] else None
        plan = self.get_plan(result['plan_digest'])
        result['payment'] = plan['payments'][result['payment_index']]
        return result

    def list_intents(self, wallet=None, plan_digest=None, network=None):
        conditions, args = [], []
        if wallet is not None:
            conditions.append('wallet=?')
            args.append(wallet)
        if network is not None:
            conditions.append('network=?')
            args.append(network)
        if plan_digest is not None:
            conditions.append('plan_digest=?')
            args.append(plan_digest)
        query = 'SELECT id FROM intents' + (' WHERE ' + ' AND '.join(conditions) if conditions else '') + ' ORDER BY id'
        with self._connection() as connection:
            identities = [row[0] for row in connection.execute(query, args)]
        return [self.get_intent(identity) for identity in identities]

    def pending_commitments(self, wallet, *, network=None):
        return sum(intent['payment']['amount_nano'] + intent['payment']['fee_nano']
                   for intent in self.list_intents(wallet=wallet, network=network)
                   if intent['status'] in ('reserved', 'unknown', 'submitted'))

    def _transition(self, identity, status, allowed, *, tx_hash=None, evidence=None, note=None):
        at = _now()
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT status,tx_hash FROM intents WHERE id=?', (identity,)).fetchone()
            if row is None or row['status'] not in allowed:
                raise StateError('invalid intent status transition')
            if tx_hash and row['tx_hash'] and tx_hash != row['tx_hash']:
                raise StateError('transaction identity cannot change')
            connection.execute('''UPDATE intents SET status=?,tx_hash=COALESCE(?,tx_hash),
                evidence=COALESCE(?,evidence),note=COALESCE(?,note),updated=? WHERE id=?''',
                (status, tx_hash, _canonical(evidence) if evidence is not None else None, note, at, identity))
            detail = {'tx_hash': tx_hash, 'evidence': evidence, 'note': note}
            connection.execute('INSERT INTO events(intent_id,status,detail,created) VALUES (?,?,?,?)',
                               (identity, status, _canonical(detail), at))
        return self.get_intent(identity)

    def mark_unknown(self, identity, note):
        _text(note, 'uncertainty reason')
        return self._transition(identity, 'unknown', ('reserved', 'submitted', 'unknown'), note=note)

    def mark_submitted(self, identity, tx_hash):
        _text(tx_hash, 'transaction hash')
        return self._transition(identity, 'submitted', ('reserved', 'unknown', 'submitted'), tx_hash=tx_hash)

    def _proof(self, identity, evidence, *, required_depth=None):
        intent = self.get_intent(identity)
        plan = self.get_plan(intent['plan_digest'])
        if not isinstance(evidence, dict):
            raise StateError('structured chain proof is required')
        for key in ('wallet', 'network', 'era'):
            if evidence.get(key) != plan[key]:
                raise StateError('chain proof identity mismatch: ' + key)
        if type(evidence.get('nonce')) is not int or evidence['nonce'] != intent['nonce']:
            raise StateError('chain proof nonce mismatch')
        if evidence.get('status') != 'applied':
            raise StateError('proof must establish successfully applied transaction')
        if evidence.get('on_best_chain') is not True:
            raise StateError('authoritative best-chain ancestry proof is required')
        _text(evidence.get('block_hash'), 'proof block hash')
        threshold = plan.get('min_confirmations', 20) if required_depth is None else required_depth
        _integer(threshold, 'required confirmation depth', 1)
        _integer(evidence.get('depth'), 'confirmation depth', threshold)
        return intent, plan

    def mark_confirmed(self, identity, evidence, *, required_depth=None):
        intent, plan = self._proof(identity, evidence, required_depth=required_depth)
        _text(evidence.get('tx_hash'), 'transaction hash')
        if evidence['tx_hash'] != intent['tx_hash']:
            raise StateError('confirmation hash does not match submitted receipt')
        for key in ('recipient', 'amount_nano', 'fee_nano', 'memo'):
            if evidence.get(key, '' if key == 'memo' else None) != intent['payment'].get(key, ''):
                raise StateError('confirmation payment identity mismatch: ' + key)
        for key in ('amount_nano', 'fee_nano'):
            _integer(evidence[key], key)
        # Explicit operator policy may change for an existing immutable plan.
        # Preserve the plan and record the actual threshold in durable evidence.
        proof = dict(evidence, required_depth=plan.get('min_confirmations',20) if required_depth is None else required_depth)
        return self._transition(identity, 'confirmed', ('submitted',), evidence=proof)

    def resolve_failed(self, identity, evidence):
        """Release commitment only with a different confirmed payment at this nonce.

        This does not enable resubmission: the plan row and nonce stay reserved.
        Network proof must be independently obtained and validated by the caller.
        """
        intent, plan = self._proof(identity, evidence)
        kind = evidence.get('kind')
        if kind != 'nonce_consumed':
            raise StateError('terminal proof must establish a different applied payment consumed the nonce')
        _text(evidence.get('tx_hash'), 'replacement transaction hash')
        _text(evidence.get('recipient'), 'replacement recipient')
        _integer(evidence.get('amount_nano'), 'replacement amount')
        _integer(evidence.get('fee_nano'), 'replacement fee')
        if not isinstance(evidence.get('memo'), str):
            raise StateError('replacement memo is required')
        if evidence['tx_hash'] == intent['tx_hash']:
            raise StateError('same transaction cannot prove failure')
        if all(evidence[key] == intent['payment'].get(key, '')
               for key in ('recipient', 'amount_nano', 'fee_nano', 'memo')):
            raise StateError('matching payment may be the original transaction with a lost receipt')
        return self._transition(identity, 'failed', ('reserved', 'unknown', 'submitted'), evidence=evidence)

    def import_history(self, source, metadata):
        """Copy a historical artifact for status only; never create plans/intents."""
        source = Path(source)
        payload = source.read_bytes().decode('utf-8')
        try:
            json.loads(payload)
        except ValueError as exc:
            raise StateError('historical artifact is not valid JSON') from exc
        if not isinstance(metadata, dict):
            raise StateError('historical identity metadata is required')
        for key in ('network', 'era'):
            _text(metadata.get(key), key)
        _integer(metadata.get('epoch'), 'epoch')
        digest = _hash(payload.encode('utf-8'))
        with self._connection() as connection:
            existing = connection.execute('SELECT metadata FROM historical WHERE digest=?', (digest,)).fetchone()
            if existing and existing[0] != _canonical(metadata):
                raise StateError('historical artifact already imported with different identity')
            connection.execute('INSERT OR IGNORE INTO historical VALUES (?,?,?,?,?)',
                               (digest, source.name, payload, _canonical(metadata), _now()))
        return digest

    def historical_records(self):
        with self._connection() as connection:
            records = [dict(row) for row in connection.execute('SELECT * FROM historical ORDER BY created')]
        for record in records:
            if _hash(record['payload'].encode('utf-8')) != record['digest']:
                raise StateError('historical artifact checksum mismatch')
            record['metadata'] = json.loads(record['metadata'])
        return records

    def backup(self, destination, *, extra_files=()):
        """Consistent SQLite snapshot plus selected private files and SHA256 manifest.

        Hold the wallet lock across this operation when including the ledger so
        both snapshots describe the same accounting boundary.
        """
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(destination)
        files = [Path(path) for path in extra_files]
        names = [path.name for path in files]
        if len(set(names)) != len(names) or any(name in ('journal.sqlite3', 'manifest.json') for name in names):
            raise StateError('backup filenames must be unique')
        if any(not path.is_file() or path.is_symlink() for path in files):
            raise StateError('backup inputs must be regular files')
        _claim_directory(destination)
        try:
            with self._connection() as source:
                target = sqlite3.connect(str(destination / 'journal.sqlite3'))
                try:
                    source.backup(target)
                finally:
                    target.close()
            for path in files:
                shutil.copyfile(path, destination / path.name)
            manifest = {'version': 1, 'created': _now(), 'files': {}}
            for path in destination.iterdir():
                os.chmod(path, 0o600)
                with path.open('rb') as handle:
                    os.fsync(handle.fileno())
                manifest['files'][path.name] = _hash(path.read_bytes())
            atomic_write_json(destination / 'manifest.json', manifest)
            _sync_directory(destination)
            return destination
        except BaseException:
            shutil.rmtree(destination)
            raise


def verified_restore(backup, destination):
    """Verify all bytes before restoring to an absent destination; never overwrite."""
    backup, destination = Path(backup), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    try:
        manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict) or not manifest['files']:
            raise StateError('invalid backup manifest')
        content = {}
        for name, digest in manifest['files'].items():
            if not isinstance(name, str) or name != Path(name).name or name in ('.', '..', 'manifest.json'):
                raise StateError('unsafe backup path')
            path = backup / name
            if path.is_symlink() or not path.is_file():
                raise StateError('backup member must be a regular file')
            content[name] = path.read_bytes()
            if _hash(content[name]) != digest:
                raise StateError('backup checksum mismatch')
        if 'journal.sqlite3' not in content:
            raise StateError('backup is missing its journal')
    except (OSError, ValueError, TypeError) as exc:
        raise StateError('backup verification failed') from exc
    _claim_directory(destination)
    try:
        for name, payload in content.items():
            fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        StateStore(destination)
        _sync_directory(destination)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return destination
