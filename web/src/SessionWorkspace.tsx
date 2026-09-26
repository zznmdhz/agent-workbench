import {useEffect, useState, type FormEvent, type KeyboardEvent} from 'react'
import {FilterBar, type FilterValues} from './FilterBar'
import {HandoffPanel} from './HandoffPanel'
import {sessionLabel, workingDirectoryName, type SessionIdentity} from './sessionIdentity'
import './sessions.css'

export type Session = SessionIdentity & {
  device_id:string|null
  device_name:string
  message_count:number
  run_count:number
  profile:string
  source_id?:string
  content_coverage?:Coverage
  latest_matching_activity?:string|null
  matching_activity_basis?:string|null
}
export type Focus = {kind:'latest'|'date'|'run'|'message',id?:string}
export type SessionTab = 'conversation'|'runs'|'files'|'evidence'
type Coverage={readable:number,total:number,missing:number}
type Message={id:string,role:string,body:string|null,content_state:string,omission_reason?:string|null,occurred_at:string|null,source_char_count:number|null,run_id?:string|null,turn_mapping_basis?:string|null}
type Run={id:string,status:string,start_at:string|null,end_at:string|null,duration_ms:number|null,model:string|null,message_count?:number,readable_count?:number,file_count?:number}
type FileFact={event_id:string,relative_path?:string|null,native_path?:string|null,logical_root?:string|null,relation?:string,operation_status?:string,run_id?:string|null,size_bytes?:number|null,mtime?:string|null,checked_at?:string|null,operation_at?:string|null,evidence_json?:string,detail_status?:string,evidence_status?:string}
type FileDetail={file:FileFact,maps:{environment_id:string}[],current_access?:{state:string,checked_at?:string|null,result?:{status?:string,size_bytes?:number|null}}|null}
type LocalAccess={state:string,checked_at:string|null,result:{status:string,size_bytes?:number|null,mtime_ns?:number,checked_at?:string,reason?:string}}
type Detail={session:Session,items:Message[],runs:Run[],files:FileFact[],next_cursor:string|null,prev_cursor?:string|null,page_direction?:string,content_coverage?:Coverage,unassigned_message_count?:number,file_evidence_state?:string,focus?:unknown}
type SearchResult={id:string,session_id:string,display_title?:string|null,title:string|null,agent?:string,device_name?:string,last_activity?:string|null,match_at?:string|null,match_type?:string,excerpt:string,matched_message_id?:string|null}
type Source={id:string,execution_surface:string,capability_json:string}
type Props={
  sessions:Session[]
  selectedId:string|null
  focus:Focus
  tab:SessionTab
  filters:FilterValues
  devices:{id:string,name:string}[]
  models:string[]
  sources:Source[]
  csrf:string
  sessionCursor:string|null
  refreshTick:number
  onFilter:(patch:Partial<FilterValues>)=>void
  onSelect:(id:string,focus?:Focus,tab?:SessionTab)=>void
  onTab:(tab:SessionTab)=>void
  onMoreSessions:()=>void
  onSettings:()=>void
  onRefresh:()=>void
}

const formatTimestamp=(value:string|null|undefined,tz:string)=>value?new Date(value).toLocaleString('zh-CN',{timeZone:tz}):'时间未知'
const formatCompactTime=(value:string|null|undefined,tz:string)=>value?new Date(value).toLocaleTimeString('zh-CN',{timeZone:tz,hour:'2-digit',minute:'2-digit'}):'未知'
const size=(bytes:number|null|undefined)=>bytes===null||bytes===undefined?'大小未采集':bytes<1024?`${bytes} B`:bytes<1048576?`${(bytes/1024).toFixed(1)} KB`:`${(bytes/1048576).toFixed(1)} MB`
const statusName=(value:string)=>({completed:'已完成',running:'进行中',failed:'失败',cancelled:'已取消'} as Record<string,string>)[value]||value
const relationName=(value:string|undefined)=>({created:'创建',modified:'修改',read:'读取',referenced:'引用',possible:'可能相关'} as Record<string,string>)[value||'']||value||'关系未判定'
const operationName=(value:string|undefined)=>({success:'来源记录操作成功',succeeded:'来源记录操作成功',failed:'来源记录操作失败',unknown:'操作结果未知'} as Record<string,string>)[value||'']||'操作结果未知'
const accessName=(value:string|undefined)=>({not_checked:'尚未检查当前文件',pending:'正在等待设备检查',complete:'设备检查已完成',exists:'当前可访问',missing:'当前不存在',inaccessible:'当前无法访问',unmapped:'设备未配置可检查位置',failed:'检查失败',expired:'检查已过期'} as Record<string,string>)[value||'']||'当前状态未知'
const evidenceName=(value:string|undefined)=>({recorded_operation:'来源记录的操作证据',unverified:'关系尚未核验',inferred:'根据时间推断，待核验'} as Record<string,string>)[value||'']||'关系依据待核验'

async function getJson<T>(url:string,init?:RequestInit):Promise<T>{
  const response=await fetch(url,{credentials:'same-origin',...init})
  if(!response.ok)throw new Error(`${response.status} ${await response.text()}`)
  return response.json()
}

function policy(source:Source|undefined):string{
  if(!source)return 'unknown'
  try{return JSON.parse(source.capability_json||'{}').content_policy||'stats_only'}catch{return 'unknown'}
}

function SessionRow({session,active,tz,onClick}:{session:Session,active:boolean,tz:string,onClick:()=>void}){
  const dir=workingDirectoryName(session)
  const activity=session.matching_activity_basis==='overlapping_run_no_event'?'跨日活动，日期内无精确时间':formatTimestamp(session.latest_matching_activity||session.last_activity,tz)
  return <button className={'sessionRow '+(active?'selected':'')} onClick={onClick} aria-current={active?'true':undefined} title={session.cwd?`来源记录的工作目录：${session.cwd}`:undefined}>
    <span className="agentGlyph" aria-hidden="true">{session.agent==='codex'?'C':'H'}</span>
    <span className="sessionText"><b>{sessionLabel(session)}</b><small>{session.device_name} · {session.agent} · {activity}</small><small>{dir?`来源工作目录：${dir}`:'工作目录未记录'} · 已识别 {session.run_count??0} 轮</small></span>
  </button>
}

export function SessionWorkspace(p:Props){
  const timestamp=(value:string|null|undefined)=>formatTimestamp(value,p.filters.tz)
  const compactTime=(value:string|null|undefined)=>formatCompactTime(value,p.filters.tz)
  const [detail,setDetail]=useState<Detail|null>(null)
  const [detailLoading,setDetailLoading]=useState(false)
  const [detailError,setDetailError]=useState('')
  const [localRefresh,setLocalRefresh]=useState(0)
  const [query,setQuery]=useState('')
  const [searchedQuery,setSearchedQuery]=useState('')
  const [search,setSearch]=useState<SearchResult[]|null>(null)
  const [searchError,setSearchError]=useState('')
  const [rename,setRename]=useState(false)
  const [renameText,setRenameText]=useState('')
  const [selectedFile,setSelectedFile]=useState<FileFact|null>(null)
  const [fileDetail,setFileDetail]=useState<FileDetail|null>(null)
  const [fileError,setFileError]=useState('')
  const [fileCheckJob,setFileCheckJob]=useState<string|null>(null)
  const [localAccess,setLocalAccess]=useState<LocalAccess|null>(null)
  const [localCheckBusy,setLocalCheckBusy]=useState(false)

  useEffect(()=>{
    if(!p.selectedId){setDetail(null);return}
    const params=new URLSearchParams({focus:p.focus.kind,tz:p.filters.tz,limit:'100'})
    if(p.focus.kind==='date')params.set('activity_day',p.filters.date)
    if(p.focus.kind==='run'&&p.focus.id)params.set('run_id',p.focus.id)
    if(p.focus.kind==='message'&&p.focus.id)params.set('message_id',p.focus.id)
    let cancelled=false
    setDetail(null)
    setDetailLoading(true)
    setDetailError('')
    getJson<Detail>(`/v1/sessions/${encodeURIComponent(p.selectedId)}/events?${params}`)
      .then(result=>{if(!cancelled)setDetail(result)})
      .catch(error=>{if(!cancelled)setDetailError(`会话加载失败：${String(error)}`)})
      .finally(()=>{if(!cancelled)setDetailLoading(false)})
    return()=>{cancelled=true}
  },[p.selectedId,p.focus.kind,p.focus.id,p.filters.date,p.filters.tz,p.refreshTick,localRefresh])

  useEffect(()=>{
    if(!selectedFile){setFileDetail(null);return}
    let cancelled=false
    setFileError('')
    getJson<FileDetail>(`/v1/files/${encodeURIComponent(selectedFile.event_id)}`)
      .then(value=>{if(!cancelled)setFileDetail(value)})
      .catch(error=>{if(!cancelled)setFileError(String(error))})
    return()=>{cancelled=true}
  },[selectedFile])
  useEffect(()=>{setSelectedFile(null);setFileDetail(null);setFileCheckJob(null);setLocalAccess(null)},[p.selectedId])
  useEffect(()=>{setLocalAccess(null)},[selectedFile])
  useEffect(()=>{
    if(!fileCheckJob||!selectedFile)return
    let cancelled=false
    const poll=()=>getJson<{status:string}>(`/v1/file-checks/${encodeURIComponent(fileCheckJob)}`).then(job=>{
      if(cancelled)return
      if(job.status==='complete'||job.status==='failed'||job.status==='expired'){
        setFileCheckJob(null)
        void getJson<FileDetail>(`/v1/files/${encodeURIComponent(selectedFile.event_id)}`).then(value=>{if(!cancelled)setFileDetail(value)})
      }
    }).catch(error=>{if(!cancelled){setFileError(`文件检查失败：${String(error)}`);setFileCheckJob(null)}})
    const timer=window.setInterval(()=>void poll(),2000)
    return()=>{cancelled=true;window.clearInterval(timer)}
  },[fileCheckJob,selectedFile])

  async function checkFile(){
    if(!fileDetail?.file.logical_root||!fileDetail.file.relative_path||!session?.device_id)return
    setFileError('')
    try{
      const job=await getJson<{id:string}>('/v1/file-checks',{method:'POST',headers:{'Content-Type':'application/json','x-awb-csrf':p.csrf},body:JSON.stringify({target_device_id:session.device_id,root_id:fileDetail.file.logical_root,relative_path:fileDetail.file.relative_path,operation:'stat'})})
      setFileCheckJob(job.id)
    }catch(error){setFileError(`无法发起文件检查：${String(error)}`)}
  }
  async function checkLocalFile(){
    if(!selectedFile)return
    setFileError('');setLocalCheckBusy(true)
    try{
      const result=await getJson<{current_access:LocalAccess}>(`/v1/local/files/${encodeURIComponent(selectedFile.event_id)}/check`,{method:'POST',headers:{'Content-Type':'application/json','x-awb-csrf':p.csrf}})
      setLocalAccess(result.current_access)
    }catch(error){setFileError(`当前文件检查不可用：${String(error)}`)}
    finally{setLocalCheckBusy(false)}
  }

  async function moreMessages(){
    if(!p.selectedId||!detail)return
    const cursor=detail.prev_cursor||detail.next_cursor
    if(!cursor)return
    try{
      const params=new URLSearchParams({cursor,limit:'100',focus:p.focus.kind,tz:p.filters.tz})
      if(p.focus.kind==='date')params.set('activity_day',p.filters.date)
      if(p.focus.kind==='run'&&p.focus.id)params.set('run_id',p.focus.id)
      if(p.focus.kind==='message'&&p.focus.id)params.set('message_id',p.focus.id)
      const value=await getJson<Detail>(`/v1/sessions/${encodeURIComponent(p.selectedId)}/events?${params}`)
      const older=Boolean(detail.prev_cursor)||detail.page_direction==='older'
      setDetail(current=>current?{
        ...current,
        items:(()=>{const merged=older?[...value.items,...current.items]:[...current.items,...value.items];return merged.filter((item,index)=>merged.findIndex(candidate=>candidate.id===item.id)===index)})(),
        prev_cursor:value.prev_cursor,
        next_cursor:value.next_cursor,
        page_direction:value.page_direction,
      }:value)
    }catch(error){setDetailError(String(error))}
  }

  async function doSearch(event:FormEvent){
    event.preventDefault()
    if(!query.trim())return
    setSearchError('')
    try{const results=await getJson<{items:SearchResult[]}>(`/v1/search?q=${encodeURIComponent(query.trim())}`);setSearch(results.items);setSearchedQuery(query.trim())}
    catch(error){setSearchError(`搜索失败：${String(error)}`)}
  }

  async function saveTitle(event:FormEvent){
    event.preventDefault()
    if(!p.selectedId)return
    try{
      await getJson(`/v1/sessions/${encodeURIComponent(p.selectedId)}/title`,{method:'PATCH',headers:{'Content-Type':'application/json','x-awb-csrf':p.csrf},body:JSON.stringify({title:renameText.trim()||null})})
      setRename(false);setLocalRefresh(value=>value+1);p.onRefresh()
    }catch(error){setDetailError(`标题保存失败：${String(error)}`)}
  }

  async function deleteSavedContent(){
    if(!p.selectedId)return
    if(!window.confirm('删除此会话在工作台内保存的正文、标题和文件证据？统计会保留。此操作不能撤销；已有备份仍可能暂时保留旧内容。'))return
    try{
      await getJson(`/v1/sessions/${encodeURIComponent(p.selectedId)}/content`,{method:'DELETE',headers:{'Content-Type':'application/json','x-awb-csrf':p.csrf},body:JSON.stringify({confirmation:p.selectedId,keep_statistics:true})})
      setLocalRefresh(value=>value+1);p.onRefresh()
    }catch(error){setDetailError(`删除失败：${String(error)}`)}
  }

  const selectedFromList=p.sessions.find(s=>s.id===p.selectedId)
  const session=detail?.session||selectedFromList
  const coverage=detail?.content_coverage||session?.content_coverage
  const runCount=session?.run_count??selectedFromList?.run_count??detail?.runs.length??0
  const messageCount=session?.message_count??selectedFromList?.message_count??coverage?.total??0
  const readablePage=detail?.items.filter(m=>Boolean(m.body)).length||0
  const totalPage=detail?.items.length||0
  const source=p.sources.find(s=>s.id===session?.source_id)
  const contentPolicy=policy(source)
  const currentAccess=localAccess||fileDetail?.current_access
  const activeRun=p.focus.kind==='run'?detail?.runs.find(r=>r.id===p.focus.id):null
  const activeRunMessages=activeRun?detail?.items.filter(m=>m.run_id===activeRun.id)||[]:[]
  const tabOrder:SessionTab[]=['conversation','runs','files','evidence']
  const tabText:Record<SessionTab,string>={conversation:'对话',runs:'轮次',files:'文件',evidence:'证据与设置'}

  function tabKeyDown(event:KeyboardEvent<HTMLButtonElement>,index:number){
    let target:number|undefined
    if(event.key==='ArrowRight')target=(index+1)%tabOrder.length
    if(event.key==='ArrowLeft')target=(index-1+tabOrder.length)%tabOrder.length
    if(event.key==='Home')target=0
    if(event.key==='End')target=tabOrder.length-1
    if(target===undefined)return
    event.preventDefault()
    p.onTab(tabOrder[target])
    document.getElementById(`session-tab-${tabOrder[target]}`)?.focus()
  }

  return <>
    <FilterBar {...p.filters} devices={p.devices} models={p.models}
      onDate={value=>p.onFilter({date:value})} onRange={value=>p.onFilter({rangeDays:value})}
      onTz={value=>p.onFilter({tz:value})} onDevice={value=>p.onFilter({deviceId:value})}
      onAgent={value=>p.onFilter({agent:value})} onModel={value=>p.onFilter({model:value})}
      label="会话筛选"/>
    <div className="sessionScope">显示 {p.filters.rangeDays===1?p.filters.date:`截至 ${p.filters.date} 的近 ${p.filters.rangeDays} 天`} · {p.filters.deviceId?'已筛选设备':'全部设备'} · {p.filters.agent||'全部 Agent'} · {p.filters.model||'全部模型'}。这些筛选与总览同步。<button className="textButton" onClick={()=>p.onFilter({deviceId:'',agent:'',model:''})}>清除来源筛选</button></div>
    <div className={'three '+(p.selectedId?'detailActive':'')}>
      <section className="listPane" aria-label="会话列表">
        <form className="search" onSubmit={doSearch}><input aria-label="搜索会话、工作目录或已保存正文" placeholder="搜索话题、路径或已保存正文" value={query} onChange={e=>setQuery(e.target.value)}/><button>搜索</button></form>
        {searchError&&<p className="inlineError" role="alert">{searchError}</p>}
        {search&&<><div className="listTitle">搜索结果 · {search.length} <button className="textButton" onClick={()=>setSearch(null)}>清除</button></div><p className="listHint">搜索标题、工作目录和已保存正文；未采集正文无法搜索。{searchedQuery.length===1?'单字搜索最多显示 50 条，请结合日期或更长词缩小范围。':''}</p>{search.length?search.map(result=><button className="result" key={result.id} onClick={()=>p.onSelect(result.session_id,result.matched_message_id?{kind:'message',id:result.matched_message_id}:{kind:'latest'},'conversation')}><b>{sessionLabel({...result,id:result.session_id,agent:result.agent||'Agent'})}</b><small>{result.device_name||'设备未知'} · {result.agent||'Agent'} · {timestamp(result.match_at||result.last_activity)} · {result.match_type==='stored_message'?'正文命中':result.match_type==='working_directory'?'工作目录命中':'标题命中'}</small><span>{result.excerpt||'标题或路径匹配'}</span></button>):<div className="empty">没有匹配的已保存证据。若正文未采集，结果可能不完整。</div>}</>}
        <div className="listTitle">所选日期内活动 · 已加载 {p.sessions.length}</div>
        {p.sessions.length?p.sessions.map(item=><SessionRow key={item.id} session={item} active={p.selectedId===item.id} tz={p.filters.tz} onClick={()=>p.onSelect(item.id,p.filters.rangeDays===1?{kind:'date'}:{kind:'latest'},'conversation')}/>):<div className="empty">当前范围暂无已索引活动。请检查日期、来源扫描状态或放宽筛选。</div>}
        {p.sessionCursor&&<button className="loadMore" onClick={p.onMoreSessions}>加载更多会话</button>}
      </section>
      <section className="detailPane" aria-label="会话详情">
        {session?<>
          <button className="mobileBack" onClick={()=>p.onSelect('',{kind:'latest'},'conversation')}>← 返回会话列表</button>
          {!selectedFromList&&<div className="scopeNotice" role="status">此会话未出现在当前已加载列表中；它可能在所选日期之外，也可能位于后续页。详情仍可直接查看。</div>}
          <div className="detailHead">
            <div className="eyebrow">{session.agent.toUpperCase()} · {session.device_name} · {session.profile}</div>
            {rename?<form className="renameForm" onSubmit={saveTitle}><input aria-label="自定义会话标题" value={renameText} onChange={event=>setRenameText(event.target.value)} maxLength={120} autoFocus/><button>保存</button><button type="button" onClick={()=>setRename(false)}>取消</button></form>:<div className="titleLine"><h2>{sessionLabel(session)}</h2><button className="textButton" onClick={()=>{setRenameText(session.title||'');setRename(true)}}>改名</button></div>}
            <p className="sessionMetaLine">最近已记录活动 {timestamp(session.last_activity)} · 已识别 {runCount} 轮 · 已索引 {messageCount} 条消息</p>
            <p className="directoryLine"><strong>来源记录的工作目录：</strong>{session.cwd?<code title={session.cwd}>{session.cwd}</code>:'未记录'} <span>（目录名不等于对话主题，也不代表文件由此会话产生）</span></p>
          </div>
          <div className="coverageLine" role="status">
            <span>正文覆盖：{coverage?`可读 ${coverage.readable}/${coverage.total}；未保存 ${coverage.missing}`:`当前已载入可读 ${readablePage}/${totalPage}，整段覆盖待统计`}</span>
            <span>{contentPolicy==='full_content'?'当前来源允许保存后续脱敏正文':contentPolicy==='stats_only'?'当前来源仅保存统计':contentPolicy==='excluded'?'此来源已排除':'来源策略待确认'}</span>
            {(coverage?.missing||totalPage-readablePage)>0&&<button className="textButton" onClick={p.onSettings}>查看历史补采 →</button>}
          </div>
          <div className="detailTabs" role="tablist" aria-label="会话详情分类">{tabOrder.map((tab,index)=><button key={tab} id={`session-tab-${tab}`} role="tab" aria-selected={p.tab===tab} tabIndex={p.tab===tab?0:-1} aria-controls="session-tabpanel" className={p.tab===tab?'active':''} onClick={()=>p.onTab(tab)} onKeyDown={event=>tabKeyDown(event,index)}>{tab==='files'?'文件证据':tab==='runs'?'已识别轮次':tabText[tab]}{tab==='runs'?` ${detail?.runs.length??runCount}`:tab==='files'?(detail?.file_evidence_state==='not_collected'?' · 未采集':detail?` ${detail.files.length}`:''):''}</button>)}</div>
          {detailLoading&&<div className="loadingLine" role="status">正在读取会话证据…</div>}
          {detailError&&<div className="inlineError" role="alert">{detailError}<button onClick={()=>setLocalRefresh(value=>value+1)}>重试</button></div>}
          <div id="session-tabpanel" role="tabpanel" aria-labelledby={`session-tab-${p.tab}`} className="sessionTabPanel">
            {p.tab==='conversation'&&<>
              <div className="tabIntro"><strong>{p.focus.kind==='latest'?'最近活动':p.focus.kind==='date'?`${p.filters.date} 的活动`:p.focus.kind==='run'?'已定位到轮次':'已定位到消息'}</strong><button className="textButton" onClick={()=>p.onSelect(session.id,{kind:'latest'},'conversation')}>查看最近消息</button></div>
              {detail?.items.map(message=><article className={'message '+(p.focus.kind==='message'&&p.focus.id===message.id?'focusedMessage':'')} id={`message-${message.id}`} key={message.id}>
                <div className="messageMeta"><b>{message.role==='user'?'用户':message.role==='assistant'?'Agent':message.role}</b><time>{timestamp(message.occurred_at)}</time></div>
                {message.body?<p>{message.body}</p>:<p className="missingBody">{message.content_state==='stats_only'?'正文未采集：当时的来源策略仅保存统计。':message.omission_reason?`正文不可用：${message.omission_reason}`:'正文未保存或来源未提供。'}{message.source_char_count!=null?` 来源字符数 ${message.source_char_count}。`:''}</p>}
                <div className="messageActions">{message.run_id?<button className="textButton" onClick={()=>p.onSelect(session.id,{kind:'run',id:message.run_id||undefined},'runs')}>{message.turn_mapping_basis==='unique_time_window'?'可能关联的轮次（按时间推断）':'查看所属轮次'} →</button>:<small>轮次待归属</small>}</div>
              </article>)}
              {detail&&!detail.items.length&&<div className="empty">这个位置没有可显示的消息；可切换到轮次查看活动边界。</div>}
              {Boolean(detail?.prev_cursor||detail?.next_cursor)&&<button className="loadMore" onClick={()=>void moreMessages()}>{detail?.prev_cursor?'加载更早消息':'继续加载消息'}</button>}
            </>}
            {p.tab==='runs'&&<>
              <p className="tabHelp">选择一轮查看时间、模型和已能归属的消息。没有归属证据的消息会保留为“轮次待归属”。</p>
              {detail?.runs.map((run,index)=><button key={run.id} className={'runCard '+(p.focus.kind==='run'&&p.focus.id===run.id?'selected':'')} onClick={()=>p.onSelect(session.id,{kind:'run',id:run.id},'runs')} aria-current={p.focus.kind==='run'&&p.focus.id===run.id?'true':undefined}>
                <span><b>第 {index+1} 轮 · {statusName(run.status)}</b><small>{timestamp(run.start_at)}–{run.end_at?compactTime(run.end_at):'结束未知'} · {run.model||'模型未归属'}</small></span><span className="runFacts">{run.message_count===undefined?'消息归属待统计':`${run.message_count} 条消息 / ${run.readable_count??0} 条可读`} · {run.duration_ms===null?'耗时未知':`${Math.round(run.duration_ms/1000)} 秒`}</span>
              </button>)}
              {detail&&!detail.runs.length&&<div className="empty">来源没有可识别的轮次边界，不能推断没有活动。</div>}
              {activeRun&&<section className="focusedRun"><h3>所选轮次 · {statusName(activeRun.status)}</h3><p>{timestamp(activeRun.start_at)}–{timestamp(activeRun.end_at)} · {activeRun.model||'模型未知'}</p>{activeRunMessages.some(message=>message.turn_mapping_basis==='unique_time_window')&&<p className="tabHelp">其中部分消息按唯一时间窗口关联，属于推断，尚无原生轮次 ID 直接确认。</p>}{activeRunMessages.length?activeRunMessages.map(message=><article className="message" key={message.id}><div className="messageMeta"><b>{message.role==='user'?'用户':message.role==='assistant'?'Agent':message.role}</b><time>{timestamp(message.occurred_at)}</time></div><p className={message.body?'':'missingBody'}>{message.body||'此条正文未保存'}</p></article>):<p className="missingBody">当前未找到能可靠归属到这一轮的消息。可在“对话”查看按时间排列的内容。</p>}{detail?.next_cursor&&<button className="loadMore" onClick={()=>void moreMessages()}>加载这一轮的更多消息</button>}</section>}
              {(detail?.unassigned_message_count||0)>0&&<p className="tabHelp">另有 {detail?.unassigned_message_count} 条消息尚不能可靠归属到具体轮次。</p>}
            </>}
            {p.tab==='files'&&<>
              <p className="tabHelp">只展示有来源证据的文件操作。工作目录中的文件不会自动算作会话产物。</p>
              {detail?.files.length?detail.files.map(file=><button className={'fileCard '+(selectedFile?.event_id===file.event_id?'selected':'')} key={file.event_id} onClick={()=>setSelectedFile(file)}><b>{file.relative_path||file.native_path||'路径未知'}</b><small>{relationName(file.relation)} · {size(file.size_bytes)} · 来源操作 {timestamp(file.operation_at)}</small></button>):<div className="empty">文件证据尚未采集或无法归属；不能据此判断本会话没有生成文件。</div>}
              {selectedFile&&<section className="fileDetail"><h3>文件证据</h3>
                {fileError&&<p role="alert">{fileError}</p>}{!fileDetail&&!fileError&&<p>正在读取文件依据…</p>}
                {fileDetail&&<>
                  <dl><dt>来源路径</dt><dd>{fileDetail.file.native_path||'未记录'}</dd>
                    <dt>关系</dt><dd>{relationName(fileDetail.file.relation)} · {operationName(fileDetail.file.operation_status)}</dd>
                    <dt>来源记录的大小</dt><dd>{size(fileDetail.file.size_bytes)}</dd>
                    <dt>来源记录的操作时间</dt><dd>{timestamp(fileDetail.file.operation_at)}</dd>
                    <dt>来源记录的修改时间</dt><dd>{timestamp(fileDetail.file.mtime)}</dd>
                    <dt>来源记录的文件检查时间</dt><dd>{timestamp(fileDetail.file.checked_at)}</dd>
                    <dt>当前文件快照</dt><dd>{accessName(currentAccess?.result?.status||currentAccess?.state)}{currentAccess?.checked_at?` · 检查于 ${timestamp(currentAccess.checked_at)}`:''}</dd>
                    {localAccess?.result.status==='exists'&&<><dt>当前快照大小</dt><dd>{size(localAccess.result.size_bytes)}</dd><dt>当前快照修改时间</dt><dd>{localAccess.result.mtime_ns?new Date(Math.floor(localAccess.result.mtime_ns/1_000_000)).toLocaleString('zh-CN',{timeZone:p.filters.tz}):'未返回'}</dd></>}
                    <dt>关系依据</dt><dd>{evidenceName(fileDetail.file.evidence_status)}</dd></dl>
                  {source?.execution_surface==='local'?<button className="textButton" disabled={localCheckBusy} onClick={()=>void checkLocalFile()}>{localCheckBusy?'正在检查本机文件…':'检查本机文件当前状态'}</button>:fileDetail.maps.some(map=>map.environment_id===session.device_id)&&fileDetail.file.logical_root&&fileDetail.file.relative_path?<button className="textButton" disabled={Boolean(fileCheckJob)} onClick={()=>void checkFile()}>{fileCheckJob?'正在等待设备检查…':'请求设备检查当前文件'}</button>:<p className="tabHelp">此目录尚未登记可检查的位置，暂无法核实当前文件是否存在。</p>}
                  {fileDetail.file.run_id&&<button className="textButton" onClick={()=>p.onSelect(session.id,{kind:'run',id:fileDetail.file.run_id||undefined},'runs')}>查看关联轮次 →</button>}
                </>}
              </section>}
            </>}
            {p.tab==='evidence'&&<><h3>来源与完整度</h3><dl className="evidenceList"><dt>Agent / 设备</dt><dd>{session.agent} / {session.device_name}</dd><dt>原生会话 ID</dt><dd><code>{(session as Session&{native_id?:string}).native_id||'未提供'}</code></dd><dt>来源记录的工作目录</dt><dd><code>{session.cwd||'未记录'}</code></dd><dt>工作目录当前状态</dt><dd>尚未检查；目录名不代表会话主题</dd><dt>正文策略</dt><dd>{contentPolicy==='full_content'?'后续正文脱敏保存':contentPolicy==='stats_only'?'仅保存统计':contentPolicy==='excluded'?'来源已排除':'未知'}</dd><dt>正文覆盖</dt><dd>{coverage?`${coverage.readable}/${coverage.total} 条可读`:'整段覆盖尚未计算'}</dd></dl><button className="loadMore" onClick={p.onSettings}>管理来源与历史补采</button><HandoffPanel key={session.id} sessionId={session.id} sourceDeviceId={session.device_id} devices={p.devices} csrf={p.csrf}/><details className="privacyControls"><summary>隐私与删除</summary><p>删除此会话在工作台内已保存的正文、标题和文件证据，并阻止之后扫描把它们重新导入；统计默认保留。已有备份可能暂时保留旧内容。</p><button onClick={()=>void deleteSavedContent()}>删除此会话已保存内容</button></details></>}
          </div>
        </>:<div className="selectPrompt">从左侧选择会话，查看具体对话、轮次和文件证据。</div>}
      </section>
      <aside className="contextPane" aria-label="会话导航"><h3>当前会话</h3>{session?<><p><b>{sessionLabel(session)}</b></p><p className="muted">{session.device_name} · {session.agent}</p><div className="contextLabel">快速定位轮次</div>{detail?.runs.slice(-8).reverse().map(run=><button className="contextLink" key={run.id} onClick={()=>p.onSelect(session.id,{kind:'run',id:run.id},'runs')}><b>{statusName(run.status)}</b><small>{timestamp(run.start_at)}</small></button>)}<button className="textButton" onClick={()=>p.onTab('runs')}>查看全部轮次 →</button><div className="contextLabel">文件证据</div><p className="muted">{detail?.files.length?`${detail.files.length} 条来源记录`:'尚未采集到可归属文件证据'}</p><button className="textButton" onClick={()=>p.onTab('files')}>查看文件 →</button></>:<p className="muted">选择会话后显示定位入口</p>}</aside>
    </div>
  </>
}
