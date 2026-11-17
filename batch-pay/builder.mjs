import { createHash } from 'node:crypto';
import { AccountUpdate, Mina, PublicKey, Bool } from 'o1js';

// Pinned o1js 3.1.0 internal validator: run the actual Mesa segment check BEFORE
// reserving/submitting. Covered by LocalBlockchain tests with limits enabled.
const { verifyTransactionLimits } = await import(new URL(
  './lib/mina/v1/transaction-validation.js', import.meta.resolve('o1js')));

export const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
export function nano(value) {
  if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value) || BigInt(value) > (1n << 64n) - 1n)
    throw Error('Amount/fee must be a positive UInt64 nanomina string');
  return BigInt(value);
}
function address(value) {
  if (typeof value !== 'string' || PublicKey.fromBase58(value).toBase58() !== value)
    throw Error('Invalid public key');
  return value;
}
export function makePlan({ sender, recipients, batchSize, fee, chainId, operation }) {
  address(sender); nano(fee);
  if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 30)
    throw Error('Batch size must be an integer from 1 to 30 (Mesa)');
  if (typeof chainId !== 'string' || !chainId.length ||
      typeof operation !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(operation))
    throw Error('Explicit chain identity and stable operation name required');
  if (!Array.isArray(recipients) || !recipients.length) throw Error('Recipients required');
  const seen = new Set([sender]);
  const rows = recipients.map(r => {
    address(r.address); nano(r.amount);
    if (seen.has(r.address)) throw Error('Duplicate recipient or self-payment');
    seen.add(r.address);
    if(r.create!==undefined && typeof r.create!=='boolean') throw Error('create must be an explicit boolean');
    return { address: r.address, amount: r.amount, create: r.create===true };
  });
  const batches = [];
  for (let i = 0; i < rows.length; i += batchSize) batches.push({ recipients: rows.slice(i, i + batchSize) });
  const total = rows.reduce((a, r) => a + nano(r.amount), 0n);
  const creationCost=BigInt(rows.filter(r=>r.create).length)*1000000000n;
  const cost = total + BigInt(batches.length) * nano(fee) + creationCost;
  if (cost > (1n << 64n) - 1n) throw Error('Plan total overflow');
  const plan = { version: 1, network: 'devnet', chainId, operation, sender, batchSize,
    fee, total: total.toString(), creationCost:creationCost.toString(), cost: cost.toString(), batches };
  return { ...plan, id: digest(plan) };
}
export function validatePlan(plan) {
  const rebuilt = makePlan({ ...plan, recipients: plan.batches.flatMap(b => b.recipients) });
  if (JSON.stringify(rebuilt) !== JSON.stringify(plan)) throw Error('Immutable plan mismatch');
  return plan;
}
export async function buildBatch(plan, index, nonce, key) {
  validatePlan(plan);
  if (!Number.isInteger(index) || !plan.batches[index]) throw Error('Invalid batch index');
  if (!Number.isInteger(nonce) || nonce < 0 || nonce >= 2 ** 32 - 1) throw Error('Invalid nonce');
  if (key.toPublicKey().toBase58() !== plan.sender) throw Error('Wrong signer');
  if (Mina.getNetworkId() !== 'devnet') throw Error('Mainnet submission is not enabled');
  const sender = PublicKey.fromBase58(plan.sender);
  const rows = plan.batches[index].recipients;
  for (const r of rows) {
    const exists=Mina.hasAccount(PublicKey.fromBase58(r.address));
    if(exists===r.create)throw Error(r.create?'Account already exists':'Recipient account must exist');
  }
  const creations=rows.filter(r=>r.create).length;
  if(creations && Mina.getNetworkConstants().accountCreationFee.toBigInt()!==1000000000n)
    throw Error('Network account creation fee changed');
  const tx = await Mina.transaction({ sender, fee: plan.fee, nonce,
    memo: `batch:${plan.id.slice(0, 16)}:${index}` }, async () => {
    const debit = AccountUpdate.createSigned(sender);
    // Same ledger fee mechanism as fundNewAccount, combined with the existing
    // debit to keep 30 recipients within 16 segments including the fee payer.
    if(creations)debit.balance.subInPlace(BigInt(creations)*1000000000n);
    debit.account.isNew.requireEquals(Bool(false));
    for (const r of rows) {
      const credit = debit.send({ to: PublicKey.fromBase58(r.address), amount: nano(r.amount) });
      credit.account.isNew.requireEquals(Bool(r.create));
    }
  });
  verifyTransactionLimits(tx.transaction);
  tx.sign([key]);
  return tx;
}
