import React, {useEffect, useState} from 'react'
import {createRoot} from 'react-dom/client'
import './mvp.css'

type Agent='codex'|'claude'|'hermes'
type Totals={requests:number,input_tokens:number,fresh_input_tokens:number,cached_input_tokens:number,cache_creation_tokens:number,output_tokens:number,total_tokens:number,cache_hit_rate?:number|null}
type Source=Totals&{status:string,precision:'request'|'session_model_aggregate',earliest:string|null,latest:string|null,files:number,deferred:number,partial_rows?:number,updated_files?:number,missing_cache_read?:number,missing_cache_write?:number}
type Trend=Totals&{period:string}
type Model=Totals&{agent:Agent,model:string}
type Session=Totals&{agent:Agent,native_id:string,title:string,last_request:string}
type Usage={status:string,summary:Totals,sources:Record<Agent,Source>,models:Model[],sessions:Session[],session_count:number,trend:Trend[],trend_granularity:'day'|'week'|'month',unattributed_tokens:number,note:string}
type Request=Totals&{request_id:string,occurred_at:string,first_seen?:string,model:string,precision:'request'|'session_model_aggregate'}
const names:Record<Agent,string>={codex:'Codex',claude:'Claude',hermes:'Hermes'}
const fmt=(n:number)=>new Intl.NumberFormat('zh-CN').format(n)
const shift=(day:string,n:number)=>{const d=new Date(`${day}T12:00:00Z`);d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)}
const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())
const when=(value:string|null)=>value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Hong_Kong',hour12:false}):'无记录'
const dayOf=(value:string|null)=>value?new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value)):'无记录'
const sourceStatus=(row:Source)=>row.status==='source_missing'?'未发现本机来源':row.status==='read_error'?'读取失败':row.deferred>0?`${row.deferred} 项暂未计入`:'已读取'

async function get<T>(path:string):Promise<T>{const r=await fetch(path,{credentials:'same-origin'});if(!r.ok)throw new Error(`${r.status}: ${(await r.text()).slice(0,120)}`);return r.json()}
async function post<T>(path:string,body?:object,csrf?:string):Promise<T>{const r=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',...(csrf?{'x-awb-csrf':csrf}:{})},body:body?JSON.stringify(body):undefined});if(!r.ok)throw new Error(`${r.status}: ${(await r.text()).slice(0,120)}`);return r.json()}

function App(){
  const [csrf,setCsrf]=useState('')
  const [entry,setEntry]=useState<'checking'|'setup'|'login'|'unavailable'>('checking')
  const [password,setPassword]=useState('')
  const [confirmation,setConfirmation]=useState('')
  const [error,setError]=useState('')
  const [stopped,setStopped]=useState(false)
  const [start,setStart]=useState(()=>shift(today(),-6))
  const [end,setEnd]=useState(today)
  const [preset,setPreset]=useState('7')
  const [agent,setAgent]=useState<Agent|''>('')
  const [model,setModel]=useState('')
  const [choices,setChoices]=useState<string[]>([])
  const [data,setData]=useState<Usage|null>(null)
  const [loading,setLoading]=useState(false)
  const [tick,setTick]=useState(0)
  const [selected,setSelected]=useState<Session|null>(null)
  const [requests,setRequests]=useState<Request[]|null>(null)
  const [requestCount,setRequestCount]=useState(0)
  const invalidRange=!start||!end||start>end

  useEffect(()=>{get<{csrf:string}>('/auth/me').then(x=>{setCsrf(x.csrf);setEntry('login')}).catch(()=>get<{needs_setup:boolean,web_setup_available:boolean}>('/auth/setup-status').then(x=>setEntry(x.needs_setup?(x.web_setup_available?'setup':'unavailable'):'login')).catch(()=>setError('无法连接工作台服务')))},[])
  useEffect(()=>{
    if(!csrf||invalidRange)return
    let cancelled=false
    const params=new URLSearchParams({day:start,through:end,tz:'Asia/Hong_Kong'})
    if(agent)params.set('agent',agent)
    if(model)params.set('model',model)
    setLoading(true)
    get<Usage>(`/v1/mvp/usage?${params}`).then(x=>{if(!cancelled){setData(x);setError('');if(!model)setChoices(x.models.map(m=>m.model).filter((v,i,a)=>a.indexOf(v)===i).sort())}}).catch(e=>{if(!cancelled)setError(`用量读取失败：${String(e)}`)}).finally(()=>{if(!cancelled)setLoading(false)})
    return()=>{cancelled=true}
  },[csrf,start,end,agent,model,tick,invalidRange])
  useEffect(()=>{
    if(!selected||invalidRange){setRequests(null);return}
    let cancelled=false
    setRequests(null)
    const params=new URLSearchParams({day:start,through:end,tz:'Asia/Hong_Kong',limit:'100'})
    if(model)params.set('model',model)
    get<{items:Request[],count:number}>(`/v1/mvp/usage/sessions/${selected.agent}/${encodeURIComponent(selected.native_id)}/requests?${params}`).then(x=>{if(!cancelled){setRequests(x.items);setRequestCount(x.count)}}).catch(()=>{if(!cancelled){setRequests([]);setRequestCount(0)}})
    return()=>{cancelled=true}
  },[selected,start,end,model,tick,invalidRange])

  function chooseRange(days:number){setPreset(String(days));setStart(shift(today(),1-days));setEnd(today());setSelected(null)}
  function allHistory(){
    const earliest=Object.values(data?.sources||{}).map(x=>x.earliest).filter((x):x is string=>!!x).sort()[0]
    setStart(earliest?dayOf(earliest):'2026-01-01');setEnd(today());setPreset('all');setSelected(null)
  }
  async function login(e:React.FormEvent){e.preventDefault();setError('');try{const x=await post<{csrf:string}>('/auth/login',{password});setCsrf(x.csrf);setPassword('')}catch(e){setError(`登录失败：${String(e)}`)}}
  async function setup(e:React.FormEvent){e.preventDefault();setError('');if(password.length<12||password!==confirmation){setError('密码至少 12 个字符，且两次输入一致');return}try{const x=await post<{csrf:string}>('/auth/setup',{password,confirmation});setCsrf(x.csrf);setPassword('');setConfirmation('')}catch(e){setError(`设置失败：${String(e)}`)}}
  async function logout(){try{await post('/auth/logout',undefined,csrf)}finally{setCsrf('');setData(null);setSelected(null)}}
  async function shutdown(){if(!window.confirm('关闭工作台？'))return;try{await post('/v1/local/shutdown',undefined,csrf);setStopped(true)}catch(e){setError(String(e))}}

  if(stopped)return <main className="mvpLogin"><div className="mvpLoginCard"><h1>工作台已关闭</h1><p>从开始菜单重新打开即可继续使用。</p></div></main>
  if(!csrf)return <main className="mvpLogin"><div className="mvpLoginCard"><div className="mvpMark">✳</div><h1>{entry==='setup'?'创建管理员密码':'Agent Workbench'}</h1><p>{entry==='setup'?'首次使用，在网页中创建密码。':entry==='login'?'登录后查看本机用量仪表盘。':entry==='checking'?'正在连接…':'请在 Windows 本机打开桌面程序完成设置。'}</p>{(entry==='setup'||entry==='login')&&<form onSubmit={entry==='setup'?setup:login}><label>管理员密码<input autoFocus required type="password" minLength={entry==='setup'?12:undefined} value={password} onChange={e=>setPassword(e.target.value)}/></label>{entry==='setup'&&<label>确认密码<input required type="password" value={confirmation} onChange={e=>setConfirmation(e.target.value)}/></label>}<button type="submit">{entry==='setup'?'创建并进入':'登录'}</button></form>}{error&&<p className="mvpError" role="alert">{error}</p>}</div></main>

  const summary=data?.summary
  const max=Math.max(1,...(data?.trend||[]).map(x=>x.total_tokens),data?.unattributed_tokens||0)
  const cards=summary?[
    ['请求／调用',fmt(summary.requests),'Codex/Claude 请求与 Hermes 汇总调用'],
    ['处理 Token',fmt(summary.total_tokens),'已记录量：新输入 + 缓存读 + 缓存写 + 输出'],
    ['新输入',fmt(summary.fresh_input_tokens),'不含缓存的输入'],
    ['缓存读取',fmt(summary.cached_input_tokens),summary.cache_hit_rate===null?'缓存率未知':`占全部输入 ${(summary.cache_hit_rate!*100).toFixed(1)}%`],
    ['缓存写入',fmt(summary.cache_creation_tokens),'Claude/Hermes 记录的写入'],
    ['输出',fmt(summary.output_tokens),'模型生成的 Token'],
    ['活跃会话',fmt(data!.session_count),'所选范围内有用量的会话'],
  ]:[]
  return <div className="mvpShell"><header className="mvpHeader"><div className="mvpBrand"><span className="mvpMark">✳</span><span><b>Agent Workbench</b><small>多 Agent 用量</small></span></div><div className="mvpHeaderActions"><button onClick={()=>setTick(x=>x+1)} disabled={loading}>{loading?'同步中…':'刷新'}</button><button onClick={logout}>退出</button><button onClick={shutdown}>关闭工作台</button></div></header><main className="mvpMain"><div className="mvpTitle"><div><p>USAGE / AGENTS</p><h1>用量仪表盘</h1><span>按原始记录核对 Token；不完整的历史范围会明确标示。</span></div><span className="mvpScope">Codex · Claude · Hermes</span></div>
    <section className="mvpFilters" aria-label="用量筛选"><div className="mvpRange" role="group" aria-label="快捷日期范围">{[1,7,15].map(n=><button key={n} aria-pressed={preset===String(n)} onClick={()=>chooseRange(n)}>{n===1?'今天':`近 ${n} 天`}</button>)}<button aria-pressed={preset==='all'} onClick={allHistory}>全部历史</button></div><label>开始日期 <input type="date" value={start} onChange={e=>{setStart(e.target.value);setPreset('custom');setSelected(null)}}/></label><label>结束日期 <input type="date" value={end} onChange={e=>{setEnd(e.target.value);setPreset('custom');setSelected(null)}}/></label><label>Agent <select value={agent} onChange={e=>{setAgent(e.target.value as Agent|'');setModel('');setSelected(null)}}><option value="">全部 Agent</option>{Object.entries(names).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label><label>模型 <select value={model} onChange={e=>{setModel(e.target.value);setSelected(null)}}><option value="">全部模型</option>{choices.map(x=><option key={x} value={x}>{x}</option>)}</select></label></section>
    {invalidRange&&<div className="mvpError" role="alert">开始日期不能晚于结束日期。</div>}{error&&<div className="mvpError" role="alert">{error}</div>}
    {data&&<p className="mvpProvenance">查询：{start} 至 {end}，香港时间。{data.note} 原生记录未提供的缓存字段无法补算，显示的总量可能偏低；费用尚未计价。{loading?' 正在同步本机日志…':''}</p>}
    <section className="mvpCards" aria-label="核心指标">{cards.map(([label,value,note])=><article className="mvpCard" key={label}><small>{label}</small><strong>{value}</strong><span>{note}</span></article>)}</section>
    {data&&<section className="mvpPanel"><div className="mvpPanelHead"><h2>数据来源与覆盖</h2><span>点击来源可只看该 Agent</span></div><div className="mvpSourceGrid">{(Object.keys(names) as Agent[]).map(name=>{const source=data.sources[name];return <button className="mvpSource" key={name} onClick={()=>{setAgent(name);setModel('');setSelected(null)}} aria-pressed={agent===name}><b>{names[name]}</b><strong>{fmt(source.total_tokens)} Token</strong><span>{fmt(source.requests)} {name==='hermes'?'调用（会话汇总）':'请求'} · {sourceStatus(source)}</span><small>本机记录：{dayOf(source.earliest)} 至 {dayOf(source.latest)}</small>{name==='claude'&&!!((source.missing_cache_read||0)+(source.missing_cache_write||0))&&<small>原始记录缺缓存读字段 {fmt(source.missing_cache_read||0)} 条、缺缓存写字段 {fmt(source.missing_cache_write||0)} 条；对应总量可能偏低</small>}{name==='hermes'&&<small>只能按完整会话汇总计入；跨越筛选边界 {fmt(source.partial_rows||0)} 项未计入</small>}</button>})}</div></section>}
    <section className="mvpPanel"><div className="mvpPanelHead"><h2>{data?.trend_granularity==='month'?'每月用量':data?.trend_granularity==='week'?'每周用量':'每日用量'}</h2><span>Codex / Claude 按请求时间；Hermes 不能拆到每日</span></div><div className="mvpTrend">{data?.trend.map(row=><div className="mvpTrendRow" key={row.period}><time>{row.period}</time><div className="mvpBar"><span style={{width:`${row.total_tokens/max*100}%`}}/></div><b>{fmt(row.total_tokens)}</b><small>{fmt(row.requests)} 请求</small></div>)}{!!data?.unattributed_tokens&&<div className="mvpTrendRow mvpUnattributed"><time>未分配日期</time><div className="mvpBar"><span style={{width:`${data.unattributed_tokens/max*100}%`}}/></div><b>{fmt(data.unattributed_tokens)}</b><small>Hermes 汇总</small></div>}</div></section>
    <div className="mvpTwo"><section className="mvpPanel"><div className="mvpPanelHead"><h2>模型用量</h2><span>点击行筛选模型</span></div><div className="mvpTableWrap"><table><thead><tr><th>Agent</th><th>模型</th><th>请求／调用</th><th>新输入</th><th>缓存读</th><th>缓存写</th><th>输出</th><th>处理总量</th></tr></thead><tbody>{data?.models.map(row=><tr key={`${row.agent}:${row.model}`} onClick={()=>{setAgent(row.agent);setModel(row.model);setSelected(null)}} className="mvpClickable"><td>{names[row.agent]}</td><td>{row.model}</td><td>{fmt(row.requests)}</td><td>{fmt(row.fresh_input_tokens)}</td><td>{fmt(row.cached_input_tokens)}</td><td>{fmt(row.cache_creation_tokens)}</td><td>{fmt(row.output_tokens)}</td><td>{fmt(row.total_tokens)}</td></tr>)}</tbody></table></div>{!data?.models.length&&<p className="mvpEmpty">所选范围没有可核对的用量记录</p>}</section><section className="mvpPanel"><div className="mvpPanelHead"><h2>活跃会话</h2><span>{data?.session_count||0} 条</span></div><div className="mvpSessions">{data?.sessions.slice(0,30).map(row=><button key={`${row.agent}:${row.native_id}`} onClick={()=>setSelected(row)} aria-pressed={selected?.native_id===row.native_id&&selected.agent===row.agent}><b>{names[row.agent]} · {row.title}</b><small>{when(row.last_request)} · {fmt(row.requests)} {row.agent==='hermes'?'调用':'请求'} · {fmt(row.total_tokens)} Token</small></button>)}</div>{!data?.sessions.length&&<p className="mvpEmpty">暂无可关联的会话</p>}</section></div>
    {selected&&<section className="mvpPanel"><div className="mvpPanelHead"><div><h2>{names[selected.agent]} · {selected.title}</h2><p>{selected.agent==='hermes'?'以下是会话／模型汇总，不是逐请求记录，也无法拆分到某一天。':`共 ${requestCount} 条请求；显示最近 100 条。`}</p></div><button onClick={()=>setSelected(null)}>关闭</button></div><div className="mvpTableWrap"><table><thead><tr><th>{selected.agent==='hermes'?'末次使用':'时间'}</th><th>模型</th><th>新输入</th><th>缓存读</th><th>缓存写</th><th>输出</th><th>总量</th></tr></thead><tbody>{requests?.map(row=><tr key={row.request_id}><td>{when(row.occurred_at)}</td><td>{row.model}</td><td>{fmt(row.fresh_input_tokens)}</td><td>{fmt(row.cached_input_tokens)}</td><td>{fmt(row.cache_creation_tokens)}</td><td>{fmt(row.output_tokens)}</td><td>{fmt(row.total_tokens)}</td></tr>)}</tbody></table></div>{requests===null&&<p className="mvpEmpty">正在加载用量记录…</p>}</section>}
    <p className="mvpFoot">本阶段提供本机用量统计；历史对话、文件与跨设备管理仍待后续开发。源日志不存在的时段不会被补造。</p></main></div>
}

createRoot(document.getElementById('root')!).render(<App/>)
