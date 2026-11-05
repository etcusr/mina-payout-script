import unittest
from unittest.mock import patch
from decimal import Decimal
from mina_client import MinaClient
import GraphQL
from tests.test_calculation import A,W

class AdapterTests(unittest.TestCase):
    def test_rpc_requires_explicit_nonce_and_returns_hash(self):
        client=MinaClient(); captured=[]
        client._request=lambda q,v: captured.append((q,v)) or {'ok':True}
        with self.assertRaises((ValueError,TypeError)): client.send_payment(A,W,1,1,'memo')
        self.assertEqual(client.send_payment(A,W,1,1,'memo',nonce=17),{'ok':True})
        self.assertEqual(captured[0][1]['nonce'],'17')
        self.assertIn('hash',captured[0][0])
    def test_archive_receipt_keeps_full_exact_identity_and_captured_depth(self):
        health={'network':'n','era':3,'best_chain':[{'id':2,'state_hash':'block','depth':20}], 'tip_hash':'tip'}
        row={'to_pk':A,'from_pk':W,'amount':9007199254740993,'fee':1,'nonce':17,'memo':'memo','hash':'tx','height':21,'block_id':2,'block_hash':'block','era':3,'status':'applied'}
        with patch.object(GraphQL,'_probe_schema',return_value={'uc_type_col':'command_type','buc_has_status':True}),patch.object(GraphQL,'_query_dict',return_value=[row]):
            result=GraphQL.getSentPayments(W,health=health)[0]
        self.assertEqual(result['amount_nano'],9007199254740993)
        self.assertEqual(result['amount_mina'],Decimal('9007199.254740993'))
        self.assertEqual(result['nonce'],17); self.assertEqual(result['depth'],20)
        self.assertEqual(result['wallet'],W); self.assertEqual(result['status'],'applied')
    def test_staking_balance_preserves_last_nanomina(self):
        with patch.object(GraphQL,'_resolve_epoch_for_hash',return_value=8),patch.object(GraphQL,'_download_ledger',return_value=[{'delegate':W,'pk':A,'balance':'9007199.254740993'}]):
            value=GraphQL.getStakingLedger({'delegate':W,'ledgerHash':'h'})['data']['stakes'][0]['balance']
        self.assertEqual(value,Decimal('9007199.254740993'))
    def test_staking_nan_is_fatal_not_zero(self):
        with patch.object(GraphQL,'_resolve_epoch_for_hash',return_value=8),patch.object(GraphQL,'_download_ledger',return_value=[{'delegate':W,'pk':A,'balance':'NaN'}]):
            with self.assertRaises(ValueError): GraphQL.getStakingLedger({'delegate':W,'ledgerHash':'h'})
    def test_shared_config_loader_preserves_unquoted_exact_decimal(self):
        import tempfile
        from pathlib import Path
        from tunnel import load_config
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'config.yml'
            p.write_text('ARCHIVE_DB_URL: postgresql://reader@localhost/db\nVALIDATOR_FEE: 0.123456789123456789\nMINIMUM_PAYOUT: 0.000000001\nPAYOUT_REDIRECTS:\n  source:\n    target: 0.333333333333333333\n')
            config=load_config(p)
        self.assertEqual(config['VALIDATOR_FEE'],Decimal('0.123456789123456789'))
        self.assertEqual(config['PAYOUT_REDIRECTS']['source']['target'],Decimal('0.333333333333333333'))
    def test_historical_ledger_hash_is_bound_to_archive_era(self):
        with patch.object(GraphQL,'_slots_per_epoch',return_value=7140),patch.object(GraphQL,'_protocol_version_id',return_value=3),patch.object(GraphQL,'_query_dict',return_value=[{'ledger_hash':'expected-era-ledger'}]),patch.object(GraphQL,'_daemon_query',side_effect=AssertionError('must not guess from live epoch')),patch.object(GraphQL,'_gcs_list',side_effect=AssertionError('must not guess newest bucket object')):
            self.assertEqual(GraphQL.getLedgerHash(6),'expected-era-ledger')
    def test_ambiguous_epoch_ledger_hash_fails_closed(self):
        with patch.object(GraphQL,'_slots_per_epoch',return_value=7140),patch.object(GraphQL,'_protocol_version_id',return_value=3),patch.object(GraphQL,'_query_dict',return_value=[{'ledger_hash':'one'},{'ledger_hash':'two'}]),patch.object(GraphQL,'_daemon_query',return_value={}),patch.object(GraphQL,'_gcs_list',return_value=[]):
            with self.assertRaises(ValueError): GraphQL.getLedgerHash(6)
    def test_late_withdrawal_expands_proof_ancestry_from_captured_tip(self):
        from payout_workflow import LiveAdapter
        cfg={'SEND_FROM_ADDRESS':W}
        adapter=LiveAdapter(cfg)
        short={'tip_hash':'tip','network':'chain','era':3,'epoch_first_height':None,'best_chain':[{'id':500}]}
        deep=dict(short,epoch_first_height=1,best_chain=[{'id':500},{'id':1}])
        with patch('preflight.check',return_value=deep) as check,patch.object(GraphQL,'getSentPayments',return_value=[]) as payments:
            adapter.payments(short,epoch=6)
        self.assertEqual(payments.call_args.kwargs['health']['best_chain'],[{'id':500},{'id':1}])
        self.assertEqual(check.call_args.kwargs,{'epoch':6,'require_wallet':False})

    def test_previous_epoch_expands_even_when_current_epoch_floor_is_present(self):
        from payout_workflow import LiveAdapter
        adapter=LiveAdapter({'SEND_FROM_ADDRESS':W})
        short={'tip_hash':'tip','network':'chain','era':3,'epoch_first_height':400,'checked_epoch':8,'best_chain':[{'id':500}]}
        deep=dict(short,epoch_first_height=1,checked_epoch=6,best_chain=[{'id':500},{'id':1}])
        with patch('preflight.check',return_value=deep) as check,patch.object(GraphQL,'getSentPayments',return_value=[]) as payments:
            adapter.payments(short,epoch=6)
        self.assertEqual(check.call_args.kwargs,{'epoch':6,'require_wallet':False})
        self.assertEqual(payments.call_args.kwargs['health']['checked_epoch'],6)

    def test_previous_era_uses_absolute_payment_height_not_reused_epoch(self):
        from payout_workflow import LiveAdapter
        adapter=LiveAdapter({'SEND_FROM_ADDRESS':W})
        health={'tip_hash':'tip','network':'chain','era':4,'checked_epoch':1,
                'best_chain':[{'id':500,'height':500}]}
        deep=dict(health,best_chain=[{'id':500,'height':500},{'id':100,'height':100}])
        expected=[({'nonce':4},'3',7,15)]
        with patch.object(GraphQL,'configure'),patch.object(GraphQL,'_query_scalar',return_value=100),patch('preflight.check',return_value=deep) as check,patch.object(GraphQL,'getSentPayments',return_value=[]) as payments:
            adapter.previous_payments(health,expected)
        self.assertEqual(check.call_args.kwargs,{'require_wallet':False,'min_height':100})
        self.assertEqual(payments.call_args.kwargs['health']['best_chain'][-1]['height'],100)
    def test_vesting_uses_exact_ceiling_and_handles_degenerate_cases(self):
        timing={'cliff_time':'10','vesting_period':'5','initial_minimum_balance':'10','cliff_amount':'0','vesting_increment':'3'}
        self.assertEqual(GraphQL._compute_untimed_slot(timing),30)
        self.assertEqual(GraphQL._compute_untimed_slot(dict(timing,initial_minimum_balance='0')),0)
        self.assertEqual(GraphQL._compute_untimed_slot(dict(timing,cliff_amount='11')),10)
        self.assertEqual(GraphQL._compute_untimed_slot(dict(timing,vesting_period='0')),10)
        self.assertGreater(GraphQL._compute_untimed_slot(dict(timing,vesting_increment='0')),2**32-1)
    def test_invalid_or_incomplete_timing_never_becomes_unlocked(self):
        good={'cliff_time':'10','vesting_period':'5','initial_minimum_balance':'10','cliff_amount':'0','vesting_increment':'3'}
        invalid=[{},dict(good,cliff_time='broken'),dict(good,vesting_period='-1'),dict(good,cliff_amount='NaN'),dict(good,initial_minimum_balance='Infinity'),dict(good,vesting_increment='-3'),dict(good,cliff_time='0.5')]
        for timing in invalid:
            with self.subTest(timing=timing),patch.object(GraphQL,'_resolve_epoch_for_hash',return_value=8),patch.object(GraphQL,'_download_ledger',return_value=[{'delegate':W,'pk':A,'balance':'1','timing':timing}]):
                with self.assertRaises(ValueError): GraphQL.getStakingLedger({'delegate':W,'ledgerHash':'h'})
    def test_vesting_boundary_does_not_earn_supercharge_one_period_early(self):
        from payout_calculation import calculate
        from tests.test_calculation import B
        timing={'cliff_time':'10','vesting_period':'5','initial_minimum_balance':'10','cliff_amount':'0','vesting_increment':'3'}
        timing['untimed_slot']=GraphQL._compute_untimed_slot(timing)
        config={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'DEFAULT_TX_FEE':0,'MINIMUM_PAYOUT':'0'}
        stakes=[{'public_key':A,'balance':'1','timing':timing},{'public_key':B,'balance':'1','timing':None}]
        for slot,expected in ((29,50),(30,100)):
            result=calculate(config,3,8,stakes,[{'reward_nano':200,'snark_nano':0,'fees_nano':0,'slot':slot,'winner':A}],base_coinbase_nano=100,carry={})
            self.assertEqual(result['gross_nano'][A],expected)
