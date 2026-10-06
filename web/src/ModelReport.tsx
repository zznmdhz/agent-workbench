import {useState} from 'react'
import './modelReport.css'

type Agent='codex'|'claude'|'hermes'
type Measures={requests:number,input_tokens:number,fresh_input_tokens:number,cached_input_tokens:number,cache_creation_tokens:number,output_tokens:number,total_tokens:number}
export type ReportModel=Measures&{agent:Agent,model:string,cache_hit_rate:number|null}
export type ModelReportData={grain:'day'|'week'|'month',periods:string[],models:ReportModel[],series:(Measures&{period:string,agent:Agent,model:string})[],hours:(Measures&{hour:number,agent:Agent,model:string})[],points:{agent:Agent,model:string,at:string,input_tokens:number,output_tokens:number,cached_input_tokens:number,total_tokens:number}[],point_count:number,note:string}
const lineColors=['var(--chart-1-line)','var(--chart-2-line)','var(--chart-3-line)','var(--chart-4-line)','var(--chart-5-line)','var(--chart-6-line)']
const fillColors=['var(--chart-1-fill)','var(--chart-2-fill)','var(--chart-3-fill)','var(--chart-4-fill)','var(--chart-5-fill)','var(--chart-6-fill)']
const dashPatterns=['none','6 3','2 3','8 3 2 3','12 4','1 3']
const fmt=(n:number)=>new Intl.NumberFormat('zh-CN').format(Math.round(n))
const compact=(n:number)=>new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:1}).format(n)
const agentName:Record<Agent,string>={codex:'Codex',claude:'Claude',hermes:'Hermes'}
const keys=[['total_tokens','处理 Token'],['requests','请求／调用'],['output_tokens','输出 Token'],['cached_input_tokens','缓存读取']] as const
const label=(row:ReportModel)=>`${agentName[row.agent]} · ${row.model}`
const rate=(r:ReportModel)=>r.cache_hit_rate===null?'未知':`${(r.cache_hit_rate*100).toFixed(1)}%`

export function ModelReport({data,onSelect}:{data:ModelReportData|null,onSelect:(row:ReportModel)=>void}){
  const [metric,setMetric]=useState<(typeof keys)[number][0]>('total_tokens')
  const [seriesMetric,setSeriesMetric]=useState<'total_tokens'|'requests'>('total_tokens')
  if(!data)return <section className="reportSection"><p className="mvpEmpty">正在生成模型分析…</p></section>
  const models=data.models
  const total=models.reduce((sum,r)=>sum+r.total_tokens,0)
  const calls=models.reduce((sum,r)=>sum+r.requests,0)
  const cache=models.reduce((sum,r)=>sum+r.cached_input_tokens,0)
  const input=models.reduce((sum,r)=>sum+r.input_tokens,0)
  const top=models.slice(0,6)
  const maxBar=Math.max(1,...top.map(r=>r[metric]))
  const share=top.slice(0,5)
  const other=Math.max(0,total-share.reduce((sum,r)=>sum+r.total_tokens,0))
  let angle=0
  const slices=[...share.map((r,i)=>({name:label(r),value:r.total_tokens,color:fillColors[i]})),...(other?[{name:'其他模型',value:other,color:fillColors[5]}]:[])]
  const gradient=slices.map(s=>{const from=angle;angle+=total?s.value/total*360:0;return `${s.color} ${from}deg ${angle}deg`}).join(',')
  const periods=data.periods
  const seriesModels=models.filter(r=>r.agent!=='hermes'&&data.series.some(s=>s.agent===r.agent&&s.model===r.model&&s[seriesMetric]>0)).slice(0,5)
  const trendValues=periods.map(period=>seriesModels.map(row=>data.series.find(s=>s.period===period&&s.agent===row.agent&&s.model===row.model)?.[seriesMetric]||0))
  const maxTrend=Math.max(1,...trendValues.flat())
  const hasTrend=seriesModels.length>0&&trendValues.some(row=>row.some(value=>value>0))
  const x=(i:number)=>periods.length<2?300:42+i*558/(periods.length-1)
  const y=(value:number)=>180-value/maxTrend*154
  const hourTotals=Array.from({length:24},(_,hour)=>data.hours.filter(r=>r.hour===hour).reduce((sum,r)=>sum+r.requests,0))
  const maxHour=Math.max(1,...hourTotals)
  const points=data.points
  const maxInput=Math.max(1,...points.map(p=>p.input_tokens))
  const maxOutput=Math.max(1,...points.map(p=>p.output_tokens))
  return <section className="reportSection" aria-label="模型用量分析">
    <div className="reportHeading"><div><span className="reportEyebrow">MODEL INTELLIGENCE</span><h2>模型用量分析</h2><p>比较模型规模、缓存复用与调用节奏；点击模型可筛选整页。</p></div><span className="reportBadge">{fmt(models.length)} 个模型</span></div>
    <div className="reportKpis"><div><small>处理 Token</small><strong>{compact(total)}</strong><span>含完整 Hermes 会话汇总</span></div><div><small>请求／调用</small><strong>{fmt(calls)}</strong><span>不同来源的计数精度见说明</span></div><div><small>平均每次处理</small><strong>{calls?compact(total/calls):'—'}</strong><span>Token／次，仅作规模参考</span></div><div><small>缓存读取占输入</small><strong>{input?`${(cache/input*100).toFixed(1)}%`:'—'}</strong><span>缓存读取 ÷ 全部输入</span></div></div>
    <div className="reportGrid">
      <article className="reportCard reportWide"><header><div><h3>模型对比</h3><p>前 6 个模型 · 切换指标观察差异</p></div><select aria-label="模型对比指标" value={metric} onChange={e=>setMetric(e.target.value as typeof metric)}>{keys.map(([key,name])=><option key={key} value={key}>{name}</option>)}</select></header><div className="reportBars">{top.map((row,i)=><button key={`${row.agent}:${row.model}`} className="reportBarRow" onClick={()=>onSelect(row)} title={`筛选 ${label(row)}`}><span>{label(row)}</span><i><b style={{width:`${row[metric]/maxBar*100}%`,background:fillColors[i]}}/></i><strong>{compact(row[metric])}</strong></button>)}</div>{!top.length&&<p className="mvpEmpty">所选范围暂无模型用量。</p>}</article>
      <article className="reportCard"><header><div><h3>模型份额</h3><p>处理 Token 占比 · 前 5 个及其他</p></div></header><div className="reportShare">{total>0&&<div className="reportDonut" style={{background:`conic-gradient(${gradient})`}} role="img" aria-label="模型处理 Token 份额"><span><b>{compact(total)}</b><small>Token</small></span></div>}<div className="reportLegend">{slices.map(s=><div key={s.name}><i style={{background:s.color}}/><span title={s.name}>{s.name}</span><strong>{total?(s.value/total*100).toFixed(1):0}%</strong></div>)}</div></div></article>
      <article className="reportCard reportWide"><header><div><h3>模型趋势</h3><p>按{data.grain==='day'?'天':data.grain==='week'?'周':'月'}比较前 5 个模型 · Hermes 无逐日数据</p></div><select aria-label="趋势指标" value={seriesMetric} onChange={e=>setSeriesMetric(e.target.value as typeof seriesMetric)}><option value="total_tokens">处理 Token</option><option value="requests">请求次数</option></select></header>{hasTrend?<><svg className="reportLine" viewBox="0 0 630 210" role="img" aria-label="各模型按时间的趋势折线图" preserveAspectRatio="none"><line x1="42" y1="180" x2="600" y2="180"/><line x1="42" y1="103" x2="600" y2="103"/><line x1="42" y1="26" x2="600" y2="26"/>{seriesModels.map((m,index)=><polyline key={`${m.agent}:${m.model}`} points={periods.map((_,i)=>`${x(i)},${y(trendValues[i][index])}`).join(' ')} fill="none" stroke={lineColors[index]} strokeDasharray={dashPatterns[index]} strokeWidth="3" strokeLinejoin="round" strokeLinecap="round"/>)}<text x="2" y="30">{compact(maxTrend)}</text><text x="2" y="184">0</text><text x="42" y="204">{periods[0]}</text><text x="520" y="204">{periods.at(-1)}</text></svg><div className="reportLineLegend">{seriesModels.map((m,i)=><span key={`${m.agent}:${m.model}`}><svg viewBox="0 0 28 8" aria-hidden="true"><line x1="0" y1="4" x2="28" y2="4" stroke={lineColors[i]} strokeDasharray={dashPatterns[i]} strokeWidth="3"/></svg>{label(m)}</span>)}</div></>:<p className="mvpEmpty">所选区间内无可比对的逐{data.grain==='month'?'月':data.grain==='week'?'周':'日'}数据。</p>}</article>
      <article className="reportCard"><header><div><h3>一天中的调用频率</h3><p>按请求发生的本地小时汇总</p></div></header><div className="reportHours" role="img" aria-label="24 小时请求次数柱状图">{hourTotals.map((count,h)=><div key={h} title={`${h}:00 · ${fmt(count)} 次`}><i style={{height:`${Math.max(count?5:2,count/maxHour*100)}%`}}/><small>{h%4===0?`${String(h).padStart(2,'0')}`:''}</small></div>)}</div><p className="reportFine">仅 Codex / Claude 逐次请求。柱高表示请求次数。</p></article>
      <article className="reportCard reportWide"><header><div><h3>单次请求分布</h3><p>横轴输入 Token，纵轴输出 Token · 最多均匀抽样 400 点</p></div><span className="reportSmall">{fmt(data.point_count)} 次请求</span></header>{points.length?<svg className="reportScatter" viewBox="0 0 630 200" role="img" aria-label="单次请求输入和输出 Token 散点图" preserveAspectRatio="none"><line x1="42" y1="170" x2="600" y2="170"/><line x1="42" y1="18" x2="42" y2="170"/>{points.map((p,i)=><circle key={i} cx={42+p.input_tokens/maxInput*558} cy={170-p.output_tokens/maxOutput*152} r="3.5" fill={fillColors[Math.max(0,top.findIndex(m=>m.agent===p.agent&&m.model===p.model))%fillColors.length]} stroke={lineColors[Math.max(0,top.findIndex(m=>m.agent===p.agent&&m.model===p.model))%lineColors.length]} strokeWidth="1" opacity=".8"><title>{agentName[p.agent]} · {p.model} · 输入 {fmt(p.input_tokens)} · 输出 {fmt(p.output_tokens)}</title></circle>)}<text x="3" y="20">{compact(maxOutput)}</text><text x="530" y="194">{compact(maxInput)} 输入</text></svg>:<p className="mvpEmpty">此筛选范围没有逐次请求数据。</p>}</article>
      <article className="reportCard"><header><div><h3>缓存命中率</h3><p>缓存读取 ÷ 全部输入</p></div></header><div className="reportCache">{top.map((r,i)=><div key={`${r.agent}:${r.model}`}><span title={label(r)}>{label(r)}</span><i><b style={{width:`${(r.cache_hit_rate||0)*100}%`,background:fillColors[i]}}/></i><strong>{rate(r)}</strong></div>)}</div></article>
    </div>
    <div className="reportCard reportTable"><header><div><h3>模型明细</h3><p>完整数值可用于核对图表；选择模型可聚焦分析。</p></div></header><div className="mvpTableWrap"><table><thead><tr><th>Agent</th><th>模型</th><th>请求／调用</th><th>新输入</th><th>缓存读</th><th>缓存写</th><th>输出</th><th>处理总量</th><th>缓存命中率</th></tr></thead><tbody>{models.map(row=><tr key={`${row.agent}:${row.model}`}><td>{agentName[row.agent]}</td><td><button className="mvpModelButton" onClick={()=>onSelect(row)}>{row.model}</button></td><td>{fmt(row.requests)}</td><td>{fmt(row.fresh_input_tokens)}</td><td>{fmt(row.cached_input_tokens)}</td><td>{fmt(row.cache_creation_tokens)}</td><td>{fmt(row.output_tokens)}</td><td>{fmt(row.total_tokens)}</td><td>{rate(row)}</td></tr>)}</tbody></table></div></div>
    <p className="reportNote">{data.note} 缓存命中率是 Token 占比，不是请求成功率；缓存字段缺失时不补算。不同 Agent 的请求和调用口径不同，平均值仅供规模参考。</p>
  </section>
}
