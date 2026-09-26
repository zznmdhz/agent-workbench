import React, {useEffect, useState} from 'react'
import {createRoot} from 'react-dom/client'
import {Overview} from './Overview'
import {SessionWorkspace, type Focus, type Session, type SessionTab} from './SessionWorkspace'
import {BackfillPanel} from './BackfillPanel'
import {ResourcePanel} from './ResourcePanel'
import type {FilterValues} from './FilterBar'
import './style.css'

type Metric={id:string,value:number|null,unit:string,quality_note:string,included_count:number,excluded_count:number,verification_state?:string}
type UsageExplanation={verification_state:string,cached_input_is_subset_of_input:boolean,cached_input_share_of_input?:number|null,non_cached_input_tokens?:number|null,counter_arithmetic_gap:number|null,arithmetic_note:string,counter_unallocated_intervals:number,incremental_and_cumulative_overlap:string}
type Trend={day:string,runs:number,completed:number,human_chars:number,counter_tokens:number}
type Comparison={device_id:string,runs:number,settled_duration_ms:number,active_wall_ms:number,human_chars:number,counter_tokens:number}
type Device={id:string,name:string,os:string,environment:string,last_heartbeat:string|null,pending_count:number}
type Source={id:string,device_id:string,agent:string,profile:string,last_error:string|null,last_scan:string|null,execution_surface:string,capability_json:string,message_count?:number,readable_count?:number,file_evidence_count?:number,scan_state?:string}
type View='overview'|'sessions'|'devices'
type Route=FilterValues&{view:View,sessionId:string|null,focus:Focus,tab:SessionTab}

async function request<T>(path:string,options:RequestInit={},csrf?:string):Promise<T>{
  const headers:Record<string,string>={'Content-Type':'application/json',...(options.headers as Record<string,string>||{})}
  if(csrf&&options.method&&options.method!=='GET')headers['x-awb-csrf']=csrf
  const response=await fetch(path,{...options,headers,credentials:'same-origin'})
  if(!response.ok)throw new Error(`${response.status}: ${(await response.text()).slice(0,220)}`)
  return response.json()
}

const dateLocal=(tz='Asia/Hong_Kong')=>new Intl.DateTimeFormat('en-CA',{timeZone:tz,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())
const time=(value:string|null,tz:string)=>value?new Date(value).toLocaleString('zh-CN',{timeZone:tz}):'未知'
const shiftDay=(day:string,offset:number)=>{const date=new Date(`${day}T12:00:00Z`);date.setUTCDate(date.getUTCDate()+offset);return date.toISOString().slice(0,10)}

function readRoute():Route{
  const p=new URLSearchParams(window.location.search)
  const view=p.get('view')
  const tab=p.get('tab')
  const focus=p.get('focus')
  const range=Number(p.get('range'))
  return {
    view:view==='sessions'||view==='devices'?view:'overview',
    date:/^\d{4}-\d{2}-\d{2}$/.test(p.get('date')||'')?p.get('date')!:dateLocal(),
    rangeDays:[1,7,10,15].includes(range)?range:1,
    tz:p.get('tz')==='UTC'?'UTC':'Asia/Hong_Kong',
    deviceId:p.get('device')||'',agent:p.get('agent')||'',model:p.get('model')||'',
    sessionId:p.get('session')||null,
    focus:{kind:focus==='date'||focus==='run'||focus==='message'?focus:'latest',id:p.get('target')||undefined},
    tab:tab==='runs'||tab==='files'||tab==='evidence'?tab:'conversation',
  }
}
function routeUrl(route:Route):string{
  const p=new URLSearchParams()
  p.set('view',route.view);p.set('date',route.date);p.set('range',String(route.rangeDays))
  if(route.tz!=='Asia/Hong_Kong')p.set('tz',route.tz)
  if(route.deviceId)p.set('device',route.deviceId)
  if(route.agent)p.set('agent',route.agent)
  if(route.model)p.set('model',route.model)
  if(route.sessionId){p.set('session',route.sessionId);p.set('focus',route.focus.kind);if(route.focus.id)p.set('target',route.focus.id);if(route.tab!=='conversation')p.set('tab',route.tab)}
  return `${window.location.pathname}?${p}${window.location.hash}`
}
function sourcePolicy(source:Source):string{try{return JSON.parse(source.capability_json||'{}').content_policy||'stats_only'}catch{return '未知'}}

function App(){
  const [route,setRoute]=useState<Route>(readRoute)
  const [csrf,setCsrf]=useState('')
  const [password,setPassword]=useState('')
  const [confirmation,setConfirmation]=useState('')
  const [entry,setEntry]=useState<'checking'|'setup'|'login'|'unavailable'>('checking')
  const [stopped,setStopped]=useState(false)
  const [error,setError]=useState('')
  const [loading,setLoading]=useState(false)
  const [sessions,setSessions]=useState<Session[]>([])
  const [sessionCursor,setSessionCursor]=useState<string|null>(null)
  const [stats,setStats]=useState<Metric[]>([])
  const [usageExplanation,setUsageExplanation]=useState<UsageExplanation|null>(null)
  const [asOf,setAsOf]=useState<string|null>(null)
  const [trend,setTrend]=useState<Trend[]>([])
  const [comparison,setComparison]=useState<Comparison[]>([])
  const [devices,setDevices]=useState<Device[]>([])
  const [sources,setSources]=useState<Source[]>([])
  const [models,setModels]=useState<string[]>([])
  const [refreshTick,setRefreshTick]=useState(0)
  const [pairCode,setPairCode]=useState('')
  const [registration,setRegistration]=useState({id:'',device_id:'',agent:'codex',profile:'default',content_policy:'stats_only'})
  const pendingTotal=devices.reduce((sum,item)=>sum+item.pending_count,0)

  useEffect(()=>{
    const pop=()=>setRoute(readRoute())
    window.addEventListener('popstate',pop)
    return()=>window.removeEventListener('popstate',pop)
  },[])
  useEffect(()=>{window.history.replaceState(null,'',routeUrl(route))},[route])
  function navigate(patch:Partial<Route>){
    const next={...route,...patch}
    window.history.pushState(null,'',routeUrl(next))
    setRoute(next)
  }
  function changeFilters(patch:Partial<FilterValues>){setRoute(old=>({...old,...patch,sessionId:null,focus:{kind:'latest'},tab:'conversation'}))}
  function openSession(id:string,focus:Focus={kind:'latest'},tab:SessionTab='conversation'){
    navigate({view:'sessions',sessionId:id||null,focus,tab})
  }

  useEffect(()=>{
    request<{csrf:string}>('/auth/me').then(value=>{setCsrf(value.csrf);setEntry('login')}).catch(()=>{
      request<{needs_setup:boolean,web_setup_available:boolean}>('/auth/setup-status')
        .then(value=>setEntry(value.needs_setup?(value.web_setup_available?'setup':'unavailable'):'login'))
        .catch(()=>setError('无法连接工作台。请从开始菜单重新打开。'))
    })
  },[])

  async function refresh(quiet=false){
    if(!csrf)return
    if(!quiet){setLoading(true);setError('')}
    try{
      const start=shiftDay(route.date,1-route.rangeDays)
      const statParams=new URLSearchParams({day:start,through:route.date,tz:route.tz})
      if(route.deviceId)statParams.append('device_ids',route.deviceId)
      if(route.agent)statParams.append('agent_ids',route.agent)
      if(route.model)statParams.append('model_ids',route.model)
      const sessionParams=new URLSearchParams({limit:'100',activity_day:start,activity_through:route.date,tz:route.tz})
      if(route.deviceId)sessionParams.set('device_id',route.deviceId)
      if(route.agent)sessionParams.set('agent',route.agent)
      if(route.model)sessionParams.set('model',route.model)
      const [a,b,c,d,e]=await Promise.all([
        request<{items:Session[],next_cursor:string|null}>(`/v1/sessions?${sessionParams}`),
        request<{metrics:Metric[],daily_trend:Trend[],device_comparison:Comparison[],usage_explanation:UsageExplanation,as_of:string}>(`/v1/stats?${statParams}`),
        request<{items:Device[]}>('/v1/devices'),
        request<{items:Source[]}>('/v1/health/sources').catch(()=>request<{items:Source[]}>('/v1/sources')),
        request<{items:string[]}>('/v1/models'),
      ])
      setSessions(a.items);setSessionCursor(a.next_cursor)
      setStats(b.metrics);setTrend(b.daily_trend);setComparison(b.device_comparison);setUsageExplanation(b.usage_explanation);setAsOf(b.as_of)
      setDevices(c.items);setSources(d.items);setModels(e.items)
      if(!quiet)setRefreshTick(old=>old+1)
    }catch(reason){if(!quiet)setError(String(reason))}
    finally{if(!quiet)setLoading(false)}
  }
  useEffect(()=>{if(!csrf)return;void refresh();const timer=window.setInterval(()=>void refresh(true),15000);return()=>window.clearInterval(timer)},[csrf,route.date,route.rangeDays,route.tz,route.deviceId,route.agent,route.model])

  async function moreSessions(){
    if(!sessionCursor)return
    try{
      const start=shiftDay(route.date,1-route.rangeDays)
      const params=new URLSearchParams({limit:'100',activity_day:start,activity_through:route.date,tz:route.tz,cursor:sessionCursor})
      if(route.deviceId)params.set('device_id',route.deviceId)
      if(route.agent)params.set('agent',route.agent)
      if(route.model)params.set('model',route.model)
      const result=await request<{items:Session[],next_cursor:string|null}>(`/v1/sessions?${params}`)
      setSessions(old=>[...old,...result.items]);setSessionCursor(result.next_cursor)
    }catch(reason){setError(String(reason))}
  }

  async function doLogin(event:React.FormEvent){event.preventDefault();setError('');try{const value=await request<{csrf:string}>('/auth/login',{method:'POST',body:JSON.stringify({password})});setCsrf(value.csrf);setPassword('')}catch(reason){setError('登录失败：'+String(reason))}}
  async function doSetup(event:React.FormEvent){event.preventDefault();setError('');if(password.length<12){setError('密码至少需要 12 个字符。');return}if(password!==confirmation){setError('两次输入的密码不一致。');return}try{const value=await request<{csrf:string}>('/auth/setup',{method:'POST',body:JSON.stringify({password,confirmation})});setCsrf(value.csrf);setEntry('login');setPassword('');setConfirmation('')}catch(reason){setError('创建失败：'+String(reason))}}
  async function cancelSetup(){try{await request('/auth/cancel-setup',{method:'POST'});setStopped(true)}catch(reason){setError('关闭失败：'+String(reason))}}
  async function closeLocal(){try{await request('/auth/close-local',{method:'POST'});setStopped(true)}catch(reason){setError('关闭失败：'+String(reason))}}
  async function makePair(){try{const value=await request<{code:string}>('/v1/pairing-codes',{method:'POST'},csrf);setPairCode(value.code)}catch(reason){setError(String(reason))}}
  async function register(event:React.FormEvent){event.preventDefault();try{await request('/v1/sources/register',{method:'POST',body:JSON.stringify(registration)},csrf);setError('');void refresh()}catch(reason){setError(String(reason))}}
  async function changeLocalPolicy(sourceId:string,contentPolicy:string){try{await request(`/v1/local/sources/${sourceId}/policy`,{method:'POST',body:JSON.stringify({content_policy:contentPolicy})},csrf);void refresh()}catch(reason){setError(String(reason))}}
  async function logout(){try{await request('/auth/logout',{method:'POST'},csrf)}finally{setCsrf('');setSessions([]);setRoute(old=>({...old,sessionId:null}))}}
  async function shutdown(){if(!window.confirm('关闭工作台？关闭后可从开始菜单重新打开。'))return;try{await request('/v1/local/shutdown',{method:'POST'},csrf);setStopped(true)}catch(reason){setError('关闭失败：'+String(reason))}}

  if(stopped)return <main className="login"><div className="loginCard"><div className="brandIcon">✳</div><h1>工作台已关闭</h1><p>下次使用时，从开始菜单打开 Agent Workbench。</p></div></main>
  if(!csrf)return <main className="login"><div className="loginCard"><div className="brandIcon">✳</div><h1>{entry==='setup'?'创建管理员密码':'跨 Agent 工作台'}</h1>{entry==='checking'?<p>正在连接工作台…</p>:entry==='setup'?<><p>首次使用，请在这里创建密码。设置完成后会自动进入工作台，以后只需在此页面登录。</p><form onSubmit={doSetup}><label>管理员密码（至少 12 个字符）<input type="password" autoComplete="new-password" minLength={12} value={password} onChange={e=>setPassword(e.target.value)} required autoFocus/></label><label>再次输入密码<input type="password" autoComplete="new-password" minLength={12} value={confirmation} onChange={e=>setConfirmation(e.target.value)} required/></label><button type="submit">创建并进入工作台</button><button type="button" className="secondary" onClick={()=>void cancelSetup()}>暂不设置，关闭工作台</button></form></>:entry==='login'?<><p>输入管理员密码，查看本机 Codex 与 Hermes 的活动。</p><form onSubmit={doLogin}><label>管理员密码<input type="password" autoComplete="current-password" value={password} onChange={e=>setPassword(e.target.value)} required autoFocus/></label><button type="submit">登录</button><button type="button" className="secondary" onClick={()=>void closeLocal()}>关闭工作台</button></form><small>忘记密码？先关闭工作台，再从开始菜单选择“重设管理员密码”。便携版请双击 AgentWorkbenchReset.exe。</small></>:<p>此服务尚未设置管理员。请在 Windows 本机打开 Agent Workbench 桌面程序完成首次设置。</p>}{error&&<div className="error" role="alert">{error}</div>}</div></main>

  return <div className="shell">
    <aside className="sidebar"><div className="logo"><span className="brandIcon">✳</span><span>Agent<br/>Workbench</span></div><nav aria-label="主导航"><button aria-current={route.view==='overview'?'page':undefined} className={route.view==='overview'?'active':''} onClick={()=>navigate({view:'overview'})}>▦　总览</button><button aria-current={route.view==='sessions'?'page':undefined} className={route.view==='sessions'?'active':''} onClick={()=>navigate({view:'sessions'})}>☷　会话</button><button aria-current={route.view==='devices'?'page':undefined} className={route.view==='devices'?'active':''} onClick={()=>navigate({view:'devices'})}>◉　设备与设置</button></nav><div className="sidebarFoot"><span className="liveDot"/> 单用户工作台<br/><small>仅展示已采集证据</small><button className="logout" onClick={()=>void logout()}>退出登录</button><button className="logout" onClick={()=>void shutdown()}>关闭工作台</button></div></aside>
    <main className="main"><header className="top"><div><div className="eyebrow">WORKSPACE / {route.view==='overview'?'OVERVIEW':route.view==='sessions'?'SESSIONS':'SETTINGS'}</div><h1>{route.view==='overview'?'活动总览':route.view==='sessions'?'会话记录':'设备与设置'}</h1></div><div className="topActions"><span>{loading?'同步中…':'服务已连接'}</span><button onClick={()=>void refresh()}>刷新</button></div></header>
      {error&&<div className="error" role="alert">{error}<button onClick={()=>setError('')} aria-label="关闭错误提示">×</button></div>}
      {route.view==='overview'&&<Overview date={route.date} onDate={value=>changeFilters({date:value})} rangeDays={route.rangeDays} onRange={value=>changeFilters({rangeDays:value})} tz={route.tz} onTz={value=>changeFilters({tz:value})} deviceId={route.deviceId} onDevice={value=>changeFilters({deviceId:value})} agent={route.agent} onAgent={value=>changeFilters({agent:value})} model={route.model} onModel={value=>changeFilters({model:value})} models={models} metrics={stats} trend={trend} comparison={comparison} devices={devices} sessions={sessions} sourcesCount={sources.length} pendingTotal={pendingTotal} refreshTick={refreshTick} asOf={asOf} usageExplanation={usageExplanation} onSelect={(id,runId)=>openSession(id,runId?{kind:'run',id:runId}:{kind:'latest'},runId?'runs':'conversation')} onSessions={()=>navigate({view:'sessions',sessionId:null})} onDevices={()=>navigate({view:'devices'})}/>}
      {route.view==='sessions'&&<SessionWorkspace sessions={sessions} selectedId={route.sessionId} focus={route.focus} tab={route.tab} filters={route} devices={devices} models={models} sources={sources} csrf={csrf} sessionCursor={sessionCursor} refreshTick={refreshTick} onFilter={changeFilters} onSelect={openSession} onTab={tab=>navigate({tab})} onMoreSessions={()=>void moreSessions()} onSettings={()=>navigate({view:'devices'})} onRefresh={()=>void refresh()}/>}
      {route.view==='devices'&&<div className="settingsGrid">
        <section className="panel"><h2>已配对设备</h2><p className="muted">本机启动时自动配对并采集。服务连接只表示网页可访问，来源状态见右侧。</p>{devices.length?devices.map(device=><div className="deviceRow" key={device.id}><span className="deviceIcon">⌘</span><div><b>{device.name}</b><small>{device.id}</small><small>{device.environment}</small><small>{device.last_heartbeat?'最近心跳 '+time(device.last_heartbeat,route.tz):'等待首次采集'}</small></div><span className="badge">{device.pending_count} 待传</span></div>):<div className="empty">尚无设备</div>}</section>
        <section className="panel"><h2>本机数据来源</h2><p className="muted">默认只保存统计。开启正文后，后续消息才会保存脱敏内容；历史需要在下方按范围补采。这里的条数只代表已索引证据，不代表来源历史全部已采集。</p>{sources.length?sources.map(source=><div className="sourceRow" key={source.id}><b>{source.agent} · {source.profile}</b><small>{source.last_error?'错误：'+source.last_error:source.last_scan?'最近扫描 '+time(source.last_scan,route.tz):'尚未扫描'}{source.scan_state?` · ${source.scan_state}`:''}</small>{source.message_count!==undefined&&<small>已索引消息 {source.message_count}；可读正文 {source.readable_count??0}；已记录文件证据 {source.file_evidence_count??0}（0 不代表无文件）</small>}{source.execution_surface==='local'&&<label className="policyLabel">后续消息采集<select aria-label={`${source.agent} 正文采集策略`} value={sourcePolicy(source)} onChange={event=>void changeLocalPolicy(source.id,event.target.value)}><option value="stats_only">仅统计</option><option value="full_content">保存脱敏后的正文</option><option value="excluded">排除</option></select></label>}</div>):<div className="empty">当前没有发现 Codex 或 Hermes 来源</div>}</section>
        <BackfillPanel sources={sources} csrf={csrf} onChanged={()=>void refresh()}/>
        <ResourcePanel devices={devices}/>
        <section className="panel advancedPanel"><details><summary>添加另一台电脑</summary><p className="muted">先生成配对码，再在另一台电脑运行采集器。当前 Mac/NAS 尚未实机验收。</p><button onClick={()=>void makePair()}>生成 10 分钟配对码</button>{pairCode&&<div className="pairCode" aria-label="配对码">{pairCode}</div>}<form className="register" onSubmit={register}><label>来源 ID<input value={registration.id} onChange={event=>setRegistration({...registration,id:event.target.value})} required/></label><label>设备 ID<select value={registration.device_id} onChange={event=>setRegistration({...registration,device_id:event.target.value})} required><option value="">选择设备</option>{devices.map(device=><option key={device.id} value={device.id}>{device.name}</option>)}</select></label><label>Agent<select value={registration.agent} onChange={event=>setRegistration({...registration,agent:event.target.value})}><option value="codex">Codex</option><option value="hermes">Hermes</option></select></label><label>Profile<input value={registration.profile} onChange={event=>setRegistration({...registration,profile:event.target.value})}/></label><label>采集策略<select value={registration.content_policy} onChange={event=>setRegistration({...registration,content_policy:event.target.value})}><option value="stats_only">仅统计</option><option value="full_content">正文脱敏后采集</option><option value="excluded">排除</option></select></label><button type="submit">确认登记</button></form></details></section>
      </div>}
    </main>
  </div>
}

createRoot(document.getElementById('root')!).render(<App/>)
