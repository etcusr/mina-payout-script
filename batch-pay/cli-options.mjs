export function parseOptions(args) {
  const command=args[0]??'--help';
  if(!['--help','init','plan','send','resume','status'].includes(command))throw Error('Unknown command');
  const result={command,batchSize:10,fee:'100000000',wait:900,yes:false};
  const names={'--batch-size':'batchSize','--fee-nano':'fee','--file':'file','--operation':'operation','--plan':'plan','--wait':'wait'};
  for(let i=1;i<args.length;i++) {
    const arg=args[i];
    if(arg==='--yes'){result.yes=true;continue;}
    if(!names[arg] || i+1>=args.length)throw Error(`Unknown or incomplete option: ${arg}`);
    result[names[arg]]=args[++i];
  }
  result.batchSize=Number(result.batchSize); result.wait=Number(result.wait);
  if(!Number.isInteger(result.batchSize)||result.batchSize<1||result.batchSize>30)throw Error('Batch size must be 1..30');
  if(!Number.isInteger(result.wait)||result.wait<0||result.wait>86400)throw Error('Wait must be 0..86400 seconds');
  return result;
}
export function formatNano(value) { const n=BigInt(value);return `${n/1000000000n}.${(n%1000000000n).toString().padStart(9,'0')}`; }
