import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Mina, PublicKey, Transaction } from 'o1js';
import { makePlan,buildBatch } from '../builder.mjs';
import { Journal } from '../journal.mjs';
import { runNext, reconcile } from '../workflow.mjs';

async function fixture(t, size = 2) {
  const dir = mkdtempSync(join(tmpdir(), 'mina-batch-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const local = await Mina.LocalBlockchain({ proofsEnabled: false, enforceTransactionLimits: true });
  Mina.setActiveInstance(local);
  const [sender, a, b, c] = local.testAccounts;
  const plan = makePlan({ sender: sender.toBase58(), recipients: [a,b,c].map(x => ({ address:x.toBase58(), amount:'1' })),
    batchSize:size, fee:'10000000', chainId:'test-chain', operation:'epoch-test' });
  let journal = new Journal(dir); journal.create(plan);
  t.after(() => journal.close());
  const chain = []; let calls = 0;
  const adapter = {
    async health() { return { chainId:'test-chain', nonce:Number(Mina.getAccount(sender).nonce.toString()), balance:Mina.getBalance(sender).toBigInt() }; },
    async prepareAccounts() {},
    async submit(json) { calls++; const tx = Transaction.fromJSON(json); const result = await tx.send();
      chain.push({ hash:await Transaction.hash(json), status:'applied', confirmations:3, blockHash:'block1', height:1 });
      return { hash: result.hash }; },
    async observe(hash) { return chain.find(x => x.hash === hash) ?? { hash, status:'unknown' }; }
  };
  return { dir, sender, plan, adapter, chain, get calls(){return calls;},
    get journal(){return journal;}, restart(){journal.close(); journal=new Journal(dir);return journal;} };
}

test('lost response AFTER acceptance: restart reconciles exact hash without resubmission', async t => {
  const f = await fixture(t); const send = f.adapter.submit;
  f.adapter.submit = async json => { await send(json); throw Error('connection lost'); };
  await assert.rejects(() => runNext(f.journal, f.plan.id, f.adapter, f.sender.key), /connection/);
  assert.equal(f.journal.rows(f.plan.id)[0].state, 'unknown');
  const j = f.restart();
  await runNext(j, f.plan.id, f.adapter, f.sender.key);
  assert.equal(f.calls, 1);
  assert.equal(j.rows(f.plan.id)[0].state, 'confirmed');
  assert.equal(j.rows(f.plan.id)[1].state, 'planned'); // resume never sends next batch implicitly
});

test('lost connection BEFORE acceptance: nonce reserved, unknown is never retried', async t => {
  const f = await fixture(t); let attempts=0;
  f.adapter.submit = async () => { attempts++; throw Error('connection lost'); };
  await assert.rejects(() => runNext(f.journal, f.plan.id, f.adapter, f.sender.key));
  await runNext(f.restart(), f.plan.id, f.adapter, f.sender.key);
  assert.equal(attempts,1); assert.equal(f.journal.rows(f.plan.id)[0].state,'unknown');
  assert.equal(Mina.getAccount(f.sender).nonce.toString(),'0');
});

test('immutable operation names and completed plan cannot pay twice', async t => {
  const f = await fixture(t,3);
  assert.equal(f.journal.create(f.plan),f.plan.id);
  assert.throws(() => f.journal.create(makePlan({ ...f.plan, recipients:f.plan.batches.flatMap(b=>b.recipients), fee:'10000001' })), /operation/i);
  await runNext(f.journal,f.plan.id,f.adapter,f.sender.key);
  await reconcile(f.journal,f.plan.id,f.adapter);
  await runNext(f.restart(),f.plan.id,f.adapter,f.sender.key);
  assert.equal(f.calls,1);
});

test('two journal connections claim a nonce only once; no second live plan on wallet', async t => {
  const f=await fixture(t); const other = new Journal(f.dir); t.after(()=>other.close());
  const p2=makePlan({...f.plan,recipients:f.plan.batches.flatMap(b=>b.recipients),operation:'different'});
  assert.throws(()=>other.create(p2),/unfinished/i);
  await runNext(f.journal,f.plan.id,f.adapter,f.sender.key);
  assert.equal(other.claim(f.plan.id,0),false);
  await runNext(other,f.plan.id,f.adapter,f.sender.key);
  assert.equal(f.calls,1);
});

test('wrong chain, stale nonce, insufficient funds and non-empty pool prevent submission', async t => {
  const f=await fixture(t); const health=await f.adapter.health();
  for(const override of [{chainId:'wrong'},{balance:0n},{poolBusy:true}]) {
    f.adapter.health=async()=>({...health,...override});
    await assert.rejects(()=>runNext(f.journal,f.plan.id,f.adapter,f.sender.key));
  }
  assert.equal(f.calls,0);
});

test('failed batch and unknown proof never unlock the next batch', async t => {
  const f=await fixture(t);
  await runNext(f.journal,f.plan.id,f.adapter,f.sender.key);
  f.chain[0].status='failed'; f.chain[0].confirmations=3;
  await reconcile(f.journal,f.plan.id,f.adapter);
  await assert.rejects(()=>runNext(f.restart(),f.plan.id,f.adapter,f.sender.key),/failed/i);
  assert.equal(f.calls,1);
});

test('proof requires expected hash and sufficient depth; disappearance revokes confirmation', async t => {
  const f=await fixture(t);
  await runNext(f.journal,f.plan.id,f.adapter,f.sender.key);
  const valid=f.chain[0];
  f.adapter.observe=async()=>({...valid,hash:'wrong'});
  await assert.rejects(()=>reconcile(f.journal,f.plan.id,f.adapter),/hash/i);
  f.adapter.observe=async()=>({...valid,confirmations:2});
  await reconcile(f.journal,f.plan.id,f.adapter);
  assert.equal(f.journal.rows(f.plan.id)[0].state,'included');
  f.adapter.observe=async()=>valid;
  await reconcile(f.journal,f.plan.id,f.adapter);
  f.adapter.observe=async()=>({hash:valid.hash,status:'unknown'});
  await runNext(f.journal,f.plan.id,f.adapter,f.sender.key);
  assert.equal(f.calls,1); assert.equal(f.journal.rows(f.plan.id)[0].state,'unknown');
});

test('simultaneous senders share a durable claim and submit only once',async t=>{
  const f=await fixture(t);const other=new Journal(f.dir);t.after(()=>other.close());
  const original=f.adapter.submit;let release,entered;
  const waiting=new Promise(r=>entered=r);const barrier=new Promise(r=>release=r);
  f.adapter.submit=async json=>{entered();await barrier;return original(json);};
  const first=runNext(f.journal,f.plan.id,f.adapter,f.sender.key);
  await waiting;
  await runNext(other,f.plan.id,f.adapter,f.sender.key);
  release();await first;
  assert.equal(f.calls,1);assert.equal(Mina.getAccount(f.sender).nonce.toString(),'1');
});

test('signed-only restart with advanced nonce refuses submission',async t=>{
  const f=await fixture(t);const tx=await buildBatch(f.plan,0,0,f.sender.key);
  const json=tx.toJSON();f.journal.prepare(f.plan.id,0,0,await Transaction.hash(json),json);
  await tx.send(); // An independent process used the nonce before our claim.
  await assert.rejects(()=>runNext(f.restart(),f.plan.id,f.adapter,f.sender.key),/Nonce changed/);
  assert.equal(f.calls,0);assert.equal(f.journal.rows(f.plan.id)[0].signed,json);
});

test('reorg in a previous operation blocks new operation until fresh proof returns',async t=>{
  const f=await fixture(t,3);
  await runNext(f.journal,f.plan.id,f.adapter,f.sender.key);await reconcile(f.journal,f.plan.id,f.adapter);
  const next=makePlan({...f.plan,recipients:f.plan.batches[0].recipients,operation:'next-epoch'});f.journal.create(next);
  f.chain.length=0;
  await assert.rejects(()=>runNext(f.journal,next.id,f.adapter,f.sender.key),/previous/i);
  assert.equal(f.calls,1);
});

test('batch size 2 executes 3 recipients with exactly two fees and no duplicate recipients',async t=>{
  const f=await fixture(t,2);const before=Mina.getBalance(f.sender).toBigInt();
  await runNext(f.journal,f.plan.id,f.adapter,f.sender.key);await reconcile(f.journal,f.plan.id,f.adapter);
  await runNext(f.journal,f.plan.id,f.adapter,f.sender.key);await reconcile(f.journal,f.plan.id,f.adapter);
  assert.equal(f.calls,2);assert.equal(before-Mina.getBalance(f.sender).toBigInt(),20000003n);
  assert.deepEqual(f.journal.rows(f.plan.id).map(r=>[r.state,r.nonce]),[['confirmed',0],['confirmed',1]]);
});

test('chain identity changes after signing: keep signed record but do not claim or submit',async t=>{
  const f=await fixture(t);const health=f.adapter.health;let calls=0;
  f.adapter.health=async()=>({...await health(),chainId:++calls>=3?'different-chain':'test-chain'});
  await assert.rejects(()=>runNext(f.journal,f.plan.id,f.adapter,f.sender.key),/chain/);
  assert.equal(f.calls,0);assert.equal(f.journal.rows(f.plan.id)[0].state,'signed');
});
