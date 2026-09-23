import {useEffect, useState} from 'react'
import './timeline.css'

type TimeRun={id:string,session_id:string,title:string|null,agent:string,device_name:string|null,status:string,visible_start_ms:number,visible_end_ms:number|null}
type Timeline={window_start_ms:number,window_end_ms:number,items:TimeRun[],truncated:boolean}

export function TimelinePanel({day,onSelect}:{day:string,onSelect:(id:string)=>void}){
  const [data,setData]=useState<Timeline|null>(null)
  useEffect(()=>{
    const zone=Intl.DateTimeFormat().resolvedOptions().timeZone
    fetch(`/v1/timeline?day=${day}&tz=${encodeURIComponent(zone)}`,{credentials:'same-origin'})
      .then(r=>r.ok?r.json():Promise.reject()).then(setData).catch(()=>setData(null))
  },[day])
  if(!data)return null
  const span=data.window_end_ms-data.window_start_ms
  return <section className="panel timelinePanel"><div className="panelHead"><h2>每日时间线</h2><span className="muted">未知结束点以圆点表示</span></div>
    <div className="hourScale"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
    {data.items.length?data.items.slice(0,30).map(r=>{
      const left=Math.max(0,Math.min(100,(r.visible_start_ms-data.window_start_ms)/span*100))
      const width=r.visible_end_ms===null?0:Math.max(.5,(r.visible_end_ms-r.visible_start_ms)/span*100)
      return <button className="timelineRow" key={r.id} onClick={()=>onSelect(r.session_id)} title={`${r.device_name||'设备未知'} · ${r.agent} · ${r.status}`}>
        <span className="timelineLabel">{r.device_name||'设备未知'} / {r.agent}<small>{r.title||'未命名会话'}</small></span>
        <span className="timelineTrack"><span className={r.visible_end_ms===null?'timelinePoint':'timelineBar'} style={{left:`${left}%`,width:r.visible_end_ms===null?undefined:`${Math.min(width,100-left)}%`}}/></span>
      </button>
    }):<div className="empty">这一天没有已识别的轮次边界</div>}
    {data.truncated&&<p className="muted">只显示前 1,000 条轮次</p>}
  </section>
}
