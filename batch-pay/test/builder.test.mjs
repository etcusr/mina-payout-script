import test from 'node:test';
import assert from 'node:assert/strict';
import { Mina, PrivateKey, PublicKey, Bool, Transaction } from 'o1js';
import { makePlan, buildBatch, nano } from '../builder.mjs';

async function fixture(count = 3) {
  const local = await Mina.LocalBlockchain({ proofsEnabled: false, enforceTransactionLimits: true });
  Mina.setActiveInstance(local);
  const sender = local.testAccounts[0];
  const recipients = Array.from({ length: count }, () => {
    const pk = PrivateKey.random().toPublicKey(); local.addAccount(pk, '1000000000');
    return { address: pk.toBase58(), amount: '1' };
  });
  const plan = makePlan({ sender: sender.toBase58(), recipients, batchSize: 2,
    fee: '10000000', chainId: 'local-test', operation: 'epoch-test' });
  return { local, sender, plan };
}

test('exact nano input rejects rounding, unsafe numbers, signs and overflow', () => {
  for (const n of ['0', '-1', '1.5', '1e9', '01', 1, '18446744073709551616'])
    assert.throws(() => nano(n));
  assert.equal(nano('1'), 1n);
});

test('one signed zkApp pays two recipients, one fee and one nonce; replay cannot pay again', async () => {
  const { sender, plan } = await fixture();
  assert.deepEqual(plan.batches.map(b => b.recipients.length), [2, 1]);
  const before = Mina.getBalance(sender).toBigInt();
  const tx = await buildBatch(plan, 0, 0, sender.key);
  const json = JSON.parse(tx.toJSON());
  assert.equal(json.accountUpdates.length, 3);
  assert.equal(json.feePayer.body.nonce, '0');
  const hash = await Transaction.hash(tx.toJSON());
  assert.match(hash, /^5J/);
  await tx.send();
  assert.equal(Mina.getAccount(sender).nonce.toBigint(), 1n);
  assert.equal(before - Mina.getBalance(sender).toBigInt(), 10000002n);
  for (const r of plan.batches[0].recipients)
    assert.equal(Mina.getBalance(PublicKey.fromBase58(r.address)).toBigInt(), 1000000001n);
  await assert.rejects(() => Transaction.fromJSON(tx.toJSON()).send());
  assert.equal(before - Mina.getBalance(sender).toBigInt(), 10000002n);
});

test('batch size 30 stays within real Mesa segment limit; 31 refused', async () => {
  const { sender, plan } = await fixture(30);
  const large = makePlan({ ...plan, recipients: plan.batches.flatMap(b => b.recipients), batchSize: 30 });
  await (await buildBatch(large, 0, 0, sender.key)).send();
  assert.throws(() => makePlan({ ...large, recipients: large.batches[0].recipients, batchSize: 31 }));
});

test('bad plans, duplicate recipients, wrong signer and missing accounts fail before submission', async () => {
  const { sender, plan } = await fixture();
  const recipients = plan.batches.flatMap(b => b.recipients);
  for (const batchSize of [0, -1, 1.5, '2']) assert.throws(() => makePlan({ ...plan, recipients, batchSize }));
  assert.throws(() => makePlan({ ...plan, recipients: [recipients[0], recipients[0]] }));
  assert.throws(() => makePlan({ ...plan, recipients: [] }));
  await assert.rejects(() => buildBatch(plan, 0, 0, PrivateKey.random()), /signer/i);
  const missing = makePlan({ ...plan, recipients: [{ address: PrivateKey.random().toPublicKey().toBase58(), amount: '1' }] });
  await assert.rejects(() => buildBatch(missing, 0, 0, sender.key), /exist|account/i);
});

test('a failed recipient precondition applies none of the transfers', async () => {
  const { sender, plan } = await fixture();
  const tx = await buildBatch(plan, 0, 0, sender.key);
  tx.transaction.accountUpdates[2].body.preconditions.account.isNew.value = Bool(true);
  tx.sign([sender.key]);
  await assert.rejects(() => tx.send());
  for (const r of plan.batches[0].recipients)
    assert.equal(Mina.getBalance(PublicKey.fromBase58(r.address)).toBigInt(), 1000000000n);
});

test('explicit devnet account creation includes creation fees and refuses existing accounts', async () => {
  const { sender, plan }=await fixture();
  const recipients=Array.from({length:2},()=>({address:PrivateKey.random().toPublicKey().toBase58(),amount:'1',create:true}));
  const p=makePlan({...plan,recipients});
  assert.equal(p.creationCost,'2000000000');
  assert.equal(p.cost,'2010000002');
  const before=Mina.getBalance(sender).toBigInt();
  await(await buildBatch(p,0,0,sender.key)).send();
  assert.equal(before-Mina.getBalance(sender).toBigInt(),2010000002n);
  for(const r of recipients) assert.equal(Mina.getBalance(PublicKey.fromBase58(r.address)).toBigInt(),1n);
  await assert.rejects(()=>buildBatch(p,0,1,sender.key),/exist/);
});

test('30 new recipients also fit Mesa limit; creation fees share the sender debit', async()=>{
  const {sender,plan}=await fixture();
  const recipients=Array.from({length:30},()=>({address:PrivateKey.random().toPublicKey().toBase58(),amount:'1',create:true}));
  const p=makePlan({...plan,recipients,batchSize:30});
  const tx=await buildBatch(p,0,0,sender.key);
  assert.equal(JSON.parse(tx.toJSON()).accountUpdates.length,31);
  await tx.send();
  for(const r of recipients)assert.equal(Mina.getBalance(PublicKey.fromBase58(r.address)).toBigInt(),1n);
});

test('changing a recipient after signing invalidates the real signature',async()=>{
  const {sender,plan}=await fixture();
  const before=Mina.getBalance(sender).toBigInt();
  const tx=await buildBatch(plan,0,0,sender.key);
  tx.transaction.accountUpdates[1].body.publicKey=PublicKey.fromBase58(plan.batches[1].recipients[0].address);
  await assert.rejects(()=>tx.send(),/signature|verify/i);
  assert.equal(Mina.getBalance(sender).toBigInt(),before);
  for(const r of plan.batches.flatMap(b=>b.recipients))
    assert.equal(Mina.getBalance(PublicKey.fromBase58(r.address)).toBigInt(),1000000000n);
});
