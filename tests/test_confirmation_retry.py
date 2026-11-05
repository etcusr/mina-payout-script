"""Connection loss may repeat proof reads, never payment submission."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import payout_cli
import preflight
from payout_workflow import Workflow
from tests.test_workflow import Fake
from tests.test_calculation import A,W
from tests.test_preflight import CONFIG


class ConfirmationRetryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.fake=Fake()
        cfg={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'EXPECTED_CHAIN_ID':'chain',
             'LEGACY_PROTOCOL_VERSION_ID':3,'DEFAULT_TX_FEE':1,'TX_CONFIRMATIONS_NUM':3}
        self.w=Workflow(cfg,Path(tmp.name),adapter=self.fake); self.w.initialize()
        self.digest=self.w.create_plan(8,{'gross_nano':{A:100},'net_nano':{A:100},
            'carry_before_nano':{},'carry_after_nano':{},'policy':{'fee_nano':1}}, {})
        self.w.send(self.digest,password='fake')

    def connection_error(self):
        return getattr(preflight,'PreflightConnectionError',preflight.PreflightError)('connection unavailable')

    def test_transport_errors_are_distinguished_and_sanitized(self):
        for error in (urllib.error.URLError(ConnectionRefusedError('SECRET')),TimeoutError('SECRET')):
            with self.subTest(error=type(error).__name__),patch.object(preflight,'_graphql',side_effect=error):
                with self.assertRaises(preflight.PreflightError) as caught: preflight.check(CONFIG)
                self.assertEqual(type(caught.exception).__name__,'PreflightConnectionError')
                self.assertNotIn('SECRET',str(caught.exception))

    def test_invalid_http_response_is_not_a_retryable_transport_error(self):
        error=urllib.error.HTTPError('SECRET',401,'SECRET',{},None)
        with patch.object(preflight,'_graphql',side_effect=error):
            with self.assertRaises(preflight.PreflightError) as caught: preflight.check(CONFIG)
        self.assertNotEqual(type(caught.exception).__name__,'PreflightConnectionError')
        self.assertNotIn('SECRET',str(caught.exception))

    def test_connection_loss_recovers_and_settles_without_resending(self):
        health=self.fake.health
        calls=[]
        def interrupted(*args,**kwargs):
            calls.append(True)
            if len(calls)==1: raise self.connection_error()
            self.fake.include(3)
            return health(*args,**kwargs)
        self.fake.health=interrupted
        with patch.object(payout_cli.tunnel,'ensure',return_value={}) as ensure,patch('time.sleep'),contextlib.redirect_stdout(io.StringIO()) as output:
            result=payout_cli.complete_accounting(self.w,self.digest,60,'mina_pay --epoch 8')
        self.assertTrue(result['settled']); ensure.assert_called_once_with(self.w.config)
        self.assertIn('связь',output.getvalue().lower())
        self.assertEqual(sum(isinstance(x,tuple) for x in self.fake.calls),1)
        self.assertEqual(self.fake.calls.count('unlock'),1)

    def test_connection_timeout_leaves_reserved_payment_and_honest_result(self):
        with patch.object(self.fake,'health',side_effect=self.connection_error()),patch.object(payout_cli.tunnel,'ensure') as ensure,contextlib.redirect_stdout(io.StringIO()) as output:
            result=payout_cli.complete_accounting(self.w,self.digest,0,'mina_pay --epoch 8')
            payout_cli.completion_report(result)
        self.assertFalse(result['settled']); self.assertEqual(result['pending_reason'],'connectivity')
        self.assertIn('связь',output.getvalue().lower()); ensure.assert_not_called()
        self.assertIsNone(self.w.store().settlement(self.digest))
        self.assertEqual(self.w.store().list_intents()[0]['status'],'submitted')

    def test_failed_tunnel_recovery_stays_pending_until_deadline(self):
        clock=[0]
        with patch.object(self.fake,'health',side_effect=self.connection_error()),patch.object(payout_cli.tunnel,'ensure',side_effect=payout_cli.tunnel.TunnelError('unavailable')),patch('time.monotonic',side_effect=lambda:clock[0]),patch('time.sleep',side_effect=lambda _:clock.__setitem__(0,61)),contextlib.redirect_stdout(io.StringIO()):
            result=payout_cli.complete_accounting(self.w,self.digest,60,'mina_pay --epoch 8')
        self.assertFalse(result['settled']); self.assertIsNone(self.w.store().settlement(self.digest))
        self.assertEqual(sum(isinstance(x,tuple) for x in self.fake.calls),1)

    def test_failed_integrity_check_is_not_retried(self):
        with patch.object(self.fake,'health',side_effect=preflight.PreflightError('wrong network')),patch.object(payout_cli.tunnel,'ensure') as ensure:
            with self.assertRaises(preflight.PreflightError):
                payout_cli.complete_accounting(self.w,self.digest,60,'mina_pay --epoch 8')
        ensure.assert_not_called(); self.assertIsNone(self.w.store().settlement(self.digest))
