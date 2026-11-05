"""Health gate regressions; all daemon/DB fixtures are local and read only."""
import copy
import sqlite3
import unittest
from unittest.mock import patch

import preflight

CONFIG = {'VALIDATOR_ADDRESS': 'producer', 'SEND_FROM_ADDRESS': 'wallet',
          'EXPECTED_CHAIN_ID': 'network', 'ARCHIVE_DB_URL': 'private', 'SLOTS_PER_EPOCH': 10}
NODE = {'syncStatus': 'SYNCED', 'daemonStatus': {'chainId': 'network',
        'blockProductionKeys': ['producer'], 'blockchainLength': '25',
        'consensusConfiguration': {'k': '5', 'slotsPerEpoch': '10'}},
        'bestChain': [{'stateHash': 'h25', 'protocolState': {'consensusState':
                      {'blockchainLength': '25', 'epoch': '2'}}}]}
ACCOUNT = {'account': {'balance': {'total': '12345678901', 'liquid': '12345678900'},
           'nonce': '3', 'inferredNonce': '4'}, 'pooledUserCommands': [
           {'id': 'id', 'hash': 'tx', 'nonce': '3', 'from': 'wallet', 'to': 'other',
            'amount': '100000001', 'fee': '10000000', 'memo': '', 'isDelegation': False}]}


class Cursor:
    def __init__(self, db): self.db = db; self.cur = db.conn.cursor()
    def __enter__(self): return self
    def __exit__(self, *args): self.cur.close()
    def execute(self, query, params=None):
        import re
        # Exercise real SQL against an in-memory archive, adapting placeholders only.
        if query.startswith('SELECT set_config'):
            self.db.bounded = True; self.result = [('ok',)]; return
        self.result = None
        query = re.sub(r'%\((\w+)\)s', r':\1', query)
        self.cur.execute(query, params or {})
    def fetchone(self):
        rows = self.fetchall(); return rows[0] if rows else None
    def fetchall(self):
        if self.result is not None: return self.result
        names = [x[0] for x in self.cur.description]
        return [dict(zip(names, r)) for r in self.cur.fetchall()]


class Archive:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:'); self.closed = False; self.readonly = False; self.bounded = False
        self.conn.execute('CREATE TABLE blocks(id INTEGER PRIMARY KEY, state_hash TEXT, parent_id INTEGER, parent_hash TEXT, height INTEGER, protocol_version_id INTEGER, chain_status TEXT, global_slot_since_hard_fork INTEGER, timestamp TEXT)')
        for height in range(1, 26):
            self.conn.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?)',
                (height, 'h'+str(height), height-1 if height>1 else None, 'h'+str(height-1), height, 3,
                 'canonical' if height<=20 else 'pending', height-1, '1'))
    def cursor(self, **kwargs): return Cursor(self)
    def set_session(self, **kwargs): self.readonly = kwargs.get('readonly') and not kwargs.get('autocommit', False)
    def close(self): self.closed = True; self.conn.close()


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.db = Archive(); self.node = copy.deepcopy(NODE); self.account = copy.deepcopy(ACCOUNT)
        self.archives = [self.db]
    def fresh_archive(self):
        self.db = Archive(); self.archives.append(self.db)
    def tearDown(self):
        for db in self.archives:
            if not db.closed: db.close()
    def run_check(self, **kwargs):
        def graphql(config, query, variables=None):
            return copy.deepcopy(self.account if variables else self.node)
        with patch.object(preflight, '_graphql', side_effect=graphql), patch.object(preflight, '_connect', return_value=self.db):
            return preflight.check(CONFIG, **kwargs)
    def test_valid_history_exposes_exact_balances_and_authoritative_snapshot(self):
        got = self.run_check(epoch=1, require_wallet=True)
        self.assertEqual((got['network'], got['era'], got['tip_hash'], got['tip_height']), ('network', 3, 'h25', 25))
        self.assertEqual(got['epoch_first_height'], 11)
        self.assertEqual(got['account']['liquid_nano'], 12345678900)
        self.assertEqual(got['mempool_pending_nano'], 110000001)
        self.assertEqual(got['account']['inferred_nonce'], 4)
        self.assertTrue(self.db.readonly and self.db.bounded and self.db.closed)

    def test_absolute_height_extends_proved_ancestry_below_epoch_floor(self):
        got=self.run_check(epoch=1,min_height=2)
        self.assertEqual(got['best_chain'][-1]['height'],2)
        self.assertEqual(got['best_chain'][-1]['depth'],23)
    def test_not_synced_or_wrong_validator_or_chain_fails_closed(self):
        for mutation in ('sync', 'producer', 'network'):
            with self.subTest(mutation=mutation):
                self.node = copy.deepcopy(NODE)
                if mutation == 'sync': self.node['syncStatus'] = 'BOOTSTRAP'
                elif mutation == 'producer': self.node['daemonStatus']['blockProductionKeys'] = ['wrong']
                else: self.node['daemonStatus']['chainId'] = 'wrong'
                with self.assertRaises(preflight.PreflightError): self.run_check()
    def test_full_history_gap_fails_even_outside_target_epoch(self):
        self.db.conn.execute('DELETE FROM blocks WHERE height=3')
        with self.assertRaises(preflight.PreflightError): self.run_check(epoch=1)
    def test_parent_id_or_hash_break_fails(self):
        for column, value in [('parent_id', 1), ('parent_hash', 'wrong')]:
            with self.subTest(column=column):
                self.fresh_archive()
                self.db.conn.execute('UPDATE blocks SET '+column+'=? WHERE height=22', (value,))
                with self.assertRaises(preflight.PreflightError): self.run_check(epoch=1)
    def test_missing_head_and_orphaned_head_fail(self):
        for sql in ["DELETE FROM blocks WHERE height=25", "UPDATE blocks SET chain_status='orphaned' WHERE height=25"]:
            with self.subTest(sql=sql):
                self.fresh_archive(); self.db.conn.execute(sql)
                with self.assertRaises(preflight.PreflightError): self.run_check(epoch=1)
    def test_duplicate_canonical_and_stale_pending_fail(self):
        for status in ('canonical', 'pending'):
            with self.subTest(status=status):
                self.fresh_archive()
                self.db.conn.execute("INSERT INTO blocks SELECT 99,'extra',parent_id,parent_hash,height,protocol_version_id,?,global_slot_since_hard_fork,timestamp FROM blocks WHERE height=5", (status,))
                with self.assertRaises(preflight.PreflightError): self.run_check()
    def test_canonical_child_cannot_have_pending_parent(self):
        self.db.conn.execute("UPDATE blocks SET chain_status='pending',protocol_version_id=2 WHERE height=1")
        with self.assertRaises(preflight.PreflightError): self.run_check()
    def test_incoming_does_not_reduce_spendable_and_delegation_only_costs_fee(self):
        self.account['pooledUserCommands'].append({'from':'someone', 'amount':'999', 'fee':'1'})
        self.account['pooledUserCommands'][0]['isDelegation'] = True
        self.assertEqual(self.run_check(require_wallet=True)['mempool_pending_nano'], 10000000)
    def test_pending_nonce_gaps_duplicate_nonce_or_unreported_commitments_fail(self):
        for nonce in ('2', '4', '5'):
            with self.subTest(nonce=nonce):
                self.fresh_archive(); self.account['pooledUserCommands'][0]['nonce'] = nonce
                with self.assertRaises(preflight.PreflightError): self.run_check(require_wallet=True)
    def test_canonical_classifier_stuck_more_than_finality_k_fails(self):
        self.db.conn.execute("UPDATE blocks SET chain_status='pending' WHERE height>1")
        with self.assertRaises(preflight.PreflightError): self.run_check(epoch=1,require_wallet=True)
    def test_within_k_pending_blocks_keep_lower_payout_depth_policy(self):
        got=self.run_check(epoch=1,require_wallet=True)
        self.assertEqual(got['tip_height'],25)
        self.assertTrue(any(b['chain_status']=='pending' for b in got['best_chain']))
    def test_explicit_canonical_lag_allowance_is_block_based(self):
        self.db.conn.execute("UPDATE blocks SET chain_status='pending' WHERE height=20")
        cfg=dict(CONFIG,PREFLIGHT_CANONICAL_LAG_ALLOWANCE_BLOCKS=1)
        def graphql(config,query,variables=None): return copy.deepcopy(self.account if variables else self.node)
        with patch.object(preflight,'_graphql',side_effect=graphql),patch.object(preflight,'_connect',return_value=self.db):
            got=preflight.check(cfg,epoch=1,require_wallet=True)
        self.assertTrue(got['ok'])
    def test_truncated_pending_only_archive_is_not_a_fresh_era(self):
        self.db.conn.execute('DELETE FROM blocks WHERE height<21')
        with self.assertRaises(preflight.PreflightError): self.run_check(require_wallet=True)
    def test_fresh_era_requires_slot_zero_and_valid_previous_canonical_parent(self):
        for damage in ('nonzero_slot','missing_parent','wrong_parent_hash','pending_parent'):
            with self.subTest(damage=damage):
                self.fresh_archive()
                self.db.conn.execute("UPDATE blocks SET chain_status='pending',protocol_version_id=4,global_slot_since_hard_fork=height-21 WHERE height>20")
                self.node['bestChain'][0]['protocolState']['consensusState']['epoch']='0'
                if damage=='nonzero_slot': self.db.conn.execute('UPDATE blocks SET global_slot_since_hard_fork=height-20 WHERE height>20')
                elif damage=='missing_parent': self.db.conn.execute('UPDATE blocks SET parent_id=99 WHERE height=21')
                elif damage=='wrong_parent_hash': self.db.conn.execute("UPDATE blocks SET parent_hash='wrong' WHERE height=21")
                else: self.db.conn.execute("UPDATE blocks SET chain_status='pending' WHERE height=20")
                with self.assertRaises(preflight.PreflightError): self.run_check(require_wallet=True)
    def test_fresh_protocol_era_within_k_need_not_have_canonical_rows(self):
        self.db.conn.execute("UPDATE blocks SET chain_status='pending',protocol_version_id=4,global_slot_since_hard_fork=height-21 WHERE height>20")
        self.node['bestChain'][0]['protocolState']['consensusState']['epoch']='0'
        got=self.run_check()
        self.assertEqual(got['era'],4)
    def test_backward_slot_in_canonical_history_fails(self):
        self.db.conn.execute('UPDATE blocks SET global_slot_since_hard_fork=1 WHERE height=15')
        with self.assertRaises(preflight.PreflightError): self.run_check()
    def test_tip_epoch_must_match_archive_slot_epoch(self):
        self.db.conn.execute('UPDATE blocks SET global_slot_since_hard_fork=35 WHERE height=25')
        with self.assertRaises(preflight.PreflightError): self.run_check()
    def test_current_epoch_can_preview_but_cannot_mutate_wallet(self):
        with self.assertRaises(preflight.PreflightError): self.run_check(epoch=2, require_wallet=True)
    def test_snapshot_total_time_limit_fails_closed(self):
        with patch.object(preflight.time, 'monotonic', side_effect=[0, 100]):
            with self.assertRaises(preflight.PreflightError): self.run_check()
    def test_quiet_chain_not_rejected_by_wall_clock_age(self):
        self.assertTrue(self.run_check()['ok'])
    def test_timeout_is_sanitized(self):
        with patch.object(preflight, '_graphql', side_effect=TimeoutError('postgres://user:SECRET@host')):
            with self.assertRaises(preflight.PreflightError) as error: preflight.check(CONFIG)
        self.assertNotIn('SECRET', str(error.exception))
    def test_missing_network_binding_fails(self):
        with self.assertRaises(preflight.PreflightError): preflight.check({})
    def test_stale_snapshot_nonintegral_amount_and_missing_account_fail(self):
        for bad in (None, {'balance': {'total': '2.5', 'liquid': '2.5'}, 'nonce': '3', 'inferredNonce': '3'}):
            with self.subTest(bad=bad):
                self.fresh_archive(); self.account['account'] = bad
                with self.assertRaises(preflight.PreflightError): self.run_check(require_wallet=True)

if __name__ == '__main__': unittest.main()
