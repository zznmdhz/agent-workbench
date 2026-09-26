import {useEffect, useRef, useState} from 'react'
import {TimelinePanel} from './TimelinePanel'
import {FilterBar} from './FilterBar'
import {sessionLabel, workingDirectoryName} from './sessionIdentity'
import './overview.css'

type Metric={id:string,value:number|null,unit:string,quality_note:string,included_count:number,excluded_count:number,verification_state?:string,usage_explanation?:string}
type Trend={day:string,runs:number,completed:number,human_chars:number,counter_tokens:number}
type Device={id:string,name:string,os:string,last_heartbeat:string|null,pending_count:number}
type Session={id:string,title?:string|null,display_title?:string|null,preview?:string|null,agent:string,device_name:string,last_activity?:string|null,latest_matching_activity?:string|null,matching_activity_basis?:string|null,cwd?:string|null}
type Comparison={device_id:string,runs:number,settled_duration_ms:number,active_wall_ms:number,human_chars:number,counter_tokens:number}
type Contributor={session_id:string,title:string|null,display_title?:string|null,preview?:string|null,last_activity?:string|null,cwd:string|null,agent:string,device_name:string|null,amount:number,evidence_count:number}
type UsageExplanation={verification_state:string,cached_input_is_subset_of_input:boolean,cached_input_share_of_input?:number|null,non_cached_input_tokens?:number|null,counter_arithmetic_gap:number|null,arithmetic_note:string,counter_unallocated_intervals:number,incremental_and_cumulative_overlap:string}
type Props={date:string,onDate:(v:string)=>void,rangeDays:number,onRange:(v:number)=>void,tz:string,onTz:(v:string)=>void,deviceId:string,onDevice:(v:string)=>void,agent:string,onAgent:(v:string)=>void,model:string,onModel:(v:string)=>void,models:string[],metrics:Metric[],trend:Trend[],comparison:Comparison[],devices:Device[],sessions:Session[],sourcesCount:number,pendingTotal:number,refreshTick:number,asOf:string|null,usageExplanation:UsageExplanation|null,onSelect:(id:string,runId?:string)=>void,onSessions:()=>void,onDevices:()=>void}
const number=(n:number|null,unit='')=>n===null?'未采集':`${new Intl.NumberFormat('zh-CN').format(n)}${unit}`
const minutes=(n:number|null)=>n===null?'未采集':number(Math.round(n/60000),' 分钟')

export function Overview(p:Props){
  const [opened,setOpened]=useState<string|null>(null)
  const [contributors,setContributors]=useState<{items:Contributor[],total_sessions:number,note:string|null}|null>(null)
  const closeRef=useRef<HTMLButtonElement>(null)
  const dialogRef=useRef<HTMLElement>(null)
  useEffect(()=>{
    if(!opened)return
    const previous=document.activeElement as HTMLElement|null
    closeRef.current?.focus()
    const close=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){setOpened(null);return}
      if(event.key!=='Tab')return
      const focusable=[...(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled)')||[])]
      if(!focusable.length)return
      if(event.shiftKey&&document.activeElement===focusable[0]){event.preventDefault();focusable.at(-1)?.focus()}
      else if(!event.shiftKey&&document.activeElement===focusable.at(-1)){event.preventDefault();focusable[0].focus()}
    }
    window.addEventListener('keydown',close)
    return()=>{window.removeEventListener('keydown',close);previous?.focus()}
  },[opened])
  useEffect(()=>{
    if(!opened){setContributors(null);return}
    const d=new Date(`${p.date}T12:00:00Z`);d.setUTCDate(d.getUTCDate()+1-p.rangeDays)
    const params=new URLSearchParams({metric_id:opened,day:d.toISOString().slice(0,10),through:p.date,tz:p.tz})
    if(p.deviceId)params.append('device_ids',p.deviceId)
    if(p.agent)params.append('agent_ids',p.agent)
    if(p.model)params.append('model_ids',p.model)
    let cancelled=false
    setContributors(null)
    fetch(`/v1/stats/contributors?${params}`,{credentials:'same-origin'}).then(r=>r.ok?r.json():Promise.reject())
      .then(x=>{if(!cancelled)setContributors(x)}).catch(()=>{if(!cancelled)setContributors({items:[],total_sessions:0,note:'证据加载失败，请刷新后重试'})})
    return()=>{cancelled=true}
  },[opened,p.date,p.rangeDays,p.tz,p.deviceId,p.agent,p.model,p.refreshTick])
  const metric=(id:string)=>p.metrics.find(m=>m.id===id)
  const value=(id:string)=>metric(id)?.value??null
  const timestamp=(value:string)=>new Date(value).toLocaleString('zh-CN',{timeZone:p.tz})
  const incrementalInput=value('input_tokens')
  const incrementalOutput=value('output_tokens')
  const counterInput=value('counter_input_tokens')
  const counterOutput=value('counter_output_tokens')
  const cachedInput=value('counter_cached_input_tokens')
  const showIncremental=incrementalInput!==null||incrementalOutput!==null||counterInput===null&&counterOutput===null
  const showCounter=counterInput!==null||counterOutput!==null||incrementalInput===null&&incrementalOutput===null
  const ratio=(input:number|null,output:number|null)=>input!==null&&output!==null&&output>0?`输入:输出约 ${Math.round(input/output)}:1`:'输入与输出尚不能成对比较'
  const cards=[
    {id:'run_count',title:'开始轮次',value:number(value('run_count')),hint:'所选日期内开始的顶层轮次',detail:'只统计已发现且能确定开始时间的顶层轮次。'},
    {id:'settled_duration_ms',title:'累计执行',value:minutes(value('settled_duration_ms')),hint:'并行轮次分别计入',detail:'已结束且有可信时间边界的轮次，跨日期按实际落在日期内的时间切分。'},
    {id:'active_wall_ms',title:'活动时间',value:minutes(value('active_wall_ms')),hint:'并行时间只计一次',detail:'把执行区间取并集。它不能把每个轮次的分钟数直接相加。'},
    {id:'human_input_chars',title:'人类输入',value:number(value('human_input_chars'),' 字'),hint:'原始输入的 Unicode 字符数',detail:'只计来源识别为人类输入的消息。开启模型筛选时，缺少消息到唯一模型的归属证据，因此显示未采集。'},
  ]
  if(showIncremental)cards.push(
    {id:'input_tokens',title:'逐请求输入 Token',value:number(incrementalInput),hint:'来源逐请求记录',detail:'只汇总所选范围内的逐请求增量；不与累计快照相加，也不代表所有 Agent 的输入总量。'},
    {id:'output_tokens',title:'逐请求输出 Token',value:number(incrementalOutput),hint:'来源逐请求记录',detail:'只汇总所选范围内的逐请求增量；不与累计快照相加，也不代表所有 Agent 的输出总量。'},
  )
  if(showCounter)cards.push(
    {id:'counter_input_tokens',title:'Codex 累计输入差值',value:number(counterInput),hint:'同日计数器区间',detail:'Codex 会话累计输入的同日差值，包含缓存输入；开头余额和跨日间隔不归入日期。不可与逐请求数相加。'},
    {id:'counter_output_tokens',title:'Codex 累计输出差值',value:number(counterOutput),hint:'同日计数器区间',detail:'Codex 会话累计输出的同日差值；开头余额和跨日间隔不归入日期。不可与逐请求数相加。'},
  )
  const selected=cards.find(c=>c.id===opened)
  const maxRuns=Math.max(1,...p.trend.map(x=>x.runs))
  return <>
    <FilterBar {...p} label="总览筛选"/>
    {p.pendingTotal>0&&<div className="notice">正在导入本机历史记录，还有 {number(p.pendingTotal)} 条待处理；数据会自动刷新。</div>}
    {p.sourcesCount===0&&<div className="notice">尚未发现 Codex 或 Hermes 数据来源，请到“设备与设置”查看。</div>}
    <p className="dataAsOf">统计生成于 {p.asOf?timestamp(p.asOf):'时间待确认'}（{p.tz}）；来源扫描时间请在“设备与设置”查看。</p>
    {(showIncremental||showCounter)&&<p className="usageContext">{showIncremental?`逐请求：${ratio(incrementalInput,incrementalOutput)}。`:''}{showCounter?`Codex 累计差值：${ratio(counterInput,counterOutput)}。`:''}{showIncremental&&showCounter?'两种记录可能覆盖同一使用量，不能相加为全来源总数。':''}{cachedInput!==null?`累计缓存输入 ${number(cachedInput)}，是 Codex 累计输入的子集，不再叠加。`:''}{p.usageExplanation?.cached_input_share_of_input!==null&&p.usageExplanation?.cached_input_share_of_input!==undefined&&p.usageExplanation.cached_input_share_of_input<=1?`所选范围内，缓存约占累计输入的 ${(p.usageExplanation.cached_input_share_of_input*100).toFixed(1)}%；非缓存输入 ${number(p.usageExplanation.non_cached_input_tokens??null)}。`:''}这些比例由已归属数相除，不能视为逐轮配对或源事件对账结果。点击指标可查看依据。</p>}
    <section className="cards" aria-label="活动指标">{cards.map(c=><button key={c.id} className="card metricCard" onClick={()=>setOpened(c.id)} aria-label={`查看${c.title}说明与相关会话`}><small>{c.title}</small><strong>{c.value}</strong><span>{c.hint}</span><em>查看依据 →</em></button>)}</section>
    <div className="columns"><section className="panel"><div className="panelHead"><h2>所选范围内的活动会话</h2><button className="textButton" onClick={p.onSessions}>查看所选范围 →</button></div>{p.sessions.length?p.sessions.slice(0,8).map(s=><button className="sessionRow" key={s.id} onClick={()=>p.onSelect(s.id)}><span className="agentGlyph">{s.agent==='codex'?'C':'H'}</span><span className="sessionText"><b>{sessionLabel(s)}</b><small>{s.device_name} · {s.agent} · {s.matching_activity_basis==='overlapping_run_no_event'?'跨日活动，日期内无精确时间':`范围内活动 ${s.latest_matching_activity?timestamp(s.latest_matching_activity):'时间待归属'}`}{workingDirectoryName(s)?` · 来源工作目录：${workingDirectoryName(s)}`:''}</small></span></button>):<div className="empty">所选范围内没有已索引活动；来源覆盖可能不完整</div>}</section>
      <section className="panel"><div className="panelHead"><h2>设备状态</h2><button className="textButton" onClick={p.onDevices}>管理 →</button></div>{p.devices.map(d=><button className="deviceRow deviceButton" key={d.id} onClick={()=>p.onDevice(d.id)}><span className="deviceIcon">⌘</span><span><b>{d.name}</b><small>{d.os} · {d.last_heartbeat?`最后心跳 ${timestamp(d.last_heartbeat)}`:'等待首次心跳'}</small></span><span className="badge">{d.pending_count} 待传</span></button>)}</section></div>
    {p.rangeDays===1?<TimelinePanel day={p.date} tz={p.tz} deviceId={p.deviceId} agent={p.agent} model={p.model} refreshTick={p.refreshTick} onSelect={p.onSelect}/>:<section className="panel trendPanel"><div className="panelHead"><h2>每日活动趋势</h2><span className="muted">点击一天查看当天时间线</span></div><div className="trendRows">{p.trend.map(item=><button key={item.day} className="trendRow" onClick={()=>{p.onDate(item.day);p.onRange(1)}}><span>{item.day.slice(5)}</span><span className="trendTrack"><span style={{width:`${Math.max(0,item.runs/maxRuns*100)}%`}}/></span><b>{item.runs} 轮已识别</b><small>{item.counter_tokens?`${number(item.counter_tokens)} Token`:'无可归属 Token 依据'}</small></button>)}</div></section>}
    <section className="panel comparePanel"><div className="panelHead"><h2>设备对比</h2><span className="muted">按所选范围与筛选统计；活动时间每台设备单独去重</span></div><div className="compareTable"><div className="compareRow head"><span>设备</span><span>已识别开始轮次</span><span>累计执行</span><span>活动时间</span><span>人类输入</span><span>累计 Token 差值</span></div>{p.comparison.map(row=><button key={row.device_id} className="compareRow" onClick={()=>p.onDevice(row.device_id)}><span>{p.devices.find(d=>d.id===row.device_id)?.name||'未知设备'}</span><span>{number(row.runs)}</span><span>{minutes(row.settled_duration_ms)}</span><span>{minutes(row.active_wall_ms)}</span><span>{number(row.human_chars)}</span><span>{row.counter_tokens?number(row.counter_tokens):'无可归属依据'}</span></button>)}</div>{p.comparison.length===0&&<div className="empty">此范围暂无可比较的已索引活动</div>}</section>
    {selected&&<div className="metricOverlay" onClick={()=>setOpened(null)}><section ref={dialogRef} className="metricDialog" role="dialog" aria-modal="true" aria-label={`${selected.title}依据`} onClick={e=>e.stopPropagation()}><button ref={closeRef} className="dialogClose" onClick={()=>setOpened(null)} aria-label="关闭">×</button><small>指标说明</small><h2>{selected.title}</h2><strong>{selected.value}</strong><p>{selected.detail}</p><p className="muted">{metric(selected.id)?.quality_note||selected.hint}</p>{metric(selected.id)?.verification_state&&<p className="verificationNote">核验状态：{metric(selected.id)?.verification_state==='not_source_reconciled'?'源事件尚未逐条对账':metric(selected.id)?.verification_state==='derived_from_recorded_facts'?'由已记录事实计算':metric(selected.id)?.verification_state}</p>}{selected.id.includes('tokens')&&<><p>{selected.id.startsWith('counter_')?`累计缓存输入${cachedInput===null?'未采集或未归属':`${number(cachedInput)}，已包含在 Codex 累计输入内`}；不能再加到输入。`:'逐请求来源的缓存输入细分尚未提供，不能用 Codex 累计缓存量解释这项指标。'}</p><p>{p.usageExplanation?.arithmetic_note||'分项能相加，不等于源事件和去重已核验。'}无法归属的计数区间：{p.usageExplanation?.counter_unallocated_intervals??'未知'}；逐请求与累计快照可能重叠，不能直接相加。</p></>}<p className="muted">已观测证据：纳入 {metric(selected.id)?.included_count??0} 条；未纳入 {metric(selected.id)?.excluded_count??0} 条。未发现的来源历史不在这些数量内。</p><h3>已归属会话 {contributors?`· ${contributors.total_sessions}`:''}</h3>{contributors?.note&&<p className="muted">{contributors.note}</p>}<div className="contributorList">{contributors?contributors.items.length?contributors.items.map(item=><button key={item.session_id} onClick={()=>{setOpened(null);p.onSelect(item.session_id)}}><span><b>{sessionLabel({...item,id:item.session_id})}</b><small>{item.device_name||'设备未知'} · {item.agent} · {item.evidence_count} 条证据{workingDirectoryName({...item,id:item.session_id})?` · 来源工作目录：${workingDirectoryName({...item,id:item.session_id})}`:''}</small></span><strong>{selected.id.endsWith('_ms')?minutes(item.amount):number(item.amount)}</strong></button>):<div className="empty">没有可归属到会话的证据</div>:<div className="empty">正在加载来源证据…</div>}</div><button className="loadMore" onClick={()=>{setOpened(null);p.onSessions()}}>查看当前筛选的已索引会话 →</button></section></div>}
  </>
}
