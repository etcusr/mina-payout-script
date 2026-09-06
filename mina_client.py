"""Minimal Mina daemon GraphQL client.

Replaces the old `coda-python-client` dependency, which was pulled from a
GitHub repo that no longer exists and was wired in as a broken git submodule.
Only the handful of operations this project actually needs are implemented,
and the return shapes match the old client so callers stayed unchanged.

Every method returns the `data` field of the GraphQL response.

    from mina_client import MinaClient
    client = MinaClient(host="127.0.0.1", port=3085)
    client.unlock_wallet(pk, password)
    client.send_payment(to_pk=..., from_pk=..., amount=..., fee=..., memo=...)
    client.lock_wallet(pk)
"""

import json
import urllib.error
import urllib.request


class MinaGraphQLError(RuntimeError):
    """The daemon answered, but with an `errors` block."""

    def __init__(self, errors):
        self.errors = errors
        msgs = "; ".join(e.get("message", str(e)) for e in errors)
        super().__init__(msgs)


class MinaClient:
    def __init__(self, graphql_host="127.0.0.1", graphql_port=3085,
                 timeout=60, scheme="http"):
        self.url = f"{scheme}://{graphql_host}:{graphql_port}/graphql"
        self.timeout = timeout

    # --- transport ---------------------------------------------------------

    def _request(self, query, variables=None):
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # The daemon returns 4xx/5xx with a JSON body for GraphQL errors
            try:
                body = json.loads(e.read())
            except Exception:
                raise
        if "errors" in body and body["errors"]:
            raise MinaGraphQLError(body["errors"])
        return body["data"]

    # --- wallets -----------------------------------------------------------

    def get_wallets(self):
        return self._request("""
            { ownedWallets { publicKey balance { total } locked } }
        """)

    def get_wallet(self, pk):
        return self._request("""
            query($publicKey: PublicKey!) {
              wallet(publicKey: $publicKey) {
                publicKey
                balance { total unknown }
                nonce
                delegate
                locked
              }
            }
        """, {"publicKey": pk})

    def unlock_wallet(self, pk, password):
        return self._request("""
            mutation($publicKey: PublicKey!, $password: String!) {
              unlockWallet(input: {publicKey: $publicKey, password: $password}) {
                account { publicKey locked }
              }
            }
        """, {"publicKey": pk, "password": password})

    def lock_wallet(self, pk):
        return self._request("""
            mutation($publicKey: PublicKey!) {
              lockWallet(input: {publicKey: $publicKey}) {
                account { publicKey locked }
              }
            }
        """, {"publicKey": pk})

    # --- payments ----------------------------------------------------------

    def send_payment(self, to_pk, from_pk, amount, fee, memo):
        """Submit a payment. The sending wallet must be unlocked first.

        `amount` and `fee` are in nanomina.
        """
        return self._request("""
            mutation($from: PublicKey!, $to: PublicKey!, $amount: UInt64!,
                     $fee: UInt64!, $memo: String) {
              sendPayment(input: {
                from: $from, to: $to, amount: $amount, fee: $fee, memo: $memo
              }) {
                payment { id nonce from to amount fee memo }
              }
            }
        """, {"from": from_pk, "to": to_pk, "amount": str(amount),
              "fee": str(fee), "memo": memo})

    def get_pooled_payments(self, pk):
        """User commands sitting in the mempool for this account."""
        return self._request("""
            query($publicKey: PublicKey) {
              pooledUserCommands(publicKey: $publicKey) {
                id nonce from to amount fee memo
              }
            }
        """, {"publicKey": pk})

    def get_transaction_status(self, payment_id):
        """UNKNOWN / PENDING / INCLUDED for a submitted payment id."""
        return self._request("""
            query($paymentId: ID!) { transactionStatus(payment: $paymentId) }
        """, {"paymentId": payment_id})


# Backwards-compatible alias: the old code did
#   from src.codaclient import CodaClient
#   CodaClient.Client(graphql_host=..., graphql_port=...)
Client = MinaClient
