import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, lstatSync, openSync, closeSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { validatePlan } from './builder.mjs';

export class Journal {
  constructor(directory) {
    mkdirSync(directory, { recursive:true, mode:0o700 });
    const stat=lstatSync(directory);
    if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077)) throw Error('Journal directory must be private (0700), not a symlink');
    const path=join(directory,'batch-journal.sqlite3');
    if (!existsSync(path)) closeSync(openSync(path,'wx',0o600));
    const file=lstatSync(path);
    if (!file.isFile() || file.isSymbolicLink() || (file.mode & 0o077)) throw Error('Journal must be a private regular file');
    this.db=new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, chain TEXT NOT NULL, sender TEXT NOT NULL,
        operation TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(chain,sender,operation));
      CREATE TABLE IF NOT EXISTS batches(plan TEXT NOT NULL REFERENCES plans(id), idx INTEGER NOT NULL,
        chain TEXT NOT NULL, sender TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned', nonce INTEGER,
        hash TEXT, signed TEXT, observation TEXT, PRIMARY KEY(plan,idx), UNIQUE(chain,sender,nonce));`);
  }
  close() { this.db.close(); }
  atomic(fn) { this.db.exec('BEGIN IMMEDIATE'); try { const r=fn(); this.db.exec('COMMIT'); return r; }
    catch(e) { this.db.exec('ROLLBACK'); throw e; } }
  create(plan) {
    validatePlan(plan);
    return this.atomic(()=>{
      const old=this.db.prepare('SELECT * FROM plans WHERE chain=? AND sender=? AND operation=?').get(plan.chainId,plan.sender,plan.operation);
      if(old) { if(old.id!==plan.id || old.body!==JSON.stringify(plan)) throw Error('Operation already exists with a different immutable plan'); return old.id; }
      const pending=this.db.prepare("SELECT 1 FROM batches WHERE chain=? AND sender=? AND state!='confirmed' LIMIT 1").get(plan.chainId,plan.sender);
      if(pending) throw Error('Wallet has an unfinished batch operation');
      this.db.prepare('INSERT INTO plans VALUES(?,?,?,?,?)').run(plan.id,plan.chainId,plan.sender,plan.operation,JSON.stringify(plan));
      for(let i=0;i<plan.batches.length;i++) this.db.prepare('INSERT INTO batches(plan,idx,chain,sender) VALUES(?,?,?,?)').run(plan.id,i,plan.chainId,plan.sender);
      return plan.id;
    });
  }
  plan(id) { const row=this.db.prepare('SELECT body FROM plans WHERE id=?').get(id); if(!row)throw Error('Unknown plan'); return validatePlan(JSON.parse(row.body)); }
  rows(id) { return this.db.prepare('SELECT * FROM batches WHERE plan=? ORDER BY idx').all(id); }
  list() { return this.db.prepare('SELECT id,operation,sender FROM plans ORDER BY rowid').all(); }
  prepare(id,index,nonce,hash,signed) {
    return this.atomic(()=>{
      const row=this.rows(id)[index]; if(!row)throw Error('Unknown batch');
      if(row.state!=='planned') {
        if(row.nonce!==nonce || row.hash!==hash || row.signed!==signed)throw Error('Immutable signed batch mismatch');
        return;
      }
      this.db.prepare("UPDATE batches SET nonce=?,hash=?,signed=?,state='signed' WHERE plan=? AND idx=? AND state='planned'").run(nonce,hash,signed,id,index);
    });
  }
  claim(id,index) {
    // Commit the uncertainty BEFORE touching the network. A crash here must
    // remain uncertain even if the request never left this process.
    return this.db.prepare("UPDATE batches SET state='unknown' WHERE plan=? AND idx=? AND state='signed'").run(id,index).changes===1;
  }
  observe(id,index,proof) {
    const state=proof.status==='applied' ? (proof.confirmations>=3?'confirmed':'included') : proof.status==='pending'?'submitted':proof.status==='failed'?'failed':'unknown';
    this.db.prepare('UPDATE batches SET state=?,observation=? WHERE plan=? AND idx=? AND hash=?').run(state,JSON.stringify(proof),id,index,proof.hash);
  }
}
