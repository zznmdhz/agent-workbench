import './overview.css'

export type FilterValues = {
  date: string
  rangeDays: number
  tz: string
  deviceId: string
  agent: string
  model: string
}

type Props = FilterValues & {
  devices: {id:string,name:string}[]
  models: string[]
  onDate:(value:string)=>void
  onRange:(value:number)=>void
  onTz:(value:string)=>void
  onDevice:(value:string)=>void
  onAgent:(value:string)=>void
  onModel:(value:string)=>void
  label?: string
}

export function FilterBar(p:Props){
  return <section className="dashboardFilters" aria-label={p.label||'活动筛选'}>
    <div className="rangeButtons" role="group" aria-label="日期范围">
      {[1,7,10,15].map(n=><button type="button" key={n} aria-pressed={p.rangeDays===n} className={p.rangeDays===n?'active':''} onClick={()=>p.onRange(n)}>{n===1?'单日':`近 ${n} 天`}</button>)}
    </div>
    <label>结束日期 <input type="date" value={p.date} onChange={e=>p.onDate(e.target.value)}/></label>
    <label>设备 <select value={p.deviceId} onChange={e=>p.onDevice(e.target.value)}><option value="">全部设备</option>{p.devices.map(d=><option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
    <label>Agent <select value={p.agent} onChange={e=>p.onAgent(e.target.value)}><option value="">全部 Agent</option><option value="codex">Codex</option><option value="hermes">Hermes</option></select></label>
    <label>模型 <select value={p.model} onChange={e=>p.onModel(e.target.value)}><option value="">全部模型</option>{p.models.map(m=><option key={m} value={m}>{m}</option>)}</select></label>
    <label>报告时区 <select value={p.tz} onChange={e=>p.onTz(e.target.value)}><option value="Asia/Hong_Kong">香港时间</option><option value="UTC">UTC</option></select></label>
  </section>
}
