// Deterministic bankroll paths + ±1σ band. Flat 1-unit stake, win prob p, decimal odds o.
// 400 simulated paths; the drawn ones sit at fixed quantiles of the final result.
function mulberry32(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
function makeChart({odds=2.10,p=0.5,N=1000,W=920,H=400,quantiles=[0.05,0.2,0.4,0.6,0.8,0.95]}={}){
  const b=odds-1, mean=b*p-(1-p), sd=Math.sqrt(p*b*b+(1-p)-mean*mean);
  const exp=mean*N, sN=sd*Math.sqrt(N), yMax=Math.ceil((exp+2.2*sN)/20)*20, yMin=-Math.ceil(Math.max(1.6*sN-exp,20)/20)*20;
  const sx=n=>n/N*W, sy=v=>(yMax-v)/(yMax-yMin)*H;
  const sims=[];
  for(let s=1;s<=400;s++){const r=mulberry32(1000+s*7919);let bank=0;const pts=[[0,0]];for(let n=1;n<=N;n++){bank+=r()<p?b:-1;if(n%10===0)pts.push([n,bank]);}sims.push({final:bank,pts});}
  sims.sort((a,b)=>a.final-b.final);
  const picks=quantiles.map(q=>sims[Math.floor(q*(sims.length-1))]);
  const paths=picks.map(pk=>pk.pts.map(([n,v])=>sx(n).toFixed(1)+','+sy(v).toFixed(1)).join(' '));
  const up=[],lo=[];for(let n=0;n<=N;n+=10){const m=mean*n,s=sd*Math.sqrt(n);up.push(sx(n).toFixed(1)+','+sy(m+s).toFixed(1));lo.push(sx(n).toFixed(1)+','+sy(m-s).toFixed(1));}
  const band=up.concat(lo.reverse()).join(' ');
  const expY=Math.round(exp);
  return {paths,band,finals:picks.map(pk=>+pk.final.toFixed(1)),expected:`0,${sy(0)} ${W},${sy(exp)}`,sy0:sy(0),syExp:sy(exp),expY,W,H,mean,sd,exp,sdN:sd*Math.sqrt(N),negShare:sims.filter(s=>s.final<0).length/sims.length};
}
module.exports={makeChart};
if(require.main===module){const c=makeChart({odds:+(process.argv[2]||2.10)});console.log(JSON.stringify({finals:c.finals,exp:c.exp,sdN:c.sdN,negShare:c.negShare}));}
