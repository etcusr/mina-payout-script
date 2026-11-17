// A stdin-only, bounded-lifetime signer. Never echo input or library errors.
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { AccountUpdate, Mina, PublicKey, PrivateKey, Bool, Transaction, fetchAccount } from 'o1js';
const internal = p => import(new URL(p, import.meta.resolve('o1js')));
const { verifyTransactionLimits } = await internal('./lib/mina/v1/transaction-validation.js');
const { signZkappCommand, verifyZkappCommandSignature } = await internal('./mina-signer/src/sign-zkapp-command.js');
const { sendZkappQuery } = await internal('./lib/mina/v1/graphql.js');
const { Memo } = await internal('./mina-signer/src/memo.js');

const fail = () => { throw Error('Batch payload or signer validation failed'); };
function address(x) { if (typeof x !== 'string' || PublicKey.fromBase58(x).toBase58() !== x) fail(); return x; }
function nano(x) {
  if (typeof x !== 'string' || !/^[1-9][0-9]*$/.test(x) || BigInt(x) > 2n ** 64n - 1n) fail();
  return BigInt(x);
}
export function validateExpected(p) {
  if (!p || p.network !== 'mainnet' || !Number.isInteger(p.nonce) || p.nonce < 0 || p.nonce >= 2 ** 32 - 1) fail();
  address(p.wallet); nano(p.fee_nano);
  if (typeof p.memo !== 'string' || Buffer.byteLength(p.memo, 'utf8') > 32) fail();
  if (!Array.isArray(p.recipients) || p.recipients.length < 1 || p.recipients.length > 30) fail();
  let seen = new Set([p.wallet]), total = 0n;
  for (const r of p.recipients) {
    if (!r || Object.keys(r).sort().join() !== 'amount_nano,recipient') fail();
    address(r.recipient); if (seen.has(r.recipient)) fail(); seen.add(r.recipient); total += nano(r.amount_nano);
  }
  if (total + nano(p.fee_nano) > 2n ** 64n - 1n) fail();
  return total;
}
// Independent allowlist: every body field must match an inert native-token
// update, with only the explicitly listed transfer/precondition fields changed.
function expectedBody(publicKey, magnitude, debit) {
  let body = AccountUpdate.toJSON(AccountUpdate.default(PublicKey.fromBase58(publicKey))).body;
  body.balanceChange = { magnitude: magnitude.toString(), sgn: debit ? 'Negative' : 'Positive' };
  body.callDepth = debit ? 0 : 1;
  body.preconditions.account.isNew = false;
  body.useFullCommitment = debit;
  body.authorizationKind.isSigned = debit;
  return body;
}
export function validateUnsigned(unsigned, expected, signed = false) {
  const total = validateExpected(expected);
  const tx = typeof unsigned === 'string' ? JSON.parse(unsigned) : unsigned;
  if (!tx || Object.keys(tx).sort().join() !== 'accountUpdates,feePayer,memo' ||
      !tx.feePayer || Object.keys(tx.feePayer).sort().join() !== 'authorization,body' ||
      !isDeepStrictEqual(tx.feePayer.body, {publicKey: expected.wallet, fee: expected.fee_nano, validUntil: null, nonce: String(expected.nonce)}) ||
      tx.memo !== Memo.toBase58(Memo.fromString(expected.memo)) ||
      !Array.isArray(tx.accountUpdates) || tx.accountUpdates.length !== expected.recipients.length + 1) fail();
  const bodies = [expectedBody(expected.wallet, total, true), ...expected.recipients.map(r => expectedBody(r.recipient, r.amount_nano, false))];
  for (let i = 0; i < bodies.length; i++) {
    const update = tx.accountUpdates[i];
    if (!update || Object.keys(update).sort().join() !== 'authorization,body' || !isDeepStrictEqual(update.body, bodies[i])) fail();
    const auth = update.authorization;
    if (!auth || Object.keys(auth).sort().join() !== 'proof,signature' || auth.proof !== null ||
        (signed && i === 0 ? typeof auth.signature !== 'string' : auth.signature !== null)) fail();
  }
  if (typeof tx.feePayer.authorization !== 'string') fail();
  // Deserialize only after the complete structural allowlist; parsing must not
  // erase unknown fields before they have been checked.
  verifyTransactionLimits(Transaction.fromJSON(tx).transaction);
  if (signed && !verifyZkappCommandSignature(tx, expected.wallet, expected.network)) fail();
  return tx;
}
export async function buildUnsigned(expected, { fetchAccounts = true } = {}) {
  validateExpected(expected);
  if (fetchAccounts) {
    const url = new URL(expected.endpoint);
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || !['http:', 'https:'].includes(url.protocol)) fail();
    Mina.setActiveInstance(Mina.Network({ networkId: 'mainnet', mina: url.href }));
    for (const publicKey of [expected.wallet, ...expected.recipients.map(r => r.recipient)]) {
      const result = await fetchAccount({publicKey}, url.href);
      if (result.error || !result.account) fail();
    }
  }
  const sender = PublicKey.fromBase58(expected.wallet);
  for (const publicKey of [expected.wallet, ...expected.recipients.map(r => r.recipient)])
    if (!Mina.hasAccount(PublicKey.fromBase58(publicKey))) fail();
  const tx = await Mina.transaction({sender, nonce: expected.nonce, fee: expected.fee_nano, memo: expected.memo}, async () => {
    const debit = AccountUpdate.createSigned(sender);
    debit.account.isNew.requireEquals(Bool(false));
    for (const r of expected.recipients) {
      const credit = debit.send({to: PublicKey.fromBase58(r.recipient), amount: nano(r.amount_nano)});
      credit.account.isNew.requireEquals(Bool(false));
    }
  });
  verifyTransactionLimits(tx.transaction);
  return JSON.stringify(validateUnsigned(tx.toJSON(), expected));
}
export async function signUnsigned(unsigned, expected, privateKey) {
  const tx = validateUnsigned(unsigned, expected);
  if (PrivateKey.fromBase58(privateKey).toPublicKey().toBase58() !== expected.wallet) fail();
  const signedObject = signZkappCommand(tx, privateKey, expected.network);
  validateUnsigned(signedObject, expected, true);
  const signed = JSON.stringify(signedObject);
  const tx_hash = await Transaction.hash(signed);
  if (tx_hash !== await Transaction.hash(Transaction.fromJSON(signed).toJSON())) fail();
  return {signed, tx_hash};
}
export async function handle(packet) {
  if (packet?.network !== 'mainnet') fail();
  if (packet.action === 'build') return {unsigned: await buildUnsigned(packet)};
  if (packet.action === 'validate') { validateUnsigned(packet.unsigned, packet.expected); return {valid: true}; }
  if (packet.action === 'verify') {
    if (packet.expected?.network !== packet.network) fail();
    const tx = validateUnsigned(packet.signed, packet.expected, true);
    const json = JSON.stringify(tx);
    return {tx_hash: await Transaction.hash(json), query: sendZkappQuery(json)};
  }
  if (packet.action === 'sign') {
    if (packet.expected?.network !== packet.network) fail();
    return signUnsigned(packet.unsigned, packet.expected, packet.privateKey);
  }
  fail();
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    let input = '', bytes = 0;
    for await (const chunk of process.stdin) { bytes += chunk.length; if (bytes > 2_000_000) fail(); input += chunk; }
    const result = await handle(JSON.parse(input));
    process.stdout.write(JSON.stringify(result));
  } catch { process.stderr.write('Batch worker failed validation or execution\n'); process.exitCode = 1; }
}
