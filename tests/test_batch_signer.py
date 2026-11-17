import json
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import batch_signer as signer

WALLET = 'B62' + 'a' * 52
KEY = 'EK' + 'b' * 50
PASSWORD = 'secret password; $not-shell-expanded'
CONFIG = {'SEND_FROM_ADDRESS': WALLET, 'SSH_HOST': 'test-host', 'EXPECTED_CHAIN_ID': 'test-chain'}
EXPECTED = {'network': 'mainnet', 'wallet': WALLET, 'nonce': 0, 'fee_nano': '10000000',
            'memo': 'batch_test', 'recipients': [{'recipient': 'B62' + 'c' * 52, 'amount_nano': '1'}]}
PATH = '/root/.mina-config/wallets/store/' + WALLET


class SignerTests(unittest.TestCase):
    def setUp(self):
        self.chain = patch.object(signer, '_check_chain')
        self.chain.start()
        self.addCleanup(self.chain.stop)

    def test_secret_pipe_only_and_sanitized_worker_environment(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            self.assertNotIn(PASSWORD, repr(args))
            self.assertNotIn(KEY, repr(args))
            if args[0] == 'ssh':
                self.assertEqual(kwargs['input'], PASSWORD + '\n')
                return SimpleNamespace(returncode=0, stdout=f'Public key: {WALLET}\nPrivate key: {KEY}\n', stderr='')
            self.assertNotIn('NODE_OPTIONS', kwargs['env'])
            self.assertNotIn('NODE_PATH', kwargs['env'])
            packet = json.loads(kwargs['input'])
            result = {'valid': True} if packet['action'] == 'validate' else {'signed': '{}', 'tx_hash': '5J' + 'x' * 50}
            if packet['action'] == 'sign':
                self.assertEqual(packet['privateKey'], KEY)
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr='')
        with patch.object(signer, '_graphql', return_value={'trackedAccounts': [{'publicKey': WALLET, 'privateKeyPath': PATH}]}), patch.object(signer.subprocess, 'run', side_effect=run), patch.dict('os.environ', {'NODE_OPTIONS': '--bad', 'NODE_PATH': '/bad'}):
            result = signer.sign(CONFIG, '{}', EXPECTED, PASSWORD)
        self.assertTrue(result['tx_hash'].startswith('5J'))
        self.assertEqual([c[0][0] for c in calls], ['node', 'ssh', 'node'])

    def test_validation_failure_never_opens_wallet(self):
        with patch.object(signer, '_worker', side_effect=signer.SignerError('invalid')), patch.object(signer, '_export_key') as export:
            with self.assertRaises(signer.SignerError):
                signer.sign(CONFIG, '{}', EXPECTED, PASSWORD)
            export.assert_not_called()

    def test_network_and_config_sender_bound_before_worker(self):
        with patch.object(signer, '_worker') as worker:
            for expected in ({**EXPECTED, 'network': 'devnet'}, {**EXPECTED, 'wallet': 'other'}):
                with self.assertRaises(signer.SignerError):
                    signer.sign(CONFIG, '{}', expected, PASSWORD)
            worker.assert_not_called()

    def test_path_requires_exact_unique_wallet_and_safe_path(self):
        for rows in ([], [{'publicKey': 'other', 'privateKeyPath': PATH}],
                     [{'publicKey': WALLET, 'privateKeyPath': '/tmp/wrong'}],
                     [{'publicKey': WALLET, 'privateKeyPath': '/tmp/../' + WALLET}],
                     [{'publicKey': WALLET, 'privateKeyPath': PATH}] * 2):
            with patch.object(signer, '_graphql', return_value={'trackedAccounts': rows}), patch.object(signer.subprocess, 'run') as run:
                with self.assertRaises(signer.SignerError):
                    signer._export_key(CONFIG, WALLET, PASSWORD, 1)
                run.assert_not_called()

    def test_timeout_and_bad_export_never_expose_secret_output(self):
        cases = [subprocess.TimeoutExpired(['ssh'], 1, output=KEY, stderr=PASSWORD),
                 SimpleNamespace(returncode=1, stdout=KEY, stderr=PASSWORD),
                 SimpleNamespace(returncode=0, stdout=f'{KEY}\nB62' + 'z' * 52, stderr=PASSWORD)]
        for result in cases:
            with patch.object(signer, 'discover_key_path', return_value=PATH), patch.object(signer.subprocess, 'run', side_effect=result if isinstance(result, Exception) else None, return_value=result):
                with self.assertRaises(signer.SignerError) as caught:
                    signer._export_key(CONFIG, WALLET, PASSWORD, 1)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn(PASSWORD, str(caught.exception))

    def test_worker_failure_sanitized(self):
        with patch.object(signer.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['node'], 1, output=KEY, stderr=PASSWORD)):
            with self.assertRaises(signer.SignerError) as caught:
                signer._worker({'privateKey': KEY})
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_validate_signed_is_offline_and_keyless(self):
        with patch.object(signer, '_worker', return_value={'tx_hash': '5J' + 'x' * 50, 'query': 'mutation {}'}) as worker, patch.object(signer, '_export_key') as export, patch.object(signer, '_graphql') as graphql:
            signer.validate(CONFIG, '{}', EXPECTED)
            self.assertEqual(worker.call_args.args[0]['action'], 'verify')
            export.assert_not_called()
            graphql.assert_not_called()

    def test_chain_identity_and_signature_domain(self):
        self.chain.stop()
        for data in ({'signatureKind': 'devnet', 'daemonStatus': {'chainId': 'test-chain'}}, {'signatureKind': 'mainnet', 'daemonStatus': {'chainId': 'other'}}):
            with patch.object(signer, '_graphql', return_value=data):
                with self.assertRaises(signer.SignerError):
                    signer._check_chain(CONFIG)
        with patch.object(signer, '_graphql', return_value={'signatureKind': 'mainnet', 'daemonStatus': {'chainId': 'test-chain'}}):
            signer._check_chain(CONFIG)

    def test_build_returns_unsigned_without_key_access(self):
        with patch.object(signer, '_worker', return_value={'unsigned': '{}'}) as worker, patch.object(signer, '_export_key') as export:
            self.assertEqual(signer.build(CONFIG, EXPECTED), '{}')
            self.assertEqual(worker.call_args.args[0]['endpoint'], 'http://127.0.0.1:3085/graphql')
            export.assert_not_called()
        with self.assertRaises(signer.SignerError):
            signer.build({**CONFIG, 'GRAPHQL_HOST': 'remote.invalid'}, EXPECTED)


if __name__ == '__main__':
    unittest.main()
