#!/usr/bin/env node
import { parseOptions } from './cli-options.mjs';
const options=parseOptions(process.argv.slice(2));
if(options.command==='--help') {
  console.log(`Mina batch pay — изолированная Devnet, реальные zkApp-транзакции.
Из корня проекта:
  node batch-pay/cli.mjs init
  node batch-pay/cli.mjs plan --file recipients.json --operation test-001 --batch-size 10 --fee-nano 100000000
  node batch-pay/cli.mjs send --plan ID [--yes] [--wait 900]
  node batch-pay/cli.mjs resume --plan ID [--wait 900]
  node batch-pay/cli.mjs status [--plan ID]
JSON получателей: [{"address":"B62...","amount":"1"}]. Суммы — целые nanomina.
Явное "create":true оплачивает создание нового аккаунта (1 tMINA).
resume только сверяет, send подтверждает отправку оставшихся пакетов.
Mainnet отключён. Текущие выплаты и учёт проекта не используются.`);
  process.exit(0);
}
const { mkdirSync,readFileSync,writeFileSync,lstatSync,existsSync }=await import('node:fs');
const { fileURLToPath }=await import('node:url');
const { join }=await import('node:path');
const { PrivateKey }=await import('o1js');
const { makePlan }=await import('./builder.mjs');
const { Journal }=await import('./journal.mjs');
const { Devnet,CHAIN_ID }=await import('./network.mjs');
const { runNext,reconcile }=await import('./workflow.mjs');
const {showPlan,showRows,paint:color}=await import('./display.mjs');
const directory=fileURLToPath(new URL('.state/devnet/',import.meta.url));
const keyPath=join(directory,'key.json');
function readKey() {
  const stat=lstatSync(keyPath);
  if(!stat.isFile()||stat.isSymbolicLink()||(stat.mode&0o077))throw Error('Test key must be a private 0600 regular file');
  const data=JSON.parse(readFileSync(keyPath,'utf8'));
  const key=PrivateKey.fromBase58(data.privateKey);
  if(key.toPublicKey().toBase58()!==data.publicKey)throw Error('Test key identity mismatch');
  return key;
}
let journal;
try {
  if(options.command==='init') {
    mkdirSync(directory,{recursive:true,mode:0o700});
    if(!existsSync(keyPath)) {
      const key=PrivateKey.random();
      writeFileSync(keyPath,JSON.stringify({privateKey:key.toBase58(),publicKey:key.toPublicKey().toBase58()})+'\n',{mode:0o600,flag:'wx'});
    }
    console.log(`Тестовый адрес: ${readKey().toPublicKey().toBase58()}`);
    process.exit(0);
  }
  journal=new Journal(directory);
  if(options.command==='status') {
    if(options.plan){showPlan(journal.plan(options.plan));showRows(journal.rows(options.plan));}
    else for(const p of journal.list())console.log(`${p.operation}: ${p.id}`);
  } else {
    const adapter=new Devnet();
    if(options.command==='plan') {
      if(!options.file||!options.operation)throw Error('--file and --operation required');
      const plan=makePlan({sender:readKey().toPublicKey().toBase58(),chainId:CHAIN_ID,
        recipients:JSON.parse(readFileSync(options.file,'utf8')),batchSize:options.batchSize,fee:options.fee,operation:options.operation});
      await adapter.health(plan.sender);
      for(let i=0;i<plan.batches.length;i++)await adapter.prepareAccounts(plan,i);
      journal.create(plan);showPlan(plan);
      console.log('План сохранён. Отправка не выполнялась.');
    } else {
      if(!options.plan)throw Error('--plan required');
      const plan=journal.plan(options.plan);showPlan(plan);
      if(options.command==='send'&&!options.yes) {
        const {createInterface}=await import('node:readline/promises');
        const prompt=createInterface({input:process.stdin,output:process.stdout});
        const answer=await prompt.question('Отправить оставшиеся тестовые пакеты? [y/N]: ');prompt.close();
        if(answer.trim().toLowerCase()!=='y')throw Error('Отправка отменена');
      }
      const key=options.command==='send'?readKey():null;
      const deadline=Date.now()+options.wait*1000;let last='';
      while(true) {
        const rows=options.command==='send'?await runNext(journal,plan.id,adapter,key):await reconcile(journal,plan.id,adapter);
        const current=JSON.stringify(rows.map(r=>[r.state,r.observation]));
        if(current!==last){showRows(rows);last=current;}
        if(rows.every(r=>r.state==='confirmed')){console.log(color('Все пакеты подтверждены. Повторной отправки не будет.',32));break;}
        if(rows.some(r=>r.state==='failed'))throw Error('Пакет не выполнен в цепочке. Нужна проверка, автоматической повторной выплаты нет.');
        if(options.command==='resume'&&rows.every(r=>['planned','signed','confirmed'].includes(r.state))){process.exitCode=2;break;}
        if(Date.now()>=deadline){console.log('Ожидание закончено; результат сохранён. Продолжите resume.');process.exitCode=2;break;}
        await new Promise(resolve=>setTimeout(resolve,15000));
      }
    }
  }
} catch(e) {
  console.error(color(`Остановлено: ${e.message}`,31));
  if(options.plan)console.error(`Сверка: node batch-pay/cli.mjs resume --plan ${options.plan}`);
  process.exitCode=1;
} finally { journal?.close(); }
