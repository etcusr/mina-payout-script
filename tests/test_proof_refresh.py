import unittest
from unittest.mock import patch
import GraphQL
from payout_workflow import LiveAdapter
from payout_state import StateError
from tests.test_calculation import W


def snapshot(tip, ancestors):
    return {'tip_hash':tip,'network':'chain','era':3,'epoch_first_height':None,
            'best_chain':[{'state_hash':h,'height':500-i} for i,h in enumerate(ancestors)]}


class ProofRefreshTests(unittest.TestCase):
    def test_new_block_repeats_read_only_capture_and_uses_fresh_proof(self):
        adapter=LiveAdapter({'SEND_FROM_ADDRESS':W})
        old=snapshot('old',['old']); new=snapshot('new',['new','old'])
        with patch.object(GraphQL,'configure'), patch.object(GraphQL,'_query_scalar',return_value=100), \
             patch('preflight.check',side_effect=[new,new]) as check, \
             patch.object(GraphQL,'getSentPayments',return_value=[]) as payments:
            adapter.previous_payments(old,[({'nonce':4},'3',7,15)])
        self.assertEqual(check.call_count,2)
        self.assertIs(payments.call_args.kwargs['health'],new)
        self.assertEqual(old['tip_hash'],'old')

    def test_reorganization_or_network_change_never_accepted(self):
        for new in (snapshot('fork',['fork']),dict(snapshot('new',['new','old']),network='other'),
                    dict(snapshot('new',['new','old']),era=4)):
            with self.subTest(new=new),patch('preflight.check',return_value=new), \
                 patch.object(GraphQL,'getSentPayments') as payments:
                with self.assertRaises(StateError):
                    LiveAdapter({'SEND_FROM_ADDRESS':W}).payments(snapshot('old',['old']),epoch=6)
                payments.assert_not_called()

    def test_continuously_moving_tip_is_bounded(self):
        snapshots=[snapshot(str(i),[str(j) for j in range(i,-1,-1)]) for i in range(1,5)]
        with patch('preflight.check',side_effect=snapshots) as check,patch.object(GraphQL,'getSentPayments') as payments:
            with self.assertRaises(StateError):
                LiveAdapter({'SEND_FROM_ADDRESS':W}).payments(snapshot('0',['0']),epoch=6)
            self.assertEqual(check.call_count,3); payments.assert_not_called()
