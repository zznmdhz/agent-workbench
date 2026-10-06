import {useEffect, useState} from 'react'
import {createRoot} from 'react-dom/client'
import './mvp.css'
import BackgroundStatus from './BackgroundStatus'
import {ModelReport, type ModelReportData, type ReportModel} from './ModelReport'

type Agent='codex'|'claude'|'hermes'
type Totals={requests:number,input_tokens:number,fresh_input_tokens:number,cached_input_tokens:number,cache_creation_tokens:number,output_tokens:number,total_tokens:number,cache_hit_rate?:number|null}
type Source=Totals&{status:string,precision:'request'|'session_model_aggregate',earliest:string|null,latest:string|null,files:number,deferred:number,partial_rows?:number,updated_files?:number,missing_cache_read?:number,missing_cache_write?:number}
type Trend=Totals&{period:string}
type Model=Totals&{agent:Agent,model:string}
type Session=Totals&{agent:Agent,native_id:string,device_id:string,title:string,last_request:string}
type HeatView='year'|'month'|'week'|'day'|'custom'
type Usage={status:string,summary:Totals,sources:Record<Agent,Source>,models:Model[],sessions:Session[],session_count:number,trend:Trend[],trend_granularity:'day'|'week'|'month',heatmap:Trend[],heatmap_view:HeatView,heatmap_granularity:'day'|'hour'|'month',unattributed_tokens:number,note:string}
type TimeCell={period:string,agent_ms:number,wall_ms:number,verified_ms:number,runs:number}
type TimeTotals=Omit<TimeCell,'period'>
type DaySession=TimeTotals&{agent:Agent,native_id:string,device_id:string,title:string,first_at:string,last_at:string,messages:number,user_turns:number,text_chars:number,file_count:number,storage_bytes:number|null,storage_kind:'record'|'payload',can_open_folder:boolean,preview:string,match?:{type:string,excerpt:string}}
type Activity={summary:TimeTotals,by_agent:Record<Agent,TimeTotals>,heatmap:TimeCell[],heatmap_granularity:'day'|'hour'|'month',focus_day:string,note:string,source:{updated_files?:number,read_errors?:number,hermes_ready:boolean}}
type SessionBrowser={day:string,through:string,query:string,counts:Record<Agent,number>,session_count:number,sessions:DaySession[]}
type Conversation={count:number,items:{id:string,role:string,occurred_at:string,body:string}[]}
type SessionInspector={agent:Agent,native_id:string,cwd:string|null,sources:{path:string,bytes:number|null,status:string}[],record_bytes:number|null,payload_bytes:number|null,file_events:{occurred_at:string,native_path:string,relation:string,evidence:string,source_file:string,current_bytes:number|null,current_state:string}[],unique_file_count:number,possible_file_count:number,confirmed_event_count:number,coverage:string}
type UpdateStatus={available:boolean,current_version:string,state:'idle'|'available'|'current'|'downloading'|'installing'|'error',latest_version:string|null,progress:number,error:string|null}
type ArchiveStatus={local_device_id:string,sync_root:string|null,sync_ready:boolean,devices:{id:string,name:string,os:string,local:boolean,facts:number,last_imported:string|null}[]}
type HeatHover={row:Trend|TimeCell,x:number,y:number,below:boolean,note:string}
const names:Record<Agent,string>={codex:'Codex',claude:'Claude',hermes:'Hermes'}
const fmt=(n:number)=>new Intl.NumberFormat('zh-CN').format(n)
const bytes=(n:number|null)=>n===null?'无法单独计算':n<1024?`${fmt(n)} B`:n<1048576?`${(n/1024).toFixed(1)} KB`:`${(n/1048576).toFixed(1)} MB`
const duration=(ms:number)=>{const m=Math.round(ms/60000);return m>=60?`${Math.floor(m/60)} 小时 ${m%60} 分钟`:m?`${m} 分钟`:ms>0?'<1 分钟':'0 分钟'}
const shift=(day:string,n:number)=>{const d=new Date(`${day}T12:00:00Z`);d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)}
const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())
const yearStart=()=>`${today().slice(0,4)}-01-01`
const when=(value:string|null)=>value?new Date(value).toLocaleString('sv-SE',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'无记录'
const dayOf=(value:string|null)=>value?new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value)):'无记录'
const sourceStatus=(row:Source)=>row.status==='source_missing'?'底库暂无记录':row.status==='read_error'?'读取失败':row.deferred>0?`${row.deferred} 项暂未计入`:row.status==='archived'?'已归档':'已读取'
const lastDayOfMonth=(year:number,month:number)=>new Date(Date.UTC(year,month,0)).toISOString().slice(0,10)
function periodBounds(view:Exclude<HeatView,'custom'>,anchor:string):[string,string]{
  if(view==='day')return [anchor,anchor]
  if(view==='year')return [`${anchor.slice(0,4)}-01-01`,`${anchor.slice(0,4)}-12-31`]
  if(view==='month')return [`${anchor.slice(0,7)}-01`,lastDayOfMonth(Number(anchor.slice(0,4)),Number(anchor.slice(5,7)))]
  const weekday=(new Date(`${anchor}T12:00:00Z`).getUTCDay()+6)%7
  const monday=shift(anchor,-weekday)
  return [monday,shift(monday,6)]
}
function movePeriod(view:Exclude<HeatView,'custom'>,anchor:string,delta:number){
  if(view==='day')return shift(anchor,delta)
  if(view==='week')return shift(anchor,delta*7)
  const d=new Date(`${anchor.slice(0,7)}-01T12:00:00Z`)
  d.setUTCMonth(d.getUTCMonth()+(view==='year'?delta*12:delta))
  return d.toISOString().slice(0,10)
}
const periodLabel=(view:HeatView,start:string,end:string)=>view==='year'?`${start.slice(0,4)} 年`:view==='month'?`${start.slice(0,7)} 月`:view==='week'?`${start}—${end}`:view==='day'?`${start} · 24 小时`:`${start}—${end}`
function UsageSparkline({rows,grain}:{rows:Trend[],grain:'day'|'week'|'month'}){
  const values=rows.map(row=>row.total_tokens)
  if(!values.some(value=>value>0))return <div className="mvpSparklineEmpty">此筛选范围没有可按时间绘制的逐次请求</div>
  const peak=Math.max(...values)
  const points=values.map((value,index)=>`${2+index*396/Math.max(1,values.length-1)},${54-value/peak*42}`).join(' ')
  const unit=grain==='day'?'日':grain==='week'?'周':'月'
  return <div className="mvpSparkline"><svg viewBox="0 0 400 64" preserveAspectRatio="none" role="img" aria-label={`按${unit}统计的处理 Token 趋势；仅包含能按请求时间归属的 Codex 和 Claude 用量`}><polyline points={points} fill="none" stroke="var(--brand-700)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke"/></svg><span>逐{unit}趋势 · Codex / Claude 请求</span></div>
}
function heatThresholds(values:number[]){
  const sorted=values.filter(value=>value>0).sort((a,b)=>a-b)
  return [0.2,0.4,0.6,0.8].map(p=>sorted[Math.min(sorted.length-1,Math.floor(sorted.length*p))]??0)
}
function heatLevel(value:number,thresholds:number[]){
  if(value<=0)return 0
  const rank=thresholds.findIndex(cutoff=>value<=cutoff)
  return rank<0?5:rank+1
}

async function get<T>(path:string):Promise<T>{const r=await fetch(path,{credentials:'same-origin'});if(!r.ok)throw new Error(`${r.status}: ${(await r.text()).slice(0,120)}`);return r.json()}
async function post<T>(path:string,body?:object,csrf?:string):Promise<T>{const r=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',...(csrf?{'x-awb-csrf':csrf}:{})},body:body?JSON.stringify(body):undefined});if(!r.ok)throw new Error(`${r.status}: ${(await r.text()).slice(0,120)}`);return r.json()}

function App(){
  const [csrf,setCsrf]=useState('')
  const [error,setError]=useState('')
  const [stopped,setStopped]=useState(false)
  const [update,setUpdate]=useState<UpdateStatus|null>(null)
  const [archiveStatus,setArchiveStatus]=useState<ArchiveStatus|null>(null)
  const [deviceId,setDeviceId]=useState('')
  const [syncRootInput,setSyncRootInput]=useState('')
  const [syncMessage,setSyncMessage]=useState('')
  const [start,setStart]=useState(yearStart)
  const [end,setEnd]=useState(today)
  const [preset,setPreset]=useState('year')
  const [heatView,setHeatView]=useState<HeatView>('year')
  const [agent,setAgent]=useState<Agent|''>('')
  const [model,setModel]=useState('')
  const [choices,setChoices]=useState<string[]>([])
  const [data,setData]=useState<Usage|null>(null)
  const [report,setReport]=useState<ModelReportData|null>(null)
  const [loading,setLoading]=useState(false)
  const [refreshing,setRefreshing]=useState(false)
  const [heatHover,setHeatHover]=useState<HeatHover|null>(null)
  const [heatFocus,setHeatFocus]=useState(0)
  const [activeSection,setActiveSection]=useState('overview')
  const [heatMetric,setHeatMetric]=useState<'token'|'time'>('token')
  const [focusDay,setFocusDay]=useState(today)
  const [activity,setActivity]=useState<Activity|null>(null)
  const [activityLoading,setActivityLoading]=useState(false)
  const [activityError,setActivityError]=useState('')
  const [sessionStart,setSessionStart]=useState(today)
  const [sessionEnd,setSessionEnd]=useState(today)
  const [sessionAgent,setSessionAgent]=useState<Agent|''>('')
  const [sessionSearch,setSessionSearch]=useState('')
  const [sessionQuery,setSessionQuery]=useState('')
  const [sessionSearchIn,setSessionSearchIn]=useState('all')
  const [sessionSort,setSessionSort]=useState('recent')
  const [sessionMinText,setSessionMinText]=useState(0)
  const [sessionMinDuration,setSessionMinDuration]=useState(0)
  const [sessionHasFiles,setSessionHasFiles]=useState(false)
  const [sessionBrowser,setSessionBrowser]=useState<SessionBrowser|null>(null)
  const [sessionLoading,setSessionLoading]=useState(false)
  const [sessionError,setSessionError]=useState('')
  const [daySession,setDaySession]=useState<DaySession|null>(null)
  const [sessionTab,setSessionTab]=useState<'conversation'|'files'>('conversation')
  const [conversationView,setConversationView]=useState<'key'|'full'>('key')
  const [inspector,setInspector]=useState<SessionInspector|null>(null)
  const [inspectorError,setInspectorError]=useState('')
  const [folderStatus,setFolderStatus]=useState('')
  const [conversation,setConversation]=useState<Conversation|null>(null)
  const [conversationOffset,setConversationOffset]=useState(0)
  const [tick,setTick]=useState(0)
  const invalidRange=!start||!end||start>end
  const invalidSessionRange=!sessionStart||!sessionEnd||sessionStart>sessionEnd

  useEffect(()=>{const timer=window.setTimeout(()=>setSessionQuery(sessionSearch.trim()),350);return()=>window.clearTimeout(timer)},[sessionSearch])
  useEffect(()=>{
    const observer=new IntersectionObserver(entries=>{
      const visible=entries.filter(entry=>entry.isIntersecting).sort((a,b)=>a.boundingClientRect.top-b.boundingClientRect.top)
      if(visible[0])setActiveSection(visible[0].target.id)
    },{rootMargin:'-90px 0px -65% 0px'})
    document.querySelectorAll('[data-dashboard-section]').forEach(element=>observer.observe(element))
    return()=>observer.disconnect()
  },[csrf])
  useEffect(()=>{
    if(!sessionBrowser?.sessions.length||window.matchMedia('(max-width: 680px)').matches)return
    setDaySession(current=>sessionBrowser.sessions.find(row=>row.device_id===current?.device_id&&row.agent===current?.agent&&row.native_id===current?.native_id)||sessionBrowser.sessions[0])
  },[sessionBrowser])

  useEffect(()=>{get<{csrf:string}>('/auth/me').then(x=>setCsrf(x.csrf)).catch(()=>setError('无法连接本机工作台，请重新打开页面'))},[])
  useEffect(()=>{if(csrf)get<ArchiveStatus>('/v1/archive/status').then(x=>{setArchiveStatus(x);setSyncRootInput(x.sync_root||'')}).catch(e=>setError(`底库读取失败：${String(e)}`))},[csrf,tick])
  useEffect(()=>{
    if(!csrf)return
    let cancelled=false
    get<UpdateStatus>('/v1/local/update?check=true').then(async x=>{
      if(cancelled)return
      setUpdate(x)
    }).catch(e=>{if(!cancelled)setUpdate({available:true,current_version:'',state:'error',latest_version:null,progress:0,error:String(e)})})
    return()=>{cancelled=true}
  },[csrf])
  useEffect(()=>{
    if(update?.state!=='downloading'&&update?.state!=='installing')return
    const timer=window.setInterval(async()=>{
      try{
        const ready=await get<{app_version:string}>('/health/ready')
        if(update.latest_version&&ready.app_version===update.latest_version){window.location.reload();return}
        const status=await get<UpdateStatus>('/v1/local/update')
        setUpdate(status)
      }catch{ /* The old server is expected to stop while the installer runs. */ }
    },2000)
    return()=>window.clearInterval(timer)
  },[update?.state,update?.latest_version])
  useEffect(()=>{
    if(!csrf||invalidRange)return
    let cancelled=false
    const params=new URLSearchParams({day:start,through:end,tz:'Asia/Hong_Kong'})
    if(agent)params.set('agent',agent)
    if(model)params.set('model',model)
    if(deviceId)params.set('device_id',deviceId)
    setReport(null)
    get<ModelReportData>(`/v1/mvp/usage/report?${params}`).then(x=>{if(!cancelled)setReport(x)}).catch(e=>{if(!cancelled)setError(`模型报表读取失败：${String(e)}`)})
    return()=>{cancelled=true}
  },[csrf,start,end,agent,model,deviceId,tick,invalidRange])
  useEffect(()=>{
    if(!csrf||invalidRange)return
    let cancelled=false
    const params=new URLSearchParams({day:start,through:end,tz:'Asia/Hong_Kong',heatmap_view:heatView})
    if(agent)params.set('agent',agent)
    if(model)params.set('model',model)
    if(deviceId)params.set('device_id',deviceId)
    setLoading(true)
    setData(null)
    setHeatHover(null)
    get<Usage>(`/v1/mvp/usage?${params}`).then(x=>{if(!cancelled){setData(x);setError('');if(!model)setChoices(x.models.map(m=>m.model).filter((v,i,a)=>a.indexOf(v)===i).sort())}}).catch(e=>{if(!cancelled)setError(`用量读取失败：${String(e)}`)}).finally(()=>{if(!cancelled)setLoading(false)})
    return()=>{cancelled=true}
  },[csrf,start,end,agent,model,deviceId,heatView,tick,invalidRange])
  useEffect(()=>{
    if(!csrf||invalidRange)return
    let cancelled=false
    const chosen=focusDay>=start&&focusDay<=end?focusDay:start
    const params=new URLSearchParams({day:start,through:end,tz:'Asia/Hong_Kong',heatmap_view:heatView,focus_day:chosen})
    if(agent)params.set('agent',agent)
    if(deviceId)params.set('device_id',deviceId)
    setActivityLoading(true)
    get<Activity>(`/v1/mvp/activity?${params}`).then(x=>{if(!cancelled){setActivity(x);setActivityError('')}}).catch(e=>{if(!cancelled)setActivityError(`会话读取失败：${String(e)}`)}).finally(()=>{if(!cancelled)setActivityLoading(false)})
    return()=>{cancelled=true}
  },[csrf,start,end,heatView,focusDay,agent,deviceId,tick,invalidRange])
  useEffect(()=>{
    if(!csrf||invalidSessionRange)return
    let cancelled=false
    const params=new URLSearchParams({day:sessionStart,through:sessionEnd,tz:'Asia/Hong_Kong',search_in:sessionSearchIn,sort:sessionSort,min_text:String(sessionMinText),min_duration:String(sessionMinDuration),has_files:String(sessionHasFiles)})
    if(sessionAgent)params.set('agent',sessionAgent)
    if(sessionQuery)params.set('q',sessionQuery)
    if(deviceId)params.set('device_id',deviceId)
    setSessionLoading(true)
    setSessionBrowser(null)
    setSessionError('')
    get<SessionBrowser>(`/v1/mvp/activity/sessions?${params}`).then(x=>{if(!cancelled){setSessionBrowser(x);setSessionError('')}}).catch(e=>{if(!cancelled)setSessionError(`会话读取失败：${String(e)}`)}).finally(()=>{if(!cancelled)setSessionLoading(false)})
    return()=>{cancelled=true}
  },[csrf,sessionStart,sessionEnd,sessionAgent,sessionQuery,sessionSearchIn,sessionSort,sessionMinText,sessionMinDuration,sessionHasFiles,deviceId,tick,invalidSessionRange])
  useEffect(()=>{
    if(!daySession)return
    let cancelled=false
    setConversation(null)
    const params=new URLSearchParams({day:sessionStart,through:sessionEnd,tz:'Asia/Hong_Kong',view:conversationView,offset:String(conversationOffset),limit:'100'})
    params.set('device_id',daySession.device_id)
    get<Conversation>(`/v1/mvp/activity/sessions/${daySession.agent}/${encodeURIComponent(daySession.native_id)}/conversation?${params}`).then(x=>{if(!cancelled)setConversation(x)}).catch(()=>{if(!cancelled)setConversation({items:[],count:0})})
    return()=>{cancelled=true}
  },[daySession,conversationOffset,conversationView,sessionStart,sessionEnd])
  useEffect(()=>{
    if(!daySession){setInspector(null);return}
    let cancelled=false
    setInspector(null)
    setInspectorError('')
    get<SessionInspector>(`/v1/mvp/activity/sessions/${daySession.agent}/${encodeURIComponent(daySession.native_id)}/inspector?device_id=${encodeURIComponent(daySession.device_id)}`).then(x=>{if(!cancelled)setInspector(x)}).catch(e=>{if(!cancelled)setInspectorError(String(e))})
    return()=>{cancelled=true}
  },[daySession,tick])

  function chooseRange(days:number){setPreset(String(days));setHeatView(days===1?'day':'custom');setStart(shift(today(),1-days));setEnd(today())}
  function chooseYear(){setPreset('year');setHeatView('year');setStart(yearStart());setEnd(today())}
  function allHistory(){
    const earliest=Object.values(data?.sources||{}).map(x=>x.earliest).filter((x):x is string=>!!x).sort()[0]
    setStart(earliest?dayOf(earliest):'2026-01-01');setEnd(today());setPreset('all');setHeatView('custom')
  }
  function selectHeatView(view:Exclude<HeatView,'custom'>,anchor?:string){
    const chosen=anchor||(start>today()?start:start<=today()&&end>=today()?today():end)
    const [from,to]=periodBounds(view,chosen)
    setStart(from);setEnd(to);setPreset('heat');setHeatView(view);setDaySession(null);setFocusDay(chosen)
    if(anchor&&view==='day'){setSessionStart(anchor);setSessionEnd(anchor)}
  }
  function chooseSessionRange(days:number){setSessionStart(shift(today(),1-days));setSessionEnd(today());setDaySession(null)}
  function openSession(row:DaySession){setDaySession(row);setSessionTab('conversation');setConversationOffset(0);setFolderStatus('')}
  async function openFolder(agent:Agent,nativeId:string,sourceDevice:string,kind:'source'|'workspace'|'file',path?:string){
    setFolderStatus('正在打开所在文件夹…')
    try{
      await post(`/v1/mvp/activity/sessions/${agent}/${encodeURIComponent(nativeId)}/reveal?device_id=${encodeURIComponent(sourceDevice)}`,{kind,...(path?{path}:{})},csrf)
      setFolderStatus('已打开所在文件夹')
    }catch(e){setFolderStatus(`无法打开所在文件夹：${String(e)}`)}
  }
  function switchAgent(value:Agent|''){setAgent(value);setModel('')}
  function selectModel(value:ReportModel){setAgent(value.agent);setModel(value.model)}
  async function shutdown(){if(!window.confirm('退出后台会停止采集，关闭浏览器则不会。确定退出后台吗？'))return;try{await post('/v1/local/shutdown',undefined,csrf);document.title='○ 后台已退出 · Agent Workbench';const icon=document.querySelector<HTMLLinkElement>('link[rel="icon"]');if(icon)icon.href='/status-red.svg';setStopped(true)}catch(e){setError(String(e))}}
  async function retryUpdate(){try{setUpdate(await post<UpdateStatus>('/v1/local/update',undefined,csrf))}catch(e){setUpdate(x=>x?{...x,error:String(e)}:x)}}
  async function saveSyncRoot(){try{const x=await post<ArchiveStatus>('/v1/archive/sync-root',{path:syncRootInput},csrf);setArchiveStatus(x);setSyncMessage('同步目录已保存，后台将导入可用的数据包');setTick(v=>v+1)}catch(e){setSyncMessage(`同步目录保存失败：${String(e)}`)}}
  async function refreshNow(){
    setRefreshing(true)
    try{
      await post('/v1/archive/refresh',undefined,csrf)
      setArchiveStatus(await get<ArchiveStatus>('/v1/archive/status'))
      setTick(value=>value+1)
    }catch(e){setError(`更新本机记录失败：${String(e)}`)}
    finally{setRefreshing(false)}
  }

  if(stopped)return <main className="mvpLogin"><div className="mvpLoginCard"><h1>工作台已关闭</h1><p>重新打开 Agent Workbench 即可继续使用。</p></div></main>
  if(!csrf)return <main className="mvpLogin"><div className="mvpLoginCard"><div className="mvpMark">✳</div><h1>Agent Workbench</h1><p>{error||'正在读取本机数据…'}</p>{error&&<button onClick={()=>window.location.reload()}>重试连接</button>}</div></main>

  const summary=data?.summary
  const heatRows=heatMetric==='token'?data?.heatmap:activity?.heatmap
  const heatGrain=heatMetric==='token'?data?.heatmap_granularity:activity?.heatmap_granularity
  const heatValues=(heatRows||[]).map(x=>heatMetric==='token'?(x as Trend).total_tokens:(x as TimeCell).wall_ms)
  const thresholds=heatThresholds(heatValues)
  const focusableHeat=(heatRows||[]).map((row,index)=>({row,index})).filter(({row})=>row.period.slice(0,10)<=today()&&row.period.slice(0,10)>=start&&row.period.slice(0,10)<=end&&(heatMetric==='token'?(row as Trend).total_tokens:(row as TimeCell).wall_ms)>0).map(({index})=>index)
  const currentHeatFocus=focusableHeat.includes(heatFocus)?heatFocus:focusableHeat[0]
  const heatLayout=heatView==='custom'?(heatGrain==='month'?'months':(heatRows?.length||0)>31?'year':'month'):heatView
  const heatOffset=heatLayout==='year'||heatLayout==='month'?(new Date(`${heatRows?.[0]?.period?.slice(0,10)||start}T12:00:00Z`).getUTCDay()+6)%7:0
  const heatWeeks=Math.ceil((heatOffset+(heatRows?.length||0))/7)
  const activeHeatCells=heatRows?.filter(row=>heatMetric==='token'?(row as Trend).total_tokens>0:(row as TimeCell).wall_ms>0).length||0
  const currentHour=Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Hong_Kong',hour:'2-digit',hourCycle:'h23'}).format(new Date()))
  const secondaryCards=summary?[
    ['请求／调用',fmt(summary.requests),'Codex/Claude 请求与 Hermes 汇总调用'],
    ['有用量的会话',fmt(data!.session_count),'所选时段有 Token 记录，不代表正在运行'],
    ['缓存命中率',summary.cache_hit_rate==null?'—':`${(summary.cache_hit_rate*100).toFixed(1)}%`,'缓存读取 Token ÷ 全部输入 Token'],
  ]:[]
  const tokenParts=summary?[
    ['新输入',summary.fresh_input_tokens],['缓存读取',summary.cached_input_tokens],
    ['缓存写入',summary.cache_creation_tokens],['输出',summary.output_tokens],
  ] as const:[]
  return <div className="mvpShell"><header className="mvpHeader"><div className="mvpBrand"><span className="mvpMark">✳</span><span><b>Agent Workbench</b><small>多 Agent 用量 · v{update?.current_version||'0.8.0'}</small></span></div><div className="mvpHeaderActions"><button onClick={()=>void refreshNow()} disabled={refreshing}>{refreshing?'更新记录中…':'刷新'}</button><button className="mvpShutdown" onClick={shutdown}>退出后台</button></div></header><BackgroundStatus csrf={csrf} onRefresh={()=>setTick(value=>value+1)} onReconnect={()=>{void get<{csrf:string}>('/auth/me').then(value=>{setCsrf(value.csrf);setTick(t=>t+1)})}}/><nav className="mvpSectionNav" aria-label="页面区块">{[['overview','概览'],['sources','数据来源'],['heatmap','热力图'],['runtime','运行时间'],['sessions','会话'],['models','模型']] .map(([id,label])=><a key={id} href={`#${id}`} aria-current={activeSection===id?'location':undefined}>{label}</a>)}</nav><main className="mvpMain"><div className="mvpTitle" id="overview" data-dashboard-section><div><p>USAGE / AGENTS</p><h1>用量仪表盘</h1><span>对话、用量和文件索引已保存至 Workbench 底库。</span></div><span className="mvpScope">{deviceId?(archiveStatus?.devices.find(x=>x.id===deviceId)?.name||'所选电脑'):'全部电脑汇总'} · {archiveStatus?.sync_ready?'同步目录已连接':'同步目录未连接'}</span><div className="mvpAgentSwitch" role="group" aria-label="切换 Agent"><button aria-pressed={agent===''} onClick={()=>switchAgent('')}>全部</button>{(Object.keys(names) as Agent[]).map(name=><button key={name} aria-pressed={agent===name} onClick={()=>switchAgent(name)}><span className={`mvpAgentIcon ${name}`}>{name==='codex'?'C':name==='claude'?'✳':'H'}</span>{names[name]}</button>)}</div></div>
    <section className="mvpFilters" aria-label="电脑来源"><label>电脑 <select value={deviceId} onChange={e=>{setDeviceId(e.target.value);setDaySession(null)}}><option value="">全部电脑（去重汇总）</option>{archiveStatus?.devices.map(row=><option key={row.id} value={row.id}>{row.name} · {row.os}{row.local?'（本机）':''}</option>)}</select></label><label>NAS 同步文件夹 <input type="text" value={syncRootInput} onChange={e=>setSyncRootInput(e.target.value)} placeholder="选择已同步的 Sync_AI 路径"/></label><button onClick={()=>void saveSyncRoot()} disabled={!syncRootInput.trim()}>保存目录</button><span>{archiveStatus?.sync_ready?'已连接':'未连接'} · 文件本身不备份</span>{syncMessage&&<span role="status">{syncMessage}</span>}</section>
    {update?.available&&update.state==='downloading'&&<div className="mvpUpdate" role="status">检测到 v{update.latest_version}，正在自动下载并校验安装包：{update.progress}%</div>}
    {update?.available&&update.state==='installing'&&<div className="mvpUpdate" role="status">安装包已校验，正在自动关闭旧版、安装并重新打开工作台。请稍候…</div>}
    {update?.available&&update.state==='error'&&<div className="mvpError" role="alert">自动更新暂时失败：{update.error}。当前版本仍可使用。{update.latest_version&&<button onClick={retryUpdate}>重试更新</button>}</div>}
    <section className="mvpFilters" aria-label="用量筛选"><div className="mvpRange" role="group" aria-label="快捷日期范围">{[1,7,15].map(n=><button key={n} aria-pressed={preset===String(n)} onClick={()=>chooseRange(n)}>{n===1?'今天':`近 ${n} 天`}</button>)}<button aria-pressed={preset==='year'} onClick={chooseYear}>今年至今</button><button aria-pressed={preset==='all'} onClick={allHistory}>全部历史</button></div><label>开始日期 <input type="date" value={start} onChange={e=>{setStart(e.target.value);setPreset('custom');setHeatView('custom')}}/></label><label>结束日期 <input type="date" value={end} onChange={e=>{setEnd(e.target.value);setPreset('custom');setHeatView('custom')}}/></label><label>模型 <select value={model} onChange={e=>{setModel(e.target.value)}}><option value="">全部模型</option>{choices.map(x=><option key={x} value={x}>{x}</option>)}</select></label></section>
    {invalidRange&&<div className="mvpError" role="alert">开始日期不能晚于结束日期。</div>}{error&&<div className="mvpError" role="alert">{error}</div>}
    <section className="mvpCards" aria-label="核心指标">{summary&&<article className="mvpCard mvpCardHero"><small>处理 Token · {start} 至 {end}</small><strong>{fmt(summary.total_tokens)}</strong><UsageSparkline rows={data?.trend||[]} grain={data?.trend_granularity||'day'}/><span>已记录的新输入、缓存读写与输出；Hermes 会话汇总计入总量，不计入逐时段曲线</span></article>}{secondaryCards.map(([label,value,note])=><article className="mvpCard" key={label}><small>{label}</small><strong>{value}</strong><span>{note}</span></article>)}</section>
    {summary&&<section className="mvpComposition" aria-label="处理 Token 构成"><h2>处理 Token 构成</h2><div className="mvpCompositionBar" role="img" aria-label={tokenParts.map(([label,value])=>`${label} ${fmt(value)}`).join('，')}>{tokenParts.map(([label,value],index)=><span key={label} className={`part${index+1}`} style={{width:`${summary.total_tokens?value/summary.total_tokens*100:0}%`}} title={`${label} ${fmt(value)}`} />)}</div><div className="mvpCompositionLabels">{tokenParts.map(([label,value],index)=><span key={label}><i className={`part${index+1}`}/>{label} <b>{fmt(value)}</b></span>)}</div></section>}
    {data&&<details className="mvpMethod"><summary>统计口径 · {start} 至 {end}（香港时间）</summary><p>{data.note} 原生记录未提供的缓存字段无法补算，显示的总量可能偏低；费用尚未计价。{loading?' 正在同步底库…':''}</p></details>}
    {data&&<section className="mvpPanel" id="sources" data-dashboard-section><div className="mvpPanelHead"><h2>数据来源与覆盖</h2></div><div className="mvpSourceGrid">{(Object.keys(names) as Agent[]).map(name=>{const source=data.sources[name];return <button className="mvpSource" key={name} onClick={()=>{setAgent(name);setModel('')}} aria-pressed={agent===name}><b>{names[name]}</b><strong>{fmt(source.total_tokens)} Token</strong><span>{fmt(source.requests)} {name==='hermes'?'调用（会话汇总）':'请求'} · {sourceStatus(source)}</span><small>底库记录：{dayOf(source.earliest)} 至 {dayOf(source.latest)}</small>{name==='claude'&&!!((source.missing_cache_read||0)+(source.missing_cache_write||0))&&<small>原始记录缺缓存读字段 {fmt(source.missing_cache_read||0)} 条、缺缓存写字段 {fmt(source.missing_cache_write||0)} 条；对应总量可能偏低</small>}{name==='hermes'&&<small>只能按完整会话汇总计入；跨越筛选边界 {fmt(source.partial_rows||0)} 项未计入</small>}<small className="mvpSourceAction">点击只看 {names[name]} →</small></button>})}</div></section>}
    <section className="mvpPanel mvpHeatPanel" id="heatmap" data-dashboard-section>
      <div className="mvpPanelHead"><div><h2>{heatMetric==='token'?'Token 活动热力图':'运行时间热力图'}</h2><p>{heatMetric==='token'?'Codex / Claude 按这台电脑的请求时间统计；悬停或聚焦格子查看用量。':'每格颜色按并行去重后的经过时间；悬停可比较累计 Agent 时长。Claude / Hermes 为估算。'}</p></div><div className="mvpHeatControls"><div className="mvpHeatViews" role="group" aria-label="热力图指标"><button aria-pressed={heatMetric==='token'} onClick={()=>{setHeatMetric('token');setHeatHover(null)}}>Token 用量</button><button aria-pressed={heatMetric==='time'} onClick={()=>{setHeatMetric('time');setHeatHover(null)}}>运行时间</button></div><div className="mvpHeatViews" role="group" aria-label="热力图时间粒度">{(['year','month','week','day'] as const).map(view=><button key={view} aria-pressed={heatView===view} onClick={()=>selectHeatView(view)}>{({year:'年',month:'月',week:'周',day:'日'} as const)[view]}</button>)}</div></div></div>
      <div className="mvpHeatPeriod"><button onClick={()=>heatView!=='custom'&&selectHeatView(heatView,movePeriod(heatView,start,-1))} disabled={heatView==='custom'} aria-label="上一时段">‹</button><strong>{periodLabel(heatView,start,end)}</strong><button onClick={()=>heatView!=='custom'&&selectHeatView(heatView,movePeriod(heatView,start,1))} disabled={heatView==='custom'} aria-label="下一时段">›</button><button onClick={()=>selectHeatView(heatView==='custom'?'year':heatView,today())}>回到当前</button></div>
      {(heatMetric==='token'?loading:activityLoading)&&<p className="mvpHeatLoading" role="status">正在读取所选时段的本机{heatMetric==='token'?'用量':'会话时间'}…</p>}
      {heatRows&&<div className="mvpHeatScroll">{heatView==='year'&&<div className="mvpHeatMonthHead" style={{gridTemplateColumns:`repeat(${heatWeeks},minmax(0,1fr))`}} aria-hidden="true">{Array.from({length:12},(_,month)=>{
        const year=Number(start.slice(0,4))
        const first=Date.UTC(year,0,1)
        const week=Math.floor((heatOffset+(Date.UTC(year,month,1)-first)/86400000)/7)
        return <span key={month} style={{gridColumnStart:week+1}}>{month+1}月</span>
      })}</div>}<div className={`mvpHeatGrid mvpHeat${heatLayout}`} role="group" aria-label={`${periodLabel(heatView,start,end)}，${fmt(activeHeatCells)} 格有${heatMetric==='token'?'用量':'运行记录'}；方向键浏览有记录的格子`} tabIndex={focusableHeat.length?undefined:0}>
        {Array.from({length:heatOffset},(_,index)=><span className="mvpHeatSpacer" key={`spacer-${index}`} aria-hidden="true" />)}
        {heatRows.map((row,index)=>{
          const day=row.period.slice(0,10)
          const isFuture=day>today()||(heatGrain==='hour'&&day===today()&&Number(row.period.slice(-2))>currentHour)
          const isOutside=heatGrain!=='month'&&(day<start||day>end)
          const value=heatMetric==='token'?(row as Trend).total_tokens:(row as TimeCell).wall_ms
          const level=heatLevel(value,thresholds)
          const label=`${row.period}${heatGrain==='hour'?':00':''}，${isFuture?'尚未发生':isOutside?'不在所选范围':heatMetric==='token'?`${fmt((row as Trend).total_tokens)} Token，${fmt((row as Trend).requests)} 请求`:`自然经过 ${duration((row as TimeCell).wall_ms)}，Agent 累计 ${duration((row as TimeCell).agent_ms)}`}`
          const showHover=(element:HTMLButtonElement,x:number)=>{const bounds=element.getBoundingClientRect();setHeatHover({row,x:Math.max(155,Math.min(window.innerWidth-155,x)),y:bounds.top>=120?bounds.top-8:bounds.bottom+8,below:bounds.top<120,note:isFuture?'尚未发生':isOutside?'不在所选范围':''})}
          const content=<span>{heatGrain==='month'?day.slice(5,7)+'月':heatLayout==='month'?Number(day.slice(-2)):heatLayout==='day'?row.period.slice(-2):heatLayout==='week'?new Date(`${day}T12:00:00Z`).toLocaleDateString('zh-CN',{weekday:'short',timeZone:'UTC'}):''}</span>
          if(isFuture||isOutside||value<=0)return <div key={row.period} className={`mvpHeatCell level${level}${isFuture?' future':''}${isOutside?' outside':''}`} title={label} aria-hidden="true">{content}</div>
          return <button type="button" key={row.period} data-heat-index={index} className={`mvpHeatCell level${level}`} aria-label={label} tabIndex={currentHeatFocus===index?0:-1} onPointerEnter={e=>showHover(e.currentTarget,e.clientX)} onPointerMove={e=>showHover(e.currentTarget,e.clientX)} onPointerLeave={()=>setHeatHover(null)} onFocus={e=>{setHeatFocus(index);showHover(e.currentTarget,e.currentTarget.getBoundingClientRect().left+e.currentTarget.offsetWidth/2)}} onBlur={()=>setHeatHover(null)} onKeyDown={e=>{const delta=e.key==='ArrowLeft'||e.key==='ArrowUp'?-1:e.key==='ArrowRight'||e.key==='ArrowDown'?1:0;if(!delta)return;e.preventDefault();const next=focusableHeat[Math.max(0,Math.min(focusableHeat.length-1,focusableHeat.indexOf(index)+delta))];setHeatFocus(next);e.currentTarget.parentElement?.querySelector<HTMLButtonElement>(`[data-heat-index="${next}"]`)?.focus()}} onClick={()=>{setFocusDay(day); if(heatGrain==='month')selectHeatView('month',day);else if(heatGrain==='day')selectHeatView('day',day)}}>{content}</button>
        })}
      </div></div>}
      {heatRows&&<div className="mvpHeatMeta"><span>{heatGrain==='hour'?'每格一小时':heatGrain==='month'?'每格一月':'每格一天'} · {fmt(activeHeatCells)} / {fmt(heatRows.length)} 格有{heatMetric==='token'?'用量':'运行记录'} · 空格仅代表没有已归档记录，不能证明未使用；斜纹表示未来</span><div className="mvpHeatLegend">少 {[0,1,2,3,4,5].map(level=><i className={`level${level}`} key={level}/>)} 多</div></div>}
      {heatHover&&<div className={`mvpHeatTooltip${heatHover.below?' below':''}`} style={{left:heatHover.x,top:heatHover.y}} role="status"><b>{heatHover.row.period}{heatGrain==='hour'?':00':''}</b>{heatHover.note?<strong>{heatHover.note}</strong>:heatMetric==='token'?<><strong>{fmt((heatHover.row as Trend).total_tokens)} Token</strong><span>{fmt((heatHover.row as Trend).requests)} 次请求 · 输入 {fmt((heatHover.row as Trend).input_tokens)} · 输出 {fmt((heatHover.row as Trend).output_tokens)}</span></>:<><strong>自然经过 {duration((heatHover.row as TimeCell).wall_ms)}</strong><span>Agent 累计 {duration((heatHover.row as TimeCell).agent_ms)} · 已证实 {duration((heatHover.row as TimeCell).verified_ms)} · {fmt((heatHover.row as TimeCell).runs)} 段</span></>}</div>}
      {heatMetric==='token'&&!!data?.unattributed_tokens&&<p className="mvpHeatUndated">Hermes 有 {fmt(data.unattributed_tokens)} Token 的会话／模型汇总已计入上方指标；原始记录没有逐日或逐小时用量，因此不能分配到格子里。</p>}
    </section>
    <section className="mvpPanel mvpDayPanel" id="runtime" data-dashboard-section><div className="mvpPanelHead"><div><h2>运行时间汇总</h2><p>按上方所选的 {start} 至 {end} 统计。累计时长叠加并行任务，自然经过时间合并重叠区间。</p></div></div>
      {activityError&&<p className="mvpError">{activityError}</p>}
      {activityLoading&&!activity&&<p className="mvpEmpty">正在从本地库读取会话时间…</p>}
      {activity&&<><div className="mvpTimeSummary"><div><small>全部 Agent 累计</small><strong>{duration(activity.summary.agent_ms)}</strong><span>{fmt(activity.summary.runs)} 段任务／估算片段</span></div><div><small>自然经过（并行去重）</small><strong>{duration(activity.summary.wall_ms)}</strong><span>所选日期范围的时间并集</span></div><div><small>其中已证实</small><strong>{duration(activity.summary.verified_ms)}</strong><span>Codex 已完成顶层任务</span></div>{(Object.keys(names) as Agent[]).map(name=><div key={name}><small>{names[name]} 累计 / 去重</small><strong>{duration(activity.by_agent[name].agent_ms)}</strong><span>去重 {duration(activity.by_agent[name].wall_ms)}{name!=='codex'||activity.by_agent.codex.agent_ms>activity.by_agent.codex.verified_ms?' · 含估算':''}</span></div>)}</div><p className="mvpProvenance">{activity.note}</p></>}
    </section>
    <section className="mvpPanel mvpSessionBrowser" id="sessions" data-dashboard-section><div className="mvpPanelHead"><div><h2>会话记录</h2><p>按日期、Agent 和关键词找到会话；右侧查看对话及文件线索。</p></div><span>{sessionBrowser?`共 ${fmt(sessionBrowser.session_count)} 个会话`:''}</span></div>
      <div className="mvpSessionToolbar">
        <div className="mvpSessionPrimary">
          <span className="mvpToolbarLabel">时间与来源</span>
          <div className="mvpSessionControls"><div className="mvpRange" role="group" aria-label="会话快捷日期范围">{[1,7,30].map(n=><button key={n} aria-pressed={sessionStart===shift(today(),1-n)&&sessionEnd===today()} onClick={()=>chooseSessionRange(n)}>{n===1?'今天':`近 ${n} 天`}</button>)}</div><label>开始日期 <input type="date" value={sessionStart} onChange={e=>{setSessionStart(e.target.value);setDaySession(null)}}/></label><label>结束日期 <input type="date" value={sessionEnd} onChange={e=>{setSessionEnd(e.target.value);setDaySession(null)}}/></label></div>
          <div className="mvpSessionAgentSwitch" role="group" aria-label="会话 Agent"><button aria-pressed={sessionAgent===''} onClick={()=>{setSessionAgent('');setDaySession(null)}}>全部 {sessionBrowser?.session_count??''}</button>{(Object.keys(names) as Agent[]).map(name=><button key={name} aria-pressed={sessionAgent===name} onClick={()=>{setSessionAgent(name);setDaySession(null)}}>{names[name]} {sessionBrowser?.counts[name]??''}</button>)}</div>
        </div>
        <div className="mvpSessionExplore">
          <label className="mvpSessionSearch">查找会话 <input type="search" value={sessionSearch} maxLength={120} onChange={e=>{setSessionSearch(e.target.value);setDaySession(null)}} placeholder="搜索标题、对话内容或文件路径…"/></label>
          <div className="mvpQuickSort"><span>快速查看</span>{([['recent','最近活动'],['text_desc','文字最多'],['duration_desc','用时最长'],['storage_desc','记录最大'],['files_desc','文件最多']] as const).map(([key,title])=><button key={key} type="button" aria-pressed={sessionSort===key} onClick={()=>setSessionSort(key)}>{title}</button>)}</div>
          <details className="mvpAdvancedDetails"><summary>高级搜索与更多排序 <span>{sessionSearchIn!=='all'||sessionMinText||sessionMinDuration||sessionHasFiles?'· 已启用条件':''}</span></summary>
            <div className="mvpAdvancedFilters" aria-label="会话高级筛选">
              <label>搜索范围 <select value={sessionSearchIn} onChange={e=>setSessionSearchIn(e.target.value)}><option value="all">标题、内容、文件</option><option value="title">仅标题</option><option value="content">仅对话内容</option><option value="file">仅文件路径</option></select></label>
              <label>排序 <select value={sessionSort} onChange={e=>setSessionSort(e.target.value)}><option value="recent">最近活动</option><option value="oldest">最早活动</option><option value="text_desc">文字最多</option><option value="text_asc">文字最少</option><option value="duration_desc">用时最长</option><option value="duration_asc">用时最短</option><option value="storage_desc">记录最大</option><option value="storage_asc">记录最小</option><option value="messages_desc">消息最多</option><option value="messages_asc">消息最少</option><option value="files_desc">涉及文件最多</option></select></label>
              <label>至少文字 <input type="number" min="0" step="1000" value={sessionMinText} onChange={e=>setSessionMinText(Math.max(0,Number(e.target.value)||0))}/><span>字</span></label>
              <label>至少用时 <input type="number" min="0" step="10" value={sessionMinDuration} onChange={e=>setSessionMinDuration(Math.max(0,Number(e.target.value)||0))}/><span>分钟</span></label>
              <label className="mvpFilterCheck"><input type="checkbox" checked={sessionHasFiles} onChange={e=>setSessionHasFiles(e.target.checked)}/>有确认文件</label>
              <button type="button" onClick={()=>{setSessionSearch('');setSessionSearchIn('all');setSessionSort('recent');setSessionMinText(0);setSessionMinDuration(0);setSessionHasFiles(false)}}>清空筛选</button>
            </div>
          </details>
        </div>
      </div>
      {invalidSessionRange&&<p className="mvpError" role="alert">会话开始日期不能晚于结束日期。</p>}{sessionError&&<p className="mvpError" role="alert">{sessionError}</p>}
      <p className="mvpSessionResult">{sessionStart===sessionEnd?sessionStart:`${sessionStart} 至 ${sessionEnd}`} · {sessionAgent?names[sessionAgent]:'全部 Agent'} · {sessionBrowser?`${fmt(sessionBrowser.session_count)} 个匹配会话${sessionBrowser.session_count>sessionBrowser.sessions.length?`，显示前 ${fmt(sessionBrowser.sessions.length)} 个`:''}`:sessionError?'读取失败':sessionLoading?'正在读取…':'暂无结果'}{sessionQuery?' · 关键词匹配':''}。文字量按所选日期内的已归档消息计算；Hermes 记录大小是字段估算。</p>
      {folderStatus&&!daySession&&<p className="mvpCopyStatus" role="status">{folderStatus}</p>}
      <div className={`mvpSessionWorkspace${daySession?' hasSelection':''}`}>
        <div className="mvpSessionList" aria-label="会话列表">
          {sessionLoading&&<p className="mvpEmpty">正在读取所选时段的会话…</p>}
          {sessionBrowser?.sessions.map(row=><div className="mvpSessionItem" key={`${row.device_id}:${row.agent}:${row.native_id}`} data-selected={daySession?.device_id===row.device_id&&daySession?.agent===row.agent&&daySession.native_id===row.native_id}>
            <div className="mvpSessionItemTop"><button className="mvpSessionName" onClick={()=>openSession(row)} aria-pressed={daySession?.agent===row.agent&&daySession.native_id===row.native_id}>{names[row.agent]} · {row.title}</button><div className="mvpSessionItemActions"><span title={row.storage_kind==='payload'?'消息与工具字段大小；非数据库占用':'原始记录磁盘大小'}>{row.storage_kind==='payload'?'内容约':'记录'} {row.storage_bytes===null?'未知':bytes(row.storage_bytes)}</span><button type="button" disabled={!row.can_open_folder} onClick={()=>void openFolder(row.agent,row.native_id,row.device_id,'source')} title="打开原始会话记录所在文件夹">打开目录</button></div></div>
            <button className="mvpSessionBody" onClick={()=>openSession(row)}><small>{when(row.last_at)} · {fmt(row.messages)} 条消息 · Agent {duration(row.agent_ms)} · {fmt(row.text_chars)} 字 · {fmt(row.file_count)} 个文件</small><span>{row.match?`${row.match.type}：${row.match.excerpt}`:row.preview||'点击查看会话详情'}</span></button>
          </div>)}
          {sessionBrowser&&!sessionBrowser.sessions.length&&<p className="mvpEmpty">没有匹配的会话。可扩大日期范围或换个关键词。</p>}
        </div>
        <div className="mvpSessionDetail" aria-label="会话详情">
          {!daySession?<div className="mvpSessionPlaceholder"><strong>选择左侧会话</strong><span>这里会显示对话、原始记录位置和有证据的文件操作。</span></div>:<>
            <div className="mvpSessionDetailHead"><button className="mvpMobileBack" onClick={()=>setDaySession(null)}>← 返回会话列表</button><div className="mvpSessionDetailTitle"><small>{names[daySession.agent]} · {when(daySession.last_at)}</small><h3>{daySession.title}</h3><p>{sessionStart===sessionEnd?sessionStart:`${sessionStart} 至 ${sessionEnd}`} · {fmt(daySession.messages)} 条消息</p></div><div className="mvpSessionQuickStats"><div><small>用户提问</small><strong>{fmt(daySession.user_turns)} 轮</strong></div><div><small>Agent 用时</small><strong>{duration(daySession.agent_ms)}</strong></div><div><small>已确认文件</small><strong>{inspector?fmt(inspector.unique_file_count):'…'}</strong></div></div></div>
            <div className="mvpDetailTabs" role="group" aria-label="会话详情视图"><button aria-pressed={sessionTab==='conversation'} onClick={()=>setSessionTab('conversation')}>对话</button><button aria-pressed={sessionTab==='files'} onClick={()=>setSessionTab('files')}>文件与位置 {inspector?`(${inspector.unique_file_count} 已确认)` :''}</button></div>
            {folderStatus&&<p className="mvpCopyStatus" role="status">{folderStatus}</p>}
            {sessionTab==='conversation'?<><div className="mvpConversationViews" role="group" aria-label="对话显示方式"><button aria-pressed={conversationView==='key'} onClick={()=>{setConversationView('key');setConversationOffset(0)}}>关键节点</button><button aria-pressed={conversationView==='full'} onClick={()=>{setConversationView('full');setConversationOffset(0)}}>完整详情</button><span>关键节点保留用户输入和每轮最后的 Agent 回复</span></div><div className="mvpMessages">{conversation?.items.map(row=><article key={row.id} className={row.role}><small>{row.role==='user'?'我':'Agent'} · {when(row.occurred_at)}</small><p>{row.body}</p></article>)}</div>{conversation===null&&<p className="mvpEmpty">正在读取对话…</p>}{conversation?.count===0&&<p className="mvpEmpty">所选时段没有可读取的文字消息。</p>}{conversation&&conversation.count>100&&<div className="mvpConversationPages"><button disabled={conversationOffset===0} onClick={()=>setConversationOffset(x=>Math.max(0,x-100))}>上一页</button><span>{conversationOffset+1}–{Math.min(conversationOffset+100,conversation.count)} / {conversation.count}</span><button disabled={conversationOffset+100>=conversation.count} onClick={()=>setConversationOffset(x=>x+100)}>下一页</button></div>}</>:<div className="mvpFilePanel">
              {inspectorError&&<p className="mvpError">文件信息读取失败：{inspectorError}</p>}{!inspector&&!inspectorError&&<p className="mvpEmpty">正在读取来源与文件记录…</p>}
              {inspector&&<><div className="mvpFileSummary"><div><small>原始记录磁盘大小</small><strong>{bytes(inspector.record_bytes)}</strong><span>{daySession.agent==='hermes'?'Hermes 使用共享数据库，不能按会话分摊文件大小':`${inspector.sources.length} 个原始记录文件；不是运行时 RAM`}</span></div><div><small>已确认涉及的文件</small><strong>{fmt(inspector.unique_file_count)}</strong><span>{fmt(inspector.confirmed_event_count)} 次确认操作 · {fmt(inspector.possible_file_count)} 条待核实路径</span></div></div>
                {daySession.agent==='hermes'&&inspector.payload_bytes!==null&&<p className="mvpFileNote">本会话消息与工具字段约 {bytes(inspector.payload_bytes)}；这不是数据库占用量，也不是 RAM。</p>}
                <h4>来源位置</h4><div className="mvpPathRows"><div><span>工作目录</span><code>{inspector.cwd||'原始记录未提供'}</code>{inspector.cwd&&<button disabled={daySession.device_id!==archiveStatus?.local_device_id} onClick={()=>void openFolder(daySession.agent,daySession.native_id,daySession.device_id,'workspace')}>打开目录</button>}</div>{inspector.sources.map(row=><div key={row.path}><span>{row.status==='shared_database'?'共享数据库':'会话原始记录'}</span><code>{row.path}</code><small>{row.bytes===null?'大小未能单独计算':bytes(row.bytes)}</small><button disabled={daySession.device_id!==archiveStatus?.local_device_id} onClick={()=>void openFolder(daySession.agent,daySession.native_id,daySession.device_id,'source',row.path)}>打开目录</button></div>)}</div>
                <h4>文件操作历史</h4><p className="mvpFileNote">{inspector.coverage} 当前文件状态仅是打开详情时的本机检查。</p>
                {inspector.file_events.length?<div className="mvpFileEvents">{inspector.file_events.map((event,index)=><article key={`${event.source_file}:${event.native_path}:${index}`}><div><b>{event.relation==='referenced'?'待核实路径':event.relation==='created'?'创建':event.relation==='deleted'?'删除':event.relation==='created/modified'?'写入':'修改'}</b><small>{when(event.occurred_at)} · {event.evidence}</small></div><code>{event.native_path}</code><small>当前：{event.current_state==='present'?`存在 · ${bytes(event.current_bytes)}`:event.current_state==='missing'?'未找到':'无法检查'}</small><button disabled={daySession.device_id!==archiveStatus?.local_device_id} onClick={()=>void openFolder(daySession.agent,daySession.native_id,daySession.device_id,'file',event.native_path)}>打开所在文件夹</button></article>)}</div>:<p className="mvpEmpty">尚未取得文件操作线索；不能据此判定会话没有产出文件。</p>}
              </>}
            </div>}
          </>}
        </div>
      </div>
    </section>
    <div id="models" data-dashboard-section><ModelReport data={report} onSelect={selectModel}/></div>
    <p className="mvpFoot">本机记录包含 Token 用量与会话时间；跨设备数据仍需在各设备采集后同步。源日志不存在的时段不会被补造。</p></main></div>
}

createRoot(document.getElementById('root')!).render(<App/>)
