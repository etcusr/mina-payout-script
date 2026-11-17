import { Transaction } from 'o1js';
import { buildBatch } from './builder.mjs';

function checkedHealth(plan,h) {
  if(h.chainId!==plan.chainId)throw Error('Wrong chain identity');
  if(!Number.isInteger(h.nonce) || h.nonce<0)throw Error('Invalid chain nonce');
}
export async function reconcile(journal,id,adapter) {
  const plan=journal.plan(id);
  const health=await adapter.health(plan.sender); checkedHealth(plan,health);
  for(const row of journal.rows(id)) {
    if(['planned','signed'].includes(row.state)) continue;
    if(await Transaction.hash(row.signed)!==row.hash)throw Error('Stored transaction hash mismatch');
    const proof=await adapter.observe(row.hash);
    if(proof.hash!==row.hash)throw Error('Proof hash mismatch');
    if(!['unknown','pending','applied','failed'].includes(proof.status))throw Error('Unknown proof status');
    if(['applied','failed'].includes(proof.status) &&
      (!Number.isInteger(proof.confirmations) || proof.confirmations<0 || !proof.blockHash || !Number.isInteger(proof.height)))
      throw Error('Incomplete inclusion proof');
    journal.observe(id,row.idx,proof);
  }
  return journal.rows(id);
}
export async function runNext(journal,id,adapter,key) {
  const plan=journal.plan(id);
  for(const previous of journal.list()) {
    if(previous.id===id || previous.sender!==plan.sender)continue;
    const previousPlan=journal.plan(previous.id);
    if(previousPlan.chainId!==plan.chainId)continue;
    const proofs=await reconcile(journal,previous.id,adapter);
    if(proofs.some(r=>r.state!=='confirmed'))throw Error('A previous operation lacks fresh confirmation; sending is blocked');
  }
  // A recovery invocation reconciles the interrupted batch, without implicitly
  // sending a new one. The CLI explicitly advances only after confirmed rows.
  let rows=journal.rows(id);
  const wasPending=rows.some(r=>['unknown','submitted','included'].includes(r.state));
  await reconcile(journal,id,adapter);
  rows=journal.rows(id);
  if(rows.some(r=>r.state==='failed'))throw Error('Batch failed in chain; manual review required, no retry');
  if(wasPending || rows.some(r=>['unknown','submitted','included'].includes(r.state)))return rows;
  const row=rows.find(r=>r.state!=='confirmed');
  if(!row)return rows;
  await adapter.prepareAccounts(plan,row.idx);
  const health=await adapter.health(plan.sender); checkedHealth(plan,health);
  if(health.poolBusy)throw Error('Sender already has pending transactions');
  const remaining=rows.filter(r=>r.state!=='confirmed').reduce((sum,r)=>sum+BigInt(plan.fee)+plan.batches[r.idx].recipients.reduce((s,p)=>s+BigInt(p.amount)+(p.create?1000000000n:0n),0n),0n);
  if(health.balance<remaining)throw Error('Insufficient balance for remaining batches including fees');
  if(row.state==='planned') {
    const tx=await buildBatch(plan,row.idx,health.nonce,key);
    const signed=tx.toJSON(); const hash=await Transaction.hash(signed);
    journal.prepare(id,row.idx,health.nonce,hash,signed);
  }
  const ready=journal.rows(id)[row.idx];
  const finalHealth=await adapter.health(plan.sender);checkedHealth(plan,finalHealth);
  if(ready.nonce!==finalHealth.nonce)throw Error('Nonce changed; signed transaction retained for review');
  if(finalHealth.poolBusy || finalHealth.balance<remaining)throw Error('Available funds or pending transactions changed; signed batch retained');
  if(await Transaction.hash(ready.signed)!==ready.hash)throw Error('Stored transaction hash mismatch');
  if(!journal.claim(id,row.idx))return journal.rows(id);
  const result=await adapter.submit(ready.signed);
  if(result.hash!==ready.hash)throw Error('Submission hash mismatch; outcome unknown');
  journal.observe(id,row.idx,{hash:ready.hash,status:'pending'});
  return journal.rows(id);
}
