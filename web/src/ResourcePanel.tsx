import {useEffect, useState} from 'react'

type Device={id:string,name:string}
type Sample={id:number,pid:number,process_name:string,sampled_at:string,rss_bytes:number|null,cpu_percent:string|null}
const memory=(bytes:number|null)=>bytes===null?'未记录':`${(bytes/1048576).toFixed(1)} MB`

export function ResourcePanel({devices}:{devices:Device[]}){
  const [deviceId,setDeviceId]=useState('')
  const [samples,setSamples]=useState<Sample[]>([])
  const [notice,setNotice]=useState('')
  useEffect(()=>{if(!devices.some(device=>device.id===deviceId))setDeviceId(devices[0]?.id||'')},[devices,deviceId])
  useEffect(()=>{
    if(!deviceId){setSamples([]);return}
    let cancelled=false
    fetch(`/v1/resources?device_id=${encodeURIComponent(deviceId)}&limit=100`,{credentials:'same-origin'})
      .then(response=>response.ok?response.json():Promise.reject(new Error(`HTTP ${response.status}`)))
      .then(value=>{if(!cancelled){setSamples(value.items||[]);setNotice(value.notice||'')}})
      .catch(()=>{if(!cancelled){setSamples([]);setNotice('资源样本暂不可用。')}})
    return()=>{cancelled=true}
  },[deviceId])
  const latest=new Map<string,Sample>()
  for(const sample of samples){const key=`${sample.pid}-${sample.process_name}`;if(!latest.has(key))latest.set(key,sample)}
  return <section className="panel resourcePanel"><h2>进程资源观察值</h2><p className="muted">仅显示主动开启采样后留下的瞬时进程 RSS/CPU；共享进程不能精确分摊给某个会话，历史未采样的内存无法补算。</p><label>设备 <select value={deviceId} onChange={event=>setDeviceId(event.target.value)}><option value="">选择设备</option>{devices.map(device=><option value={device.id} key={device.id}>{device.name}</option>)}</select></label>{latest.size?<div className="resourceRows">{[...latest.values()].slice(0,12).map(sample=><div key={sample.id}><b>{sample.process_name||`进程 ${sample.pid}`}</b><span>RSS {memory(sample.rss_bytes)} · CPU {sample.cpu_percent??'未记录'}%</span><small>{new Date(sample.sampled_at).toLocaleString('zh-CN')} · PID {sample.pid}</small></div>)}</div>:<div className="empty">尚无此设备的资源采样。这里不会把空白解释为零内存。</div>}{notice&&<p className="muted">{notice}</p>}</section>
}
