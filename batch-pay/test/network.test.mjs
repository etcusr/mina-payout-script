import test from 'node:test';
import assert from 'node:assert/strict';
import { proofFromChain, Devnet, CHAIN_ID } from '../network.mjs';

const block=(h,hash,prev,commands=[])=>({stateHash:hash,protocolState:{previousStateHash:prev,consensusState:{blockHeight:String(h)}},transactions:{zkappCommands:commands}});
const chain=()=>[block(4,'b4','b3'),block(3,'b3','b2'),block(2,'b2','b1'),block(1,'b1','b0',[{hash:'tx',failureReason:null}])];
test('chain proof counts descendants and validates connected ancestry regardless of order',()=>{
  assert.deepEqual(proofFromChain(chain(),'tx'),{hash:'tx',status:'applied',confirmations:3,height:1,blockHash:'b1'});
  assert.equal(proofFromChain(chain().reverse(),'tx').confirmations,3);
});
test('broken ancestry, duplicate heights and duplicate transaction evidence are rejected',()=>{
  const broken=chain(); broken[1].protocolState.previousStateHash='other';
  assert.throws(()=>proofFromChain(broken,'tx'),/chain/i);
  assert.throws(()=>proofFromChain([...chain(),chain()[0]],'tx'),/chain/i);
  const duplicate=chain(); duplicate[0].transactions.zkappCommands=duplicate[3].transactions.zkappCommands;
  assert.throws(()=>proofFromChain(duplicate,'tx'),/duplicate/i);
});
test('failure is not Applied; absence is unknown, not permission to resend',()=>{
  const failed=chain(); failed[3].transactions.zkappCommands[0].failureReason=[{index:1,failures:['Cancelled']}];
  assert.equal(proofFromChain(failed,'tx').status,'failed');
  assert.deepEqual(proofFromChain(chain(),'absent'),{hash:'absent',status:'unknown'});
});
test('missing application status and malformed block heights cannot confirm',()=>{
  const missing=chain();delete missing[3].transactions.zkappCommands[0].failureReason;
  assert.throws(()=>proofFromChain(missing,'tx'),/status/);
  for(const height of ['','NaN','1.5',null]) {
    const malformed=chain();malformed[3].protocolState.consensusState.blockHeight=height;
    assert.throws(()=>proofFromChain(malformed,'tx'),/chain/i);
  }
});
test('every health read pins Devnet chain and sync status',async()=>{
  const data={daemonStatus:{chainId:CHAIN_ID,syncStatus:'SYNCED'},account:{nonce:'0',inferredNonce:'0',balance:{total:'100'}},pooledZkappCommands:[],pooledUserCommands:[]};
  const adapter=new Devnet(async()=>data);
  assert.equal((await adapter.health('B62test')).balance,100n);
  data.daemonStatus.chainId='mainnet';
  await assert.rejects(()=>adapter.health('B62test'),/chain/i);
  data.daemonStatus.chainId=CHAIN_ID; data.daemonStatus.syncStatus='CATCHUP';
  await assert.rejects(()=>adapter.health('B62test'),/sync/i);
});

test('real submit transport is attempted exactly once after a lost HTTP response',async()=>{
  const {Mina,PrivateKey}=await import('o1js');
  const {makePlan,buildBatch}=await import('../builder.mjs');
  const local=await Mina.LocalBlockchain({proofsEnabled:false});Mina.setActiveInstance(local);
  const [sender,to]=local.testAccounts;
  const plan=makePlan({sender:sender.toBase58(),recipients:[{address:to.toBase58(),amount:'1'}],batchSize:1,fee:'1',chainId:CHAIN_ID,operation:'test'});
  const tx=await buildBatch(plan,0,0,sender.key);
  const adapter=new Devnet();
  const original=globalThis.fetch;let calls=0;
  globalThis.fetch=async()=>{calls++;throw new TypeError('fetch failed');};
  try{await assert.rejects(()=>adapter.submit(tx.toJSON()));assert.equal(calls,1);}
  finally{globalThis.fetch=original;}
});
