import {useState} from 'react'
import './handoff.css'

type Device={id:string,name:string}
type Result={handoff_id:string,archive_hash:string}

export function HandoffPanel({sessionId,sourceDeviceId,devices,csrf}:{sessionId:string,sourceDeviceId:string|null,devices:Device[],csrf:string}){
  const targets=devices.filter(d=>d.id!==sourceDeviceId)
  const [target,setTarget]=useState('')
  const [result,setResult]=useState<Result|null>(null)
  const [error,setError]=useState('')
  async function create(){setError('');setResult(null)
    const response=await fetch('/v1/handoffs',{method:'POST',headers:{'Content-Type':'application/json','x-awb-csrf':csrf},
      credentials:'same-origin',body:JSON.stringify({source_session_id:sessionId,target_device_id:target})})
    if(!response.ok){setError('无法生成交接包：请确认已保存交接所需的正文，且目标设备不同。未采集的历史不能写入交接包。');return}
    setResult(await response.json())
  }
  if(!targets.length)return null
  return <div className="handoffPanel"><h3>上下文交接</h3><p>冻结已保存的历史，手动在目标 Agent 创建新会话。</p>
    <select aria-label="目标设备" value={target} onChange={e=>setTarget(e.target.value)}><option value="">选择目标设备</option>{targets.map(d=><option key={d.id} value={d.id}>{d.name}</option>)}</select>
    <button disabled={!target} onClick={()=>void create()}>生成交接包</button>
    {error&&<small className="handoffError" role="alert">{error}</small>}
    {result&&<><a href={`/v1/handoffs/${result.handoff_id}/download`}>下载 ZIP</a><small>包已冻结；下载不代表任务完成。</small></>}
  </div>
}
