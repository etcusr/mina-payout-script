import { formatNano } from './cli-options.mjs';
const tty=()=>Boolean(process.stdout.isTTY&&!process.env.NO_COLOR);
export const paint=(s,color)=>tty()?`\x1b[${color}m${s}\x1b[0m`:s;
export const addressLink=address=>tty()?`\x1b]8;;https://minascan.io/devnet/account/${address}\x1b\\${address}\x1b]8;;\x1b\\`:address;
export function showPlan(plan) {
  console.log(paint(`Devnet · ${plan.operation} · ${plan.batches.length} пакет(ов)`,36));
  console.log(`Отправитель: ${addressLink(plan.sender)}\nПлан: ${plan.id}`);
  const rows=plan.batches.flatMap((b,i)=>b.recipients.map(r=>[String(i+1),r.address,formatNano(r.amount),r.create?'1 tMINA':'—']));
  const headers=['Пакет','Адрес получателя','Сумма tMINA','Создание'];
  const widths=headers.map((h,i)=>Math.max(h.length,...rows.map(r=>r[i].length)));
  const rule=(l,m,r)=>l+widths.map(w=>'─'.repeat(w+2)).join(m)+r;
  const row=(r,link=false)=>'│ '+r.map((s,i)=>((link&&i===1)?addressLink(s):s)+' '.repeat(widths[i]-s.length)).join(' │ ')+' │';
  console.log(rule('╭','┬','╮'));console.log(paint(row(headers),36));console.log(rule('├','┼','┤'));
  rows.forEach((r,i)=>{console.log(row(r,true));if(i<rows.length-1)console.log(rule('├','┼','┤'));});
  console.log(rule('╰','┴','╯'));
  console.log(`Получателям: ${formatNano(plan.total)} tMINA · создание: ${formatNano(plan.creationCost)} tMINA`);
  console.log(`Комиссия за пакет: ${formatNano(plan.fee)} tMINA · всего: ${formatNano(plan.cost)} tMINA`);
}
export function showRows(rows) {
  const names={planned:'Не отправлен',signed:'Подписан, не отправлен',unknown:'Исход неизвестен — только сверка',submitted:'В пуле / принят нодой',included:'Applied',confirmed:'Подтверждён',failed:'Не выполнен'};
  for(const r of rows) {
    const p=r.observation?JSON.parse(r.observation):{};
    console.log(paint(`Пакет ${r.idx+1}: ${names[r.state]} · nonce=${r.nonce??'—'} · подтверждения ${p.confirmations??'—'}/3`,r.state==='confirmed'?32:r.state==='failed'?31:33));
    if(r.hash)console.log(`  https://minascan.io/devnet/tx/${r.hash}?type=zk-tx`);
  }
}
