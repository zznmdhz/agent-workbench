import {useEffect, useState} from 'react'
import {sessionLabel} from './sessionIdentity'
import './timeline.css'

type TimeRun={id:string,session_id:string,title:string|null,agent:string,device_name:string|null,status:string,visible_start_ms:number,visible_end_ms:number|null}
type Timeline={window_start_ms:number,window_end_ms:number,items:TimeRun[],truncated:boolean}

export function TimelinePanel({day,tz,deviceId,agent,model,onSelect,refreshTick}:{day:string,tz:string,deviceId:string,agent:string,model:string,onSelect:(id:string,runId:string)=>void,refreshTick:number}){
  const [data,setData]=useState<Timeline|null>(null)
  const [expanded,setExpanded]=useState(false)
  useEffect(()=>{
    const params=new URLSearchParams({day,tz})
    if(deviceId)params.append('device_ids',deviceId)
    if(agent)params.append('agent_ids',agent)
    if(model)params.append('model_ids',model)
    let cancelled=false
    fetch(`/v1/timeline?${params}`,{credentials:'same-origin'})
      .then(r=>r.ok?r.json():Promise.reject()).then(x=>{if(!cancelled)setData(x)}).catch(()=>{if(!cancelled)setData(null)})
    return()=>{cancelled=true}
  },[day,tz,deviceId,agent,model,refreshTick])
  if(!data)return <section className="panel timelinePanel"><h2>当日时间线</h2><div className="empty">正在加载时间线…</div></section>
  const span=data.window_end_ms-data.window_start_ms
  const visible=data.items.slice(0,expanded?100:8)
  const clock=(ms:number)=>new Intl.DateTimeFormat('zh-CN',{timeZone:tz,hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(ms))
  const status=(value:string)=>({completed:'已完成',running:'进行中',failed:'失败',cancelled:'已取消'} as Record<string,string>)[value]||value
  return <section className="panel timelinePanel"><div className="panelHead"><h2>{day} 时间线</h2><span className="muted">已加载 {data.items.length} 条可识别轮次边界 · 结束未知以圆点表示</span></div>
    <div className="hourScale"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
    <div className={expanded?'timelineRows expanded':'timelineRows'}>
    {visible.length?visible.map(r=>{
      const left=Math.max(0,Math.min(100,(r.visible_start_ms-data.window_start_ms)/span*100))
      const width=r.visible_end_ms===null?0:Math.max(.5,(r.visible_end_ms-r.visible_start_ms)/span*100)
      return <button className="timelineRow" key={r.id} onClick={()=>onSelect(r.session_id,r.id)} aria-label={`${sessionLabel({...r,id:r.session_id,last_activity:new Date(r.visible_start_ms).toISOString()})}，${clock(r.visible_start_ms)} 至 ${r.visible_end_ms===null?'结束未知':clock(r.visible_end_ms)}，${status(r.status)}，查看对应轮次`}>
        <span className="timelineLabel"><b>{sessionLabel({...r,id:r.session_id,last_activity:new Date(r.visible_start_ms).toISOString()})}</b><small>{r.device_name||'设备未知'} · {r.agent} · {status(r.status)}</small><small>{clock(r.visible_start_ms)}–{r.visible_end_ms===null?'结束未知':clock(r.visible_end_ms)}</small></span>
        <span className="timelineTrack"><span className={r.visible_end_ms===null?'timelinePoint':'timelineBar'} style={{left:`${left}%`,width:r.visible_end_ms===null?undefined:`${Math.min(width,100-left)}%`}}/></span>
      </button>
    }):<div className="empty">这一天尚无已识别的轮次边界；不能据此判断没有活动。</div>}
    </div>
    {data.items.length>8&&<button className="loadMore" onClick={()=>setExpanded(!expanded)}>{expanded?'收起时间线':`展开已加载的 ${data.items.length} 条轮次边界`}</button>}
    {data.truncated&&<p className="muted">还有更多轮次未在此图展示，请进入会话查看。</p>}
  </section>
}
