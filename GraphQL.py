import requests


def _graphql_request(query: str, variables: dict = {}):
    """GraphQL queries all look alike, this is a generic function to facilitate a GraphQL Request.

    Arguments:
        query {str} -- A GraphQL Query

    Keyword Arguments:
        variables {dict} -- Optional Variables for the GraphQL Query (default: {{}})

    Raises:
        Exception: Raises an exception if the response is anything other than 200.

    Returns:
        dict -- Returns the JSON Response as a Dict.
    """
    # Strip all the whitespace and replace with spaces
    query = " ".join(query.split())
    payload = {'query': query}
    if variables:
        payload = {**payload, 'variables': variables}

    headers = {"Accept": "application/json"}
    response = requests.post("https://graphql.minaexplorer.com/",
                             json=payload,
                             headers=headers)
    resp_json = response.json()
    if response.status_code == 200 and "errors" not in resp_json:
        return resp_json
    else:
        print(response.text)
        raise Exception("Query failed -- returned code {}. {}".format(
            response.status_code, query))


def getStakingLedger(variables):
    """Return the staking ledger."""
    query = '''query ($delegate: String!, $ledgerHash: String!) {
  stakes(query: {delegate: $delegate, ledgerHash: $ledgerHash, chainId: "a7351a"}, limit: 2000) {
    public_key
    balance
    chainId
    timing {
      cliff_amount
      cliff_time
      initial_minimum_balance
      timed_epoch_end
      timed_in_epoch
      timed_weighting
      untimed_slot
      vesting_increment
      vesting_period
    }
  }
}
'''

    return (_graphql_request(query, variables))


def getBlocks(variables):
    """Returns all blocks the pool won."""
    query = """query($creator: String!, $epoch: Int, $blockHeightMin: Int, $blockHeightMax: Int, $dateTimeMin: DateTime, $dateTimeMax: DateTime){
    blocks(query: {creator: $creator, protocolState: {consensusState: {epoch: $epoch}}, canonical: true, blockHeight_gte: $blockHeightMin, blockHeight_lte: $blockHeightMax, dateTime_gte:$dateTimeMin, dateTime_lte:$dateTimeMax}, sortBy: DATETIME_DESC, limit: 1000) {
    blockHeight
    canonical
    creator
    dateTime
    txFees
    snarkFees
    receivedTime
    stateHash
    stateHashField
    winnerAccount {
      publicKey
    }
    protocolState {
      consensusState {
        blockHeight
        epoch
        slotSinceGenesis
      }
    }
    transactions {
      coinbase
      coinbaseReceiverAccount {
        publicKey
      }
      feeTransfer {
        fee
        recipient
        type
      }
    }
  }
}
"""
    return _graphql_request(query, variables)


def getLatestHeight():
    query = """{
  blocks(query: {canonical: true}, sortBy: DATETIME_DESC, limit: 1) {
    blockHeight
  }
}"""

    return (_graphql_request(query))


# def getLedgerHash(epoch: int) -> dict:
#     query = """query ($epoch: Int) {
#   blocks(query: {canonical: true, blockHeight_gte: 359605, protocolState:
#    {consensusState: {epoch: $epoch, blockHeight_gte: 359605}}}, limit: 1) {
#     protocolState {
#       consensusState {
#         stakingEpochData {
#           ledger {
#             hash
#           }
#         }
#         epoch
#       }
#     }
#   }
# }"""
#     variables = {
#         "epoch": epoch
#     }
#     return _graphql_request(query, variables)

def getLedgerHash(epoch: int) -> str:
    """
    Returns the staking-epoch ledger hash for a given epoch.
    Supports two GraphQL schema types:
    - Hasura (query_root, jsonb: protocol_state + where/_contains)
    - Legacy MinaExplorer (blocks(query: ...))
    """

    # 1) Detect which GraphQL schema is active
    try:
        info = _graphql_request("{ __schema { queryType { name } } }")
        query_root = info["data"]["__schema"]["queryType"]["name"]
        is_hasura = (query_root == "query_root")
    except Exception:
        # If introspection fails, assume legacy schema
        is_hasura = False

    if is_hasura:
        # 2A) Hasura variant:
        # Use JSONB field protocol_state + _contains filter on epoch
        query = """
        query($epoch:Int!){
          blocks(
            limit: 1
            order_by: { date_time: desc }
            where: {
              canonical: { _eq: true }
              block_height: { _gte: 359605 }
              protocol_state: { _contains: { consensusState: { epoch: $epoch } } }
            }
          ) {
            protocol_state
          }
        }
        """
        data = _graphql_request(query, {"epoch": int(epoch)})["data"]["blocks"]
        if not data:
            raise LookupError(f"No blocks found for epoch {epoch} (Hasura).")

        # Extract ledger hash from JSONB structure
        ps = data[0]["protocol_state"]
        cs = ps.get("consensusState") or ps.get("consensus_state") or {}
        sed = cs.get("stakingEpochData") or cs.get("staking_epoch_data") or {}
        ledger = sed.get("ledger") or {}
        h = ledger.get("hash")
        if not h:
            raise KeyError("ledger.hash not found in protocol_state (Hasura).")
        return h

    else:
        # 2B) Legacy MinaExplorer variant:
        # Use old blocks(query: {...}) syntax
        query = """
        query ($epoch: Int!) {
          blocks(
            limit: 1
            sortBy: DATETIME_DESC
            query: {
              canonical: true
              blockHeight_gte: 359605
              protocolState: { consensusState: { epoch: $epoch } }
            }
          ) {
            protocolState {
              consensusState {
                stakingEpochData { ledger { hash } }
              }
            }
          }
        }
        """
        data = _graphql_request(query, {"epoch": int(epoch)})["data"]["blocks"]
        if not data:
            raise LookupError(f"No blocks found for epoch {epoch} (legacy).")

        try:
            return (
                data[0]["protocolState"]["consensusState"]
                    ["stakingEpochData"]["ledger"]["hash"]
            )
        except (KeyError, TypeError) as e:
            raise KeyError("ledger.hash not found in legacy response.") from e
