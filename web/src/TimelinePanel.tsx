import {useEffect, useState} from 'react'
import './timeline.css'

type TimeRun={id:string,session_id:string,title:string|null,agent:string,device_name:string|null,status:string,visible_start_ms:number,visible_end_ms:number|null}
type Timeline={window_start_ms:number,window_end_ms:number,items:TimeRun[],truncated:boolean}

export function TimelinePanel({day,tz,deviceId,agent,model,onSelect,refreshTick}:{day:string,tz:string,deviceId:string,agent:string,model:string,onSelect:(id:string)=>void,refreshTick:number}){
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
  return <section className="panel timelinePanel"><div className="panelHead"><h2>{day} 时间线</h2><span className="muted">{data.items.length} 条轮次 · 未知结束以圆点表示</span></div>
    <div className="hourScale"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
    <div className={expanded?'timelineRows expanded':'timelineRows'}>
    {visible.length?visible.map(r=>{
      const left=Math.max(0,Math.min(100,(r.visible_start_ms-data.window_start_ms)/span*100))
      const width=r.visible_end_ms===null?0:Math.max(.5,(r.visible_end_ms-r.visible_start_ms)/span*100)
      return <button className="timelineRow" key={r.id} onClick={()=>onSelect(r.session_id)} title={`${r.device_name||'设备未知'} · ${r.agent} · ${r.status}`}>
        <span className="timelineLabel">{r.device_name||'设备未知'} / {r.agent}<small>{r.title||'未命名会话'}</small></span>
        <span className="timelineTrack"><span className={r.visible_end_ms===null?'timelinePoint':'timelineBar'} style={{left:`${left}%`,width:r.visible_end_ms===null?undefined:`${Math.min(width,100-left)}%`}}/></span>
      </button>
    }):<div className="empty">这一天没有已识别的轮次边界</div>}
    </div>
    {data.items.length>8&&<button className="loadMore" onClick={()=>setExpanded(!expanded)}>{expanded?'收起时间线':`查看更多轮次（共 ${data.items.length} 条）`}</button>}
    {data.truncated&&<p className="muted">还有更多轮次未在此图展示，请进入会话查看。</p>}
  </section>
}
