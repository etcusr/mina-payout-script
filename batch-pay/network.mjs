import { Mina, PublicKey, fetchAccount } from 'o1js';
const {sendZkappQuery}=await import(new URL('./lib/mina/v1/graphql.js',import.meta.resolve('o1js')));

export const ENDPOINT='https://devnet-plain-1.gcp.o1test.net/graphql';
// Verified against the official Devnet endpoint, 2026-11-17. A chain reset must
// require explicit code/config review; never silently adopt another chain.
export const CHAIN_ID='ebfce0d570bc22eb041e1a7b0a46bbc3ed5d0a1030ef2ab5e36eef908b93eba8';
const daemon='daemonStatus { chainId syncStatus }';
async function request(query,variables={}) {
  const response=await fetch(ENDPOINT,{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({query,variables}),signal:AbortSignal.timeout(25000),redirect:'error'});
  if(!response.ok)throw Error(`Devnet HTTP ${response.status}; outcome unchanged`);
  const body=await response.json();
  if(body.errors?.length || !body.data)throw Error('Devnet GraphQL rejected the read');
  return body.data;
}
function identity(data) {
  if(data.daemonStatus?.chainId!==CHAIN_ID)throw Error('Unexpected Devnet chain identity');
  if(data.daemonStatus.syncStatus!=='SYNCED')throw Error('Devnet node is not synchronized');
}
export function proofFromChain(blocks,hash) {
  if(!Array.isArray(blocks)||!blocks.length)throw Error('Empty chain evidence');
  for(const b of blocks)if(!/^[0-9]+$/.test(String(b.protocolState?.consensusState?.blockHeight)))throw Error('Malformed chain height');
  const chain=blocks.map(b=>({...b,height:Number(b.protocolState.consensusState.blockHeight)})).sort((a,b)=>b.height-a.height);
  for(let i=0;i<chain.length;i++) {
    const b=chain[i];
    if(!Number.isSafeInteger(b.height)||b.height<0||!b.stateHash||!Array.isArray(b.transactions?.zkappCommands))throw Error('Malformed chain evidence');
    if(i && (chain[i-1].height!==b.height+1 || chain[i-1].protocolState.previousStateHash!==b.stateHash))throw Error('Disconnected chain evidence');
  }
  const found=chain.flatMap(b=>b.transactions.zkappCommands.filter(t=>t.hash===hash).map(t=>({b,t})));
  if(found.length>1)throw Error('Duplicate transaction evidence');
  if(!found.length)return {hash,status:'unknown'};
  const {b,t}=found[0];
  if(t.failureReason!==null && !Array.isArray(t.failureReason))throw Error('Missing application status');
  return {hash,status:t.failureReason?.length?'failed':'applied',confirmations:chain[0].height-b.height,height:b.height,blockHash:b.stateHash};
}
export class Devnet {
  constructor(read=request) { this.read=read; Mina.setActiveInstance(Mina.Network({networkId:'devnet',mina:ENDPOINT,enforceTransactionLimits:true})); }
  async health(sender) {
    const data=await this.read(`query($pk:PublicKey!){ ${daemon}
      account(publicKey:$pk){nonce inferredNonce balance{total}}
      pooledZkappCommands(publicKey:$pk){hash} pooledUserCommands(publicKey:$pk){hash} }`,{pk:sender});
    identity(data);
    if(!data.account)throw Error('Fund the Devnet sender first');
    const nonce=Number(data.account.nonce);
    if(!Number.isInteger(nonce)||nonce<0||nonce>=2**32)throw Error('Invalid network nonce');
    return {chainId:CHAIN_ID,nonce,balance:BigInt(data.account.balance.total),
      poolBusy:nonce!==Number(data.account.inferredNonce)||data.pooledZkappCommands.length>0||data.pooledUserCommands.length>0};
  }
  async prepareAccounts(plan,index) {
    for(const row of [{address:plan.sender,create:false},...plan.batches[index].recipients]) {
      const {account,error}=await fetchAccount({publicKey:PublicKey.fromBase58(row.address)},ENDPOINT);
      if(error && !(row.create && error.statusCode===404))throw Error('Unable to fetch recipient/sender account');
      if(row.create && account)throw Error('Recipient already exists; review creation fees');
      if(!row.create && !account)throw Error('Recipient account does not exist');
    }
  }
  async submit(json) {
    // No send retry here. Any rejection or lost response stays reserved until
    // exact hash evidence can be obtained from the chain.
    // Do NOT use o1js Transaction.send(): its shared GraphQL client retries
    // mutations after transport errors. Exactly one HTTP request is allowed.
    const result=await request(sendZkappQuery(json));
    const tx=result.sendZkapp?.zkapp;
    if(!tx?.hash || tx.failureReason?.length)throw Error('Submission not acknowledged; retained as unknown');
    return {hash:tx.hash};
  }
  async observe(hash) {
    const data=await this.read(`{${daemon} bestChain(maxLength:290){stateHash
      protocolState{previousStateHash consensusState{blockHeight}}
      transactions{zkappCommands{hash failureReason{index failures}}}}}`);
    identity(data);
    const proof=proofFromChain(data.bestChain,hash);
    if(proof.status!=='unknown')return proof;
    // Pool absence is not failure and never authorizes another submission.
    const pool=await this.read(`{${daemon} pooledZkappCommands{hash}}`); identity(pool);
    return {hash,status:pool.pooledZkappCommands.some(t=>t.hash===hash)?'pending':'unknown'};
  }
}
