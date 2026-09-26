import {useEffect, useState} from 'react'
import {TimelinePanel} from './TimelinePanel'
import './overview.css'

type Metric={id:string,value:number|null,unit:string,quality_note:string,included_count:number,excluded_count:number}
type Trend={day:string,runs:number,completed:number,human_chars:number,counter_tokens:number}
type Device={id:string,name:string,os:string,last_heartbeat:string|null,pending_count:number}
type Session={id:string,title:string|null,agent:string,device_name:string,last_activity:string|null,cwd:string|null}
type Comparison={device_id:string,runs:number,settled_duration_ms:number,active_wall_ms:number,human_chars:number,counter_tokens:number}
type Contributor={session_id:string,title:string|null,cwd:string|null,agent:string,device_name:string|null,amount:number,evidence_count:number}
type Props={date:string,onDate:(v:string)=>void,rangeDays:number,onRange:(v:number)=>void,tz:string,onTz:(v:string)=>void,deviceId:string,onDevice:(v:string)=>void,agent:string,onAgent:(v:string)=>void,model:string,onModel:(v:string)=>void,models:string[],metrics:Metric[],trend:Trend[],comparison:Comparison[],devices:Device[],sessions:Session[],sourcesCount:number,pendingTotal:number,refreshTick:number,onSelect:(id:string)=>void,onSessions:()=>void,onDevices:()=>void}
const number=(n:number|null,unit='')=>n===null?'未采集':`${new Intl.NumberFormat('zh-CN').format(n)}${unit}`
const minutes=(n:number|null)=>n===null?'未采集':number(Math.round(n/60000),' 分钟')

export function Overview(p:Props){
  const [opened,setOpened]=useState<string|null>(null)
  const [contributors,setContributors]=useState<{items:Contributor[],total_sessions:number,note:string|null}|null>(null)
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
  const input=value('input_tokens')??value('counter_input_tokens')
  const output=value('output_tokens')??value('counter_output_tokens')
  const tokenBasis=value('input_tokens')!==null||value('output_tokens')!==null?'逐请求记录':'Codex 会话累计计数差值'
  const cards=[
    {id:'run_count',title:'开始轮次',value:number(value('run_count')),hint:'所选日期内开始的顶层轮次',detail:'只统计已发现且能确定开始时间的顶层轮次。'},
    {id:'settled_duration_ms',title:'累计执行',value:minutes(value('settled_duration_ms')),hint:'并行轮次分别计入',detail:'已结束且有可信时间边界的轮次，跨日期按实际落在日期内的时间切分。'},
    {id:'active_wall_ms',title:'活动时间',value:minutes(value('active_wall_ms')),hint:'并行时间只计一次',detail:'把执行区间取并集。它不能把每个轮次的分钟数直接相加。'},
    {id:'human_input_chars',title:'人类输入',value:number(value('human_input_chars'),' 字'),hint:'原始输入的 Unicode 字符数',detail:'只计来源识别为人类输入的消息。开启模型筛选时，缺少消息到唯一模型的归属证据，因此显示未采集。'},
    {id:'input_tokens',title:'输入 Token',value:number(input),hint:input===null?'来源暂未提供可归属数据':tokenBasis,detail:'优先显示逐请求输入用量；若无，则显示 Codex 会话累计输入计数在所选日期内的可确认差值。开头余额、跨日间隔不归入日期。'},
    {id:'output_tokens',title:'输出 Token',value:number(output),hint:output===null?'来源暂未提供可归属数据':tokenBasis,detail:'优先显示逐请求输出用量；若无，则显示 Codex 会话累计输出计数在所选日期内的可确认差值。开头余额、跨日间隔不归入日期。'},
  ]
  const selected=cards.find(c=>c.id===opened)
  const maxRuns=Math.max(1,...p.trend.map(x=>x.runs))
  return <>
    <section className="dashboardFilters" aria-label="总览筛选">
      <div className="rangeButtons" role="group" aria-label="日期范围">{[1,7,10,15].map(n=><button key={n} className={p.rangeDays===n?'active':''} onClick={()=>p.onRange(n)}>{n===1?'单日':`近 ${n} 天`}</button>)}</div>
      <label>结束日期 <input type="date" value={p.date} onChange={e=>p.onDate(e.target.value)}/></label>
      <label>设备 <select value={p.deviceId} onChange={e=>p.onDevice(e.target.value)}><option value="">全部设备</option>{p.devices.map(d=><option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
      <label>Agent <select value={p.agent} onChange={e=>p.onAgent(e.target.value)}><option value="">全部 Agent</option><option value="codex">Codex</option><option value="hermes">Hermes</option></select></label>
      <label>模型 <select value={p.model} onChange={e=>p.onModel(e.target.value)}><option value="">全部模型</option>{p.models.map(m=><option key={m} value={m}>{m}</option>)}</select></label>
      <label>报告时区 <select value={p.tz} onChange={e=>p.onTz(e.target.value)}><option value="Asia/Hong_Kong">香港时间</option><option value="UTC">UTC</option></select></label>
    </section>
    {p.pendingTotal>0&&<div className="notice">正在导入本机历史记录，还有 {number(p.pendingTotal)} 条待处理；数据会自动刷新。</div>}
    {p.sourcesCount===0&&<div className="notice">尚未发现 Codex 或 Hermes 数据来源，请到“设备与设置”查看。</div>}
    <section className="cards" aria-label="活动指标">{cards.map(c=><button key={c.id} className="card metricCard" onClick={()=>setOpened(c.id)} aria-label={`查看${c.title}说明与相关会话`}><small>{c.title}</small><strong>{c.value}</strong><span>{c.hint}</span><em>查看依据 →</em></button>)}</section>
    <div className="columns"><section className="panel"><div className="panelHead"><h2>最近会话</h2><button className="textButton" onClick={p.onSessions}>查看全部 →</button></div>{p.sessions.length?p.sessions.slice(0,8).map(s=><button className="sessionRow" key={s.id} onClick={()=>p.onSelect(s.id)}><span className="agentGlyph">{s.agent==='codex'?'C':'H'}</span><span className="sessionText"><b>{s.title||'未命名会话'}</b><small>{s.cwd?`工作目录：${s.cwd.split(/[\\/]/).filter(Boolean).at(-1)} · `:''}{s.device_name} · {s.agent} · {s.last_activity?new Date(s.last_activity).toLocaleString('zh-CN'):'时间未知'}</small></span></button>):<div className="empty">所选日期范围内没有已索引会话</div>}</section>
      <section className="panel"><div className="panelHead"><h2>设备状态</h2><button className="textButton" onClick={p.onDevices}>管理 →</button></div>{p.devices.map(d=><button className="deviceRow deviceButton" key={d.id} onClick={()=>p.onDevice(d.id)}><span className="deviceIcon">⌘</span><span><b>{d.name}</b><small>{d.os} · {d.last_heartbeat?`最后心跳 ${new Date(d.last_heartbeat).toLocaleString('zh-CN')}`:'等待首次心跳'}</small></span><span className="badge">{d.pending_count} 待传</span></button>)}</section></div>
    {p.rangeDays===1?<TimelinePanel day={p.date} tz={p.tz} deviceId={p.deviceId} agent={p.agent} model={p.model} refreshTick={p.refreshTick} onSelect={p.onSelect}/>:<section className="panel trendPanel"><div className="panelHead"><h2>每日活动趋势</h2><span className="muted">点击一天查看当天时间线</span></div><div className="trendRows">{p.trend.map(item=><button key={item.day} className="trendRow" onClick={()=>{p.onDate(item.day);p.onRange(1)}}><span>{item.day.slice(5)}</span><span className="trendTrack"><span style={{width:`${Math.max(0,item.runs/maxRuns*100)}%`}}/></span><b>{item.runs} 轮</b><small>{item.counter_tokens?`${number(item.counter_tokens)} Token`:'Token 未采集'}</small></button>)}</div></section>}
    <section className="panel comparePanel"><div className="panelHead"><h2>设备对比</h2><span className="muted">按所选范围与筛选统计；活动时间每台设备单独去重</span></div><div className="compareTable"><div className="compareRow head"><span>设备</span><span>开始轮次</span><span>累计执行</span><span>活动时间</span><span>人类输入</span><span>总 Token 差值</span></div>{p.comparison.map(row=><button key={row.device_id} className="compareRow" onClick={()=>p.onDevice(row.device_id)}><span>{p.devices.find(d=>d.id===row.device_id)?.name||'未知设备'}</span><span>{number(row.runs)}</span><span>{minutes(row.settled_duration_ms)}</span><span>{minutes(row.active_wall_ms)}</span><span>{number(row.human_chars)}</span><span>{row.counter_tokens?number(row.counter_tokens):'未采集'}</span></button>)}</div>{p.comparison.length===0&&<div className="empty">此范围暂无可比较活动</div>}</section>
    {selected&&<div className="metricOverlay" onClick={()=>setOpened(null)}><section className="metricDialog" role="dialog" aria-modal="true" aria-label={`${selected.title}依据`} onClick={e=>e.stopPropagation()}><button className="dialogClose" onClick={()=>setOpened(null)} aria-label="关闭">×</button><small>指标说明</small><h2>{selected.title}</h2><strong>{selected.value}</strong><p>{selected.detail}</p><p className="muted">{metric(selected.id)?.quality_note||selected.hint}</p><p className="muted">来源覆盖：{metric(selected.id)?.included_count??0} 条证据；未纳入：{metric(selected.id)?.excluded_count??0} 条。</p><h3>相关会话 {contributors?`· ${contributors.total_sessions}`:''}</h3>{contributors?.note&&<p className="muted">{contributors.note}</p>}<div className="contributorList">{contributors?contributors.items.length?contributors.items.map(item=><button key={item.session_id} onClick={()=>{setOpened(null);p.onSelect(item.session_id)}}><span><b>{item.title||'未命名会话'}</b><small>{item.cwd?`工作目录：${item.cwd.split(/[\\/]/).filter(Boolean).at(-1)} · `:''}{item.device_name||'设备未知'} · {item.agent} · {item.evidence_count} 条证据</small></span><strong>{selected.id.endsWith('_ms')?minutes(item.amount):number(item.amount)}</strong></button>):<div className="empty">没有可归属到会话的证据</div>:<div className="empty">正在加载来源证据…</div>}</div><button className="loadMore" onClick={()=>{setOpened(null);p.onSessions()}}>查看当前筛选的全部会话 →</button></section></div>}
  </>
}
