// Explicitly invoked qualification only; excluded from `npm test`.
// Uses the isolated funded Devnet fixtures, never config.yml or payout state.
import assert from 'node:assert/strict';
import { readFileSync,writeFileSync,existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { PrivateKey,fetchAccount,PublicKey } from 'o1js';
import { Journal } from '../journal.mjs';
import { Devnet,CHAIN_ID,ENDPOINT } from '../network.mjs';
import { makePlan } from '../builder.mjs';
import { runNext,reconcile } from '../workflow.mjs';

const phase=process.argv[2];
if(!['lost-response','verify'].includes(phase))throw Error('Choose lost-response or verify explicitly');
const directory=fileURLToPath(new URL('../.state/devnet/',import.meta.url));
const journal=new Journal(directory);
const adapter=new Devnet();
const key=PrivateKey.fromBase58(JSON.parse(readFileSync(join(directory,'key.json'),'utf8')).privateKey);
const recipients=JSON.parse(readFileSync(join(directory,'bootstrap.json'),'utf8')).map(r=>({address:r.address,amount:'1'}));
const plan=makePlan({sender:key.toPublicKey().toBase58(),recipients,batchSize:2,fee:'100000000',chainId:CHAIN_ID,operation:'devnet-lost-response-001'});
const reportPath=join(directory,'live-evidence.json');
async function balances() {
  const result={};
  for(const address of [plan.sender,...recipients.map(r=>r.address)]) {
    const {account,error}=await fetchAccount({publicKey:PublicKey.fromBase58(address)},ENDPOINT);
    if(error||!account)throw Error('Could not obtain live balances');
    result[address]=account.balance.toString();
  }
  return result;
}
try {
  for(const p of journal.list())if(p.operation==='devnet-setup-001') {
    const rows=await reconcile(journal,p.id,adapter);
    if(!rows.every(r=>r.state==='confirmed'))throw Error('Setup requires 3 confirmations first');
  }
  if(phase==='lost-response') {
    journal.create(plan);
    if(journal.rows(plan.id).some(r=>r.state!=='planned'))throw Error('Already attempted; use verify. Never create a replacement plan.');
    const before=await balances();
    const original=globalThis.fetch;let posts=0;
    globalThis.fetch=async(url,options)=>{
      if(options?.body?.includes('sendZkapp')) {
        posts++;await original(url,options);
        throw new TypeError('Injected loss of response after real Devnet POST');
      }
      return original(url,options);
    };
    try {
      await assert.rejects(()=>runNext(journal,plan.id,adapter,key),/Injected loss/);
    } finally {globalThis.fetch=original;}
    assert.equal(posts,1);assert.equal(journal.rows(plan.id)[0].state,'unknown');
    writeFileSync(reportPath,JSON.stringify({plan:plan.id,before,submissionPosts:posts,
      interruptedState:'unknown',hash:journal.rows(plan.id)[0].hash,startedAt:new Date().toISOString()},null,2)+'\n',{mode:0o600,flag:'wx'});
    console.log('One real POST; response intentionally lost; state unknown persisted. Run verify in a NEW process.');
  } else {
    if(!existsSync(reportPath))throw Error('Missing interruption evidence');
    const report=JSON.parse(readFileSync(reportPath,'utf8'));
    assert.equal(report.plan,plan.id);
    let submissions=0;adapter.submit=async()=>{submissions++;throw Error('Forbidden duplicate submission');};
    await runNext(journal,plan.id,adapter,key);
    assert.equal(submissions,0);
    const rows=journal.rows(plan.id);
    console.log(JSON.stringify(rows.map(({state,nonce,hash,observation})=>({state,nonce,hash,observation}))));
    if(!rows.every(r=>r.state==='confirmed')){console.log('Pending; no resubmission. Repeat verify after new blocks.');process.exitCode=2;}
    else {
      const after=await balances();
      for(const r of recipients)assert.equal(BigInt(after[r.address])-BigInt(report.before[r.address]),1n);
      assert.equal(BigInt(report.before[plan.sender])-BigInt(after[plan.sender]),100000002n);
      await runNext(journal,plan.id,adapter,key);assert.equal(submissions,0);
      writeFileSync(reportPath,JSON.stringify({...report,after,duplicateSubmissions:submissions,
        confirmedAt:new Date().toISOString(),proofs:rows.map(r=>JSON.parse(r.observation))},null,2)+'\n',{mode:0o600});
      console.log('PASS: two 1-nanomina credits, one fee, exact hash, 3+ confirmations, restart and repeat caused zero submissions.');
    }
  }
} finally {journal.close();}
