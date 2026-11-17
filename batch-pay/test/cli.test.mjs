import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { parseOptions, formatNano } from '../cli-options.mjs';
test('CLI is devnet-only and rejects unknown flags or unsafe batch sizes',()=>{
  assert.throws(()=>parseOptions(['send','--network','mainnet']),/Unknown/);
  assert.throws(()=>parseOptions(['plan','--batch-size','0']),/size/);
  assert.throws(()=>parseOptions(['plan','--batch-size','2.5']),/size/);
  assert.equal(parseOptions(['plan','--batch-size','10']).batchSize,10);
  assert.equal(formatNano('1'),'0.000000001');
});
test('help works without accessing wallets, journal or network',()=>{
  const out=spawnSync(process.execPath,['cli.mjs','--help'],{cwd:new URL('..',import.meta.url),encoding:'utf8'});
  assert.equal(out.status,0); assert.match(out.stdout,/Devnet/); assert.match(out.stdout,/batch-size/);
});
