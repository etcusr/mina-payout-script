import test from 'node:test';
import assert from 'node:assert/strict';
import { Mina, PrivateKey, PublicKey, Transaction } from 'o1js';
import { buildUnsigned, signUnsigned, validateUnsigned, handle } from '../mainnet-worker.mjs';
const {verifyZkappCommandSignature,signZkappCommand} = await import(new URL('./mina-signer/src/sign-zkapp-command.js',import.meta.resolve('o1js')));
async function fixture(count = 2) {
  const local = await Mina.LocalBlockchain({proofsEnabled:false,enforceTransactionLimits:true}); Mina.setActiveInstance(local);
  const sender=local.testAccounts[0];
  const recipients=Array.from({length:count},()=> {const k=PrivateKey.random();local.addAccount(k.toPublicKey(),'1000000000');return {recipient:k.toPublicKey().toBase58(),amount_nano:'1'};});
  const expected={network:'mainnet',wallet:sender.toBase58(),nonce:0,fee_nano:'10000000',memo:'batch_test',recipients};
  const unsigned=await buildUnsigned(expected,{fetchAccounts:false});
  return {expected,unsigned,sender};
}
test('real generated key signs both authorizations on mainnet only; hash roundtrip',async()=>{
  const {expected,unsigned,sender}=await fixture();
  const {signed,tx_hash}=await signUnsigned(unsigned,expected,sender.key.toBase58());
  assert.equal(verifyZkappCommandSignature(JSON.parse(signed),expected.wallet,'mainnet'),true);
  assert.equal(verifyZkappCommandSignature(JSON.parse(signed),expected.wallet,'devnet'),false);
  assert.equal(tx_hash,await Transaction.hash(signed));
  const verified=await handle({action:'verify',network:'mainnet',signed,expected});
  assert.equal(verified.tx_hash,tx_hash);assert.match(verified.query,/sendZkapp/);
  assert.equal(validateUnsigned(signed,expected,true).accountUpdates.length,3);
});
test('all semantic and hidden body alterations are refused before signing',async()=>{
  const {expected,unsigned,sender}=await fixture();
  const changes=[t=>t.feePayer.body.nonce='1',t=>t.feePayer.body.fee='1',t=>t.memo='bad',t=>t.accountUpdates.pop(),
    t=>t.accountUpdates[0].body.balanceChange.magnitude='1',t=>t.accountUpdates[1].body.publicKey=expected.wallet,
    t=>t.accountUpdates[1].body.tokenId='1',t=>t.accountUpdates[1].body.incrementNonce=true,
    t=>t.accountUpdates[1].body.update.delegate=expected.wallet,t=>t.accountUpdates[1].body.preconditions.account.isNew=true,
    t=>t.accountUpdates[1].body.events=[['1']],t=>t.accountUpdates[1].body.hidden='x',t=>t.hidden='x'];
  for(const change of changes){let t=JSON.parse(unsigned);change(t);await assert.rejects(()=>signUnsigned(t,expected,sender.key.toBase58()));}
  await assert.rejects(()=>signUnsigned(unsigned,expected,PrivateKey.random().toBase58()));
  await assert.rejects(()=>handle({action:'sign',network:'devnet',unsigned,expected,privateKey:sender.key.toBase58()}));
  await assert.rejects(()=>signUnsigned(unsigned,{...expected,network:'devnet'},sender.key.toBase58()));
});
test('signature changes and altered signed body are refused after signing',async()=>{
  const {expected,unsigned,sender}=await fixture(); const {signed}=await signUnsigned(unsigned,expected,sender.key.toBase58());
  for(const change of [t=>t.accountUpdates[0].authorization.signature=t.feePayer.authorization.slice(0,-1)+'1',t=>t.accountUpdates[1].body.balanceChange.magnitude='2']){
    const t=JSON.parse(signed);change(t);assert.throws(()=>validateUnsigned(t,expected,true));
  }
});
test('30 existing recipients fit actual transaction limits; missing accounts and 31 refused',async()=>{
  const {expected,unsigned,sender}=await fixture(30);await signUnsigned(unsigned,expected,sender.key.toBase58());
  await assert.rejects(()=>buildUnsigned({...expected,recipients:[...expected.recipients,expected.recipients[0]]},{fetchAccounts:false}));
  await assert.rejects(()=>buildUnsigned({...expected,recipients:[{recipient:PrivateKey.random().toPublicKey().toBase58(),amount_nano:'1'}]},{fetchAccounts:false}));
});
test('unsigned transfer executes atomically on local ledger with local devnet signature',async()=>{
  const {expected,unsigned,sender}=await fixture();
  const balance=Mina.getBalance(sender).toBigInt();
  const tx=Transaction.fromJSON(signZkappCommand(JSON.parse(unsigned),sender.key.toBase58(),'devnet'));
  await tx.send();
  assert.equal(Mina.getBalance(sender).toBigInt(),balance-10000002n);
  assert.equal(Mina.getAccount(sender).nonce.toBigint(),1n);
  for(const r of expected.recipients)
    assert.equal(Mina.getBalance(PublicKey.fromBase58(r.recipient)).toBigInt(),1000000001n);
  await assert.rejects(()=>Transaction.fromJSON(tx.toJSON()).send());
  assert.equal(Mina.getBalance(sender).toBigInt(),balance-10000002n);
});
test('expected packet rejects nonexact amounts, fee, nonce, duplicates, and oversized memo',async()=>{
  const {expected,unsigned,sender}=await fixture();
  const invalid=[{...expected,nonce:'0'},{...expected,nonce:-1},{...expected,nonce:2**32},
    {...expected,fee_nano:10000000},{...expected,fee_nano:'01'},
    {...expected,memo:'a'.repeat(33)}, {...expected,recipients:[expected.recipients[0],expected.recipients[0]]}];
  for(const amount of ['0','-1','01','1.5','1e9','18446744073709551616',1])
    invalid.push({...expected,recipients:[{...expected.recipients[0],amount_nano:amount}]});
  for(const packet of invalid) await assert.rejects(()=>signUnsigned(unsigned,packet,sender.key.toBase58()));
});
