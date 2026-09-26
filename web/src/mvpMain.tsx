import React, {useEffect, useState} from 'react'
import {createRoot} from 'react-dom/client'
import './mvp.css'

type Totals={requests:number,input_tokens:number,cached_input_tokens:number,fresh_input_tokens:number,output_tokens:number,total_tokens:number,cache_hit_rate?:number|null}
type Day={day:string}&Totals
type Model={model:string}&Totals
type Session={id:string|null,native_id:string,title:string,last_request:string,requests:number,total_tokens:number}
type Usage={status:string,source:string,sync:{status:string,files?:number,updated_files?:number,deferred_files?:number},summary:Totals,daily:Day[],models:Model[],sessions:Session[],session_count:number,coverage:{files_scanned:number,files_deferred:number,unlinked_sessions:number},note:string}
type Request={request_id:string,occurred_at:string,model:string,input_tokens:number,cached_input_tokens:number,fresh_input_tokens:number,output_tokens:number}
const fmt=(n:number)=>new Intl.NumberFormat('zh-CN').format(n)
const shift=(day:string,n:number)=>{const d=new Date(`${day}T12:00:00Z`);d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)}
const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())
const when=(value:string)=>new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Hong_Kong',hour12:false})

async function get<T>(path:string):Promise<T>{const r=await fetch(path,{credentials:'same-origin'});if(!r.ok)throw new Error(`${r.status}: ${(await r.text()).slice(0,120)}`);return r.json()}
async function post<T>(path:string,body?:object,csrf?:string):Promise<T>{const r=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',...(csrf?{'x-awb-csrf':csrf}:{})},body:body?JSON.stringify(body):undefined});if(!r.ok)throw new Error(`${r.status}: ${(await r.text()).slice(0,120)}`);return r.json()}

function App(){
  const [csrf,setCsrf]=useState('')
  const [entry,setEntry]=useState<'checking'|'setup'|'login'|'unavailable'>('checking')
  const [password,setPassword]=useState('')
  const [confirmation,setConfirmation]=useState('')
  const [error,setError]=useState('')
  const [stopped,setStopped]=useState(false)
  const [date,setDate]=useState(today)
  const [range,setRange]=useState(7)
  const [model,setModel]=useState('')
  const [choices,setChoices]=useState<string[]>([])
  const [data,setData]=useState<Usage|null>(null)
  const [loading,setLoading]=useState(false)
  const [tick,setTick]=useState(0)
  const [selected,setSelected]=useState<Session|null>(null)
  const [requests,setRequests]=useState<Request[]|null>(null)
  const [requestCount,setRequestCount]=useState(0)
  const from=shift(date,1-range)
  useEffect(()=>{get<{csrf:string}>('/auth/me').then(x=>{setCsrf(x.csrf);setEntry('login')}).catch(()=>get<{needs_setup:boolean,web_setup_available:boolean}>('/auth/setup-status').then(x=>setEntry(x.needs_setup?(x.web_setup_available?'setup':'unavailable'):'login')).catch(()=>setError('无法连接工作台服务')))},[])
  useEffect(()=>{if(!csrf)return;let cancelled=false;const params=new URLSearchParams({day:from,through:date,tz:'Asia/Hong_Kong'});if(model)params.set('model',model);setLoading(true);get<Usage>(`/v1/mvp/usage?${params}`).then(x=>{if(!cancelled){setData(x);setError('');if(!model)setChoices(x.models.map(m=>m.model))}}).catch(e=>{if(!cancelled)setError(`用量读取失败：${String(e)}`)}).finally(()=>{if(!cancelled)setLoading(false)});return()=>{cancelled=true}},[csrf,from,date,model,tick])
  useEffect(()=>{if(!selected){setRequests(null);return}let cancelled=false;const params=new URLSearchParams({day:from,through:date,tz:'Asia/Hong_Kong',limit:'100'});if(model)params.set('model',model);get<{items:Request[],count:number}>(`/v1/mvp/usage/sessions/${encodeURIComponent(selected.native_id)}/requests?${params}`).then(x=>{if(!cancelled){setRequests(x.items);setRequestCount(x.count)}}).catch(()=>{if(!cancelled){setRequests([]);setRequestCount(0)}});return()=>{cancelled=true}},[selected,from,date,model,tick])
  async function login(e:React.FormEvent){e.preventDefault();setError('');try{const x=await post<{csrf:string}>('/auth/login',{password});setCsrf(x.csrf);setPassword('')}catch(e){setError(`登录失败：${String(e)}`)}}
  async function setup(e:React.FormEvent){e.preventDefault();setError('');if(password.length<12||password!==confirmation){setError('密码至少 12 个字符，且两次输入一致');return}try{const x=await post<{csrf:string}>('/auth/setup',{password,confirmation});setCsrf(x.csrf);setPassword('');setConfirmation('')}catch(e){setError(`设置失败：${String(e)}`)}}
  async function logout(){try{await post('/auth/logout',undefined,csrf)}finally{setCsrf('');setData(null);setSelected(null)}}
  async function shutdown(){if(!window.confirm('关闭工作台？'))return;try{await post('/v1/local/shutdown',undefined,csrf);setStopped(true)}catch(e){setError(String(e))}}
  if(stopped)return <main className="mvpLogin"><div className="mvpLoginCard"><h1>工作台已关闭</h1><p>从开始菜单重新打开即可继续使用。</p></div></main>
  if(!csrf)return <main className="mvpLogin"><div className="mvpLoginCard"><div className="mvpMark">✳</div><h1>{entry==='setup'?'创建管理员密码':'Agent Workbench'}</h1><p>{entry==='setup'?'首次使用，在网页中创建密码。':entry==='login'?'登录后查看本机用量仪表盘。':entry==='checking'?'正在连接…':'请在 Windows 本机打开桌面程序完成设置。'}</p>{(entry==='setup'||entry==='login')&&<form onSubmit={entry==='setup'?setup:login}><label>管理员密码<input autoFocus required type="password" minLength={entry==='setup'?12:undefined} value={password} onChange={e=>setPassword(e.target.value)}/></label>{entry==='setup'&&<label>确认密码<input required type="password" value={confirmation} onChange={e=>setConfirmation(e.target.value)}/></label>}<button type="submit">{entry==='setup'?'创建并进入':'登录'}</button></form>}{error&&<p className="mvpError" role="alert">{error}</p>}</div></main>
  const summary=data?.summary
  const max=Math.max(1,...(data?.daily||[]).map(x=>x.total_tokens))
  const cards=summary?[
    ['请求记录',fmt(summary.requests),'从 Codex 原生日志识别的请求'],
    ['处理 Token',fmt(summary.total_tokens),'原始输入（含缓存）+ 输出'],
    ['新输入',fmt(summary.fresh_input_tokens),'输入减去缓存读取'],
    ['缓存读取',fmt(summary.cached_input_tokens),summary.cache_hit_rate===null?'缓存率未知':`占输入 ${(summary.cache_hit_rate!*100).toFixed(1)}%`],
    ['输出',fmt(summary.output_tokens),'模型生成的 Token'],
    ['活跃会话',fmt(data!.session_count),'所选范围内有用量的会话'],
  ]:[]
  return <div className="mvpShell"><header className="mvpHeader"><div className="mvpBrand"><span className="mvpMark">✳</span><span><b>Agent Workbench</b><small>Windows 用量 MVP</small></span></div><div className="mvpHeaderActions"><button onClick={()=>setTick(x=>x+1)} disabled={loading}>{loading?'同步中…':'刷新'}</button><button onClick={logout}>退出</button><button onClick={shutdown}>关闭工作台</button></div></header><main className="mvpMain"><div className="mvpTitle"><div><p>USAGE / CODEX</p><h1>用量仪表盘</h1><span>先把 Token 和会话统计做准，再扩展其他能力。</span></div><span className="mvpScope">Codex · 本机原生日志</span></div>
    <section className="mvpFilters" aria-label="用量筛选"><div className="mvpRange" role="group" aria-label="日期范围">{[1,7,10,15].map(n=><button key={n} aria-pressed={range===n} onClick={()=>{setRange(n);setSelected(null)}}>{n===1?'单日':`近 ${n} 天`}</button>)}</div><label>结束日期 <input type="date" value={date} onChange={e=>{setDate(e.target.value);setSelected(null)}}/></label><label>模型 <select value={model} onChange={e=>{setModel(e.target.value);setSelected(null)}}><option value="">全部模型</option>{choices.map(x=><option key={x} value={x}>{x}</option>)}</select></label><span>{from} 至 {date} · 香港时间</span></section>
    {error&&<div className="mvpError" role="alert">{error}</div>}
    {data?.status==='source_missing'&&<div className="mvpNotice">没有找到本机 Codex 会话目录。请先确认 Codex 已在这台电脑使用过。</div>}
    {data&&data.coverage.files_deferred>0&&<div className="mvpNotice">{data.coverage.files_deferred} 份日志暂未计入，通常是父会话关系或原生身份无法确认；总量只包含已核对记录。</div>}
    {data&&<p className="mvpProvenance">来源：{data.source||'Codex JSONL'}。已扫描 {fmt(data.coverage.files_scanned)} 份日志；本轮更新 {fmt(data.sync.updated_files||0)} 份。费用尚未计价，不能把订阅用量换算为账单。其他 Agent 尚未纳入 Token 合计。</p>}
    <section className="mvpCards" aria-label="核心指标">{cards.map(([label,value,note])=><article className="mvpCard" key={label}><small>{label}</small><strong>{value}</strong><span>{note}</span></article>)}</section>
    <section className="mvpPanel"><div className="mvpPanelHead"><h2>每日用量</h2><span>按请求发生时间归到香港日期</span></div><div className="mvpTrend">{data?.daily.map(row=><div className="mvpTrendRow" key={row.day}><time>{row.day.slice(5)}</time><div className="mvpBar"><span style={{width:`${row.total_tokens/max*100}%`}}/></div><b>{fmt(row.total_tokens)}</b><small>{fmt(row.requests)} 请求</small></div>)}</div></section>
    <div className="mvpTwo"><section className="mvpPanel"><div className="mvpPanelHead"><h2>模型用量</h2><span>缓存读取已包含在原始输入中</span></div><div className="mvpTableWrap"><table><thead><tr><th>模型</th><th>请求</th><th>新输入</th><th>缓存读取</th><th>输出</th></tr></thead><tbody>{data?.models.map(row=><tr key={row.model}><td>{row.model}</td><td>{fmt(row.requests)}</td><td>{fmt(row.fresh_input_tokens)}</td><td>{fmt(row.cached_input_tokens)}</td><td>{fmt(row.output_tokens)}</td></tr>)}</tbody></table></div>{!data?.models.length&&<p className="mvpEmpty">所选范围没有已记录的请求</p>}</section><section className="mvpPanel"><div className="mvpPanelHead"><h2>活跃会话</h2><span>{data?.session_count||0} 条</span></div><div className="mvpSessions">{data?.sessions.slice(0,20).map(row=><button key={row.native_id} onClick={()=>setSelected(row)} aria-pressed={selected?.native_id===row.native_id}><b>{row.title}</b><small>{when(row.last_request)} · {fmt(row.requests)} 请求 · {fmt(row.total_tokens)} Token</small></button>)}</div>{!data?.sessions.length&&<p className="mvpEmpty">暂无可关联的会话</p>}</section></div>
    {selected&&<section className="mvpPanel"><div className="mvpPanelHead"><div><h2>{selected.title}</h2><p>最近 {requestCount} 条请求中的前 100 条 · 仅显示用量，不读取聊天正文</p></div><button onClick={()=>setSelected(null)}>关闭</button></div><div className="mvpTableWrap"><table><thead><tr><th>时间</th><th>模型</th><th>原始输入</th><th>缓存读取</th><th>输出</th></tr></thead><tbody>{requests?.map(row=><tr key={row.request_id}><td>{when(row.occurred_at)}</td><td>{row.model}</td><td>{fmt(row.input_tokens)}</td><td>{fmt(row.cached_input_tokens)}</td><td>{fmt(row.output_tokens)}</td></tr>)}</tbody></table></div>{requests===null&&<p className="mvpEmpty">正在加载请求记录…</p>}</section>}
    <p className="mvpFoot">本阶段只验收用量与会话统计。历史对话、文件、跨设备管理暂不作为可用功能提供。</p></main></div>
}

createRoot(document.getElementById('root')!).render(<App/>)
