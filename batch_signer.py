"""Mainnet batch builder and ephemeral SSH signer.

Only ``sign`` accesses the encrypted daemon wallet. Call it after explicit review
and interactive password entry by the CLI. Secrets travel exclusively in pipes,
are never written to disk, and subprocess output is never included in errors.
Python/Node strings cannot promise cryptographic memory erasure; process lifetime
is bounded and references are dropped promptly. No retry of signing or sending.
"""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess

from preflight import _graphql


class SignerError(RuntimeError):
    pass


WORKER = Path(__file__).resolve().parent / 'batch-pay' / 'mainnet-worker.mjs'
_BASE58 = r'[1-9A-HJ-NP-Za-km-z]'


def _bound_expected(config, expected):
    if (not isinstance(expected, dict) or expected.get('network') != 'mainnet' or
            not config.get('SEND_FROM_ADDRESS') or expected.get('wallet') != config['SEND_FROM_ADDRESS']):
        raise SignerError('Batch wallet or network does not match configuration')
    return dict(expected)


def _check_chain(config):
    try:
        data = _graphql(config, '{ signatureKind daemonStatus { chainId } }')
        if (data.get('signatureKind') != 'mainnet' or not config.get('EXPECTED_CHAIN_ID') or
                data.get('daemonStatus', {}).get('chainId') != config['EXPECTED_CHAIN_ID']):
            raise ValueError()
    except Exception:
        raise SignerError('Signer daemon network or chain identity mismatch') from None


def _worker(packet, timeout=120):
    try:
        # NODE_OPTIONS/NODE_PATH must not preload arbitrary modules into a key
        # bearing process. Use the installed pinned worker without a shell.
        env = {k: v for k, v in os.environ.items() if k not in ('NODE_OPTIONS', 'NODE_PATH')}
        result = subprocess.run(['node', str(WORKER)], input=json.dumps(packet),
                                text=True, capture_output=True, timeout=timeout, check=False, env=env)
        if result.returncode:
            raise SignerError('Batch worker rejected payload or failed')
        answer = json.loads(result.stdout)
        if not isinstance(answer, dict):
            raise SignerError('Invalid batch worker result')
        return answer
    except SignerError:
        raise
    except Exception:
        raise SignerError('Batch worker failed or timed out') from None


def build(config, expected, *, timeout=120):
    """Build and validate an unsigned transaction; does not access wallet keys."""
    packet = _bound_expected(config, expected)
    _check_chain(config)
    host = config.get('GRAPHQL_HOST', '127.0.0.1')
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise SignerError('Batch builder requires a local daemon endpoint')
    endpoint = '{}://{}:{}/graphql'.format(config.get('GRAPHQL_SCHEME', 'http'),
        '[::1]' if host == '::1' else host, config.get('GRAPHQL_PORT', 3085))
    answer = _worker(dict(packet, action='build', endpoint=endpoint), timeout)
    if set(answer) != {'unsigned'} or not isinstance(answer['unsigned'], str):
        raise SignerError('Invalid unsigned transaction result')
    return answer['unsigned']


def discover_key_path(config, wallet):
    """Read only the tracked-wallet metadata; never accept a caller key path."""
    try:
        data = _graphql(config, '{ trackedAccounts { publicKey privateKeyPath } }')
        rows = [r for r in data['trackedAccounts'] if r.get('publicKey') == wallet]
        if len(rows) != 1:
            raise ValueError()
        path = rows[0]['privateKeyPath']
        if (not isinstance(path, str) or not path.startswith('/') or
                any(c in path for c in ('\x00', '\n', '\r')) or
                '..' in PurePosixPath(path).parts or PurePosixPath(path).name != wallet):
            raise ValueError()
        return path
    except Exception:
        raise SignerError('Cannot identify the exact encrypted sender wallet') from None


def _export_key(config, wallet, password, timeout):
    if not isinstance(password, str) or any(c in password for c in ('\x00', '\n', '\r')):
        raise SignerError('Wallet password must be entered as one line')
    path = discover_key_path(config, wallet)
    host = config.get('SSH_HOST', '')
    container = config.get('BATCH_SIGNER_CONTAINER', 'mina')
    if not isinstance(host, str) or not host or host.startswith('-') or any(c.isspace() for c in host):
        raise SignerError('Configure a valid SSH signer host')
    if not isinstance(container, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', container):
        raise SignerError('Configure a valid signer container')
    # The password is read within the container, not interpolated into either
    # shell. ssh and docker both retain stdin; Mina reads its standard password
    # environment variable. Neither subprocess arguments nor local env contain it.
    inner = ('IFS= read -r MINA_PRIVKEY_PASS || exit 1; export MINA_PRIVKEY_PASS; '
             'exec mina advanced dump-keypair --privkey-path ' + shlex.quote(path))
    remote = 'docker exec -i ' + shlex.quote(container) + ' sh -c ' + shlex.quote(inner)
    try:
        result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', host, remote],
                                input=password + '\n', text=True, capture_output=True,
                                timeout=timeout, check=False)
        if result.returncode:
            raise SignerError('Encrypted wallet unlock failed')
        # Mina dump-keypair prints Base58 keys. Restrict parsing to exactly one
        # private key, and reject a conflicting printed public key if present.
        keys = re.findall(r'(?<![A-Za-z0-9])(EK' + _BASE58 + r'{48,60})(?![A-Za-z0-9])', result.stdout)
        public = re.findall(r'(?<![A-Za-z0-9])(B62' + _BASE58 + r'{48,60})(?![A-Za-z0-9])', result.stdout)
        if len(keys) != 1 or any(key != wallet for key in public):
            raise SignerError('Encrypted wallet export could not be verified')
        return keys[0]
    except SignerError:
        raise
    except Exception:
        raise SignerError('Encrypted wallet unlock failed or timed out') from None


def sign(config, unsigned, expected, password, *, timeout=120):
    """Validate first, unlock the exact wallet, sign offline and verify again."""
    packet = _bound_expected(config, expected)
    _check_chain(config)
    # Invalid payloads fail BEFORE remote wallet access, including malformed
    # amounts, native-token layout, nonce, memo, payer, and hidden updates.
    validation = _worker({'action': 'validate', 'network': 'mainnet', 'unsigned': unsigned,
                          'expected': packet}, timeout)
    if validation != {'valid': True}:
        raise SignerError('Unsigned batch validation failed')
    key = None
    try:
        key = _export_key(config, packet['wallet'], password, timeout)
        answer = _worker({'action': 'sign', 'network': 'mainnet', 'unsigned': unsigned,
                          'expected': packet, 'privateKey': key}, timeout)
        if (set(answer) != {'signed', 'tx_hash'} or not isinstance(answer['signed'], str) or
                not isinstance(answer['tx_hash'], str) or not re.fullmatch(r'5J' + _BASE58 + r'{40,60}', answer['tx_hash'])):
            raise SignerError('Invalid signed transaction result')
        return answer
    finally:
        key = None
        password = None


def validate(config, signed, expected, *, timeout=120):
    """Verify immutable payload and both signatures offline before submission.

    Caller checks daemon health immediately before its one submission attempt.
    This method neither reads a wallet nor opens a network connection.
    """
    packet = _bound_expected(config, expected)
    answer = _worker({'action': 'verify', 'network': 'mainnet', 'signed': signed,
                      'expected': packet}, timeout)
    if (set(answer) != {'tx_hash', 'query'} or not isinstance(answer['query'], str) or
            not isinstance(answer['tx_hash'], str) or not re.fullmatch(r'5J' + _BASE58 + r'{40,60}', answer['tx_hash'])):
        raise SignerError('Invalid verified transaction result')
    return answer
