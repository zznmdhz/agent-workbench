import {useEffect, useRef, useState, type FormEvent} from 'react'

type Source={id:string,agent:string,profile:string,execution_surface:string,capability_json:string}
type Scope={source_ids:string[],start_at?:string,end_at?:string,cwd_prefix?:string}
type Preview={scope:Record<string,unknown>,items:{source_id:string,agent:string,units:number,eligible_messages:number}[],eligible_messages:number,warning:string}
type StatusItem={source_id:string,agent:string,requested_at:string,status:string,units_total:number,units_done:number,facts_queued:number,finished_at:string|null,error:string|null,scope_json:string}

function eligible(source:Source):boolean{
  if(source.execution_surface!=='local')return false
  try{return JSON.parse(source.capability_json||'{}').content_policy==='full_content'}catch{return false}
}
async function call<T>(path:string,method:'GET'|'POST'='GET',body?:unknown,csrf?:string):Promise<T>{
  const response=await fetch(path,{method,credentials:'same-origin',headers:{'Content-Type':'application/json',...(csrf?{'x-awb-csrf':csrf}:{})},body:body?JSON.stringify(body):undefined})
  if(!response.ok)throw new Error(`${response.status}: ${(await response.text()).slice(0,180)}`)
  return response.json()
}
function count(value:unknown):string{return typeof value==='number'?new Intl.NumberFormat('zh-CN').format(value):'待估算'}

export function BackfillPanel({sources,csrf,onChanged}:{sources:Source[],csrf:string,onChanged:()=>void}){
  const candidates=sources.filter(eligible)
  const candidateKey=candidates.map(source=>source.id).sort().join('|')
  const [chosen,setChosen]=useState<string[]>([])
  const [from,setFrom]=useState('')
  const [through,setThrough]=useState('')
  const [cwdPrefix,setCwdPrefix]=useState('')
  const [preview,setPreview]=useState<Preview|null>(null)
  const [status,setStatus]=useState<StatusItem[]>([])
  const [busy,setBusy]=useState(false)
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  const seenStatuses=useRef<string|null>(null)
  const onChangedRef=useRef(onChanged)
  useEffect(()=>{onChangedRef.current=onChanged},[onChanged])
  useEffect(()=>{setChosen(old=>old.filter(id=>candidateKey.split('|').includes(id)));setPreview(null)},[candidateKey])

  useEffect(()=>{
    let active=true
    const refresh=()=>call<{items:StatusItem[]}>('/v1/local/content-backfills').then(value=>{
      if(!active)return
      const items=value.items||[]
      const signature=JSON.stringify(items.map(item=>[item.source_id,item.status,item.units_done,item.units_total,item.facts_queued]))
      if(seenStatuses.current!==null&&signature!==seenStatuses.current&&items.some(item=>['completed','complete','done','failed'].includes(item.status)))onChangedRef.current()
      seenStatuses.current=signature
      setStatus(items)
    }).catch(()=>{})
    void refresh()
    const timer=window.setInterval(()=>void refresh(),5000)
    return()=>{active=false;window.clearInterval(timer)}
  },[])

  const scope=():Scope=>{
    const result:Scope={source_ids:chosen}
    if(from)result.start_at=new Date(`${from}T00:00:00+08:00`).toISOString()
    if(through)result.end_at=new Date(`${through}T00:00:00+08:00`).toISOString()
    if(cwdPrefix.trim())result.cwd_prefix=cwdPrefix.trim()
    return result
  }
  const changeScope=()=>{setPreview(null);setNotice('')}
  const toggle=(id:string)=>{setChosen(old=>old.includes(id)?old.filter(x=>x!==id):[...old,id]);changeScope()}

  async function showPreview(event:FormEvent){
    event.preventDefault()
    setError('');setNotice('')
    if(!chosen.length){setError('请先选择至少一个允许保存正文的本机来源。');return}
    if(from&&through&&from>=through){setError('结束日期必须晚于开始日期。');return}
    setBusy(true)
    try{setPreview(await call<Preview>('/v1/local/content-backfills/preview','POST',scope(),csrf))}
    catch(reason){setError(`预览失败：${String(reason)}`)}
    finally{setBusy(false)}
  }

  async function start(){
    if(!preview)return
    setError('');setBusy(true)
    try{
      await call('/v1/local/content-backfills','POST',scope(),csrf)
      setNotice('补采已排队。工作台会只读检查所选原生日志；完成后请刷新会话查看覆盖变化。')
      setPreview(null)
      const value=await call<{items:StatusItem[]}>('/v1/local/content-backfills')
      setStatus(value.items||[])
      onChanged()
    }catch(reason){setError(`补采启动失败：${String(reason)}`)}
    finally{setBusy(false)}
  }

  return <section className="panel backfillPanel" aria-label="历史正文补采">
    <h2>历史正文补采</h2>
    <p className="muted">现在开启“保存脱敏后的正文”只影响后续记录。这里可以从仍在本机的原生日志只读补采指定来源和范围；原日志没有保存或已删除的内容无法恢复。正文可能含私人信息，请先预览再启动。</p>
    <form onSubmit={showPreview}>
      <fieldset><legend>选择本机来源</legend>{candidates.length?candidates.map(source=><label key={source.id} className="backfillCheck"><input type="checkbox" checked={chosen.includes(source.id)} onChange={()=>toggle(source.id)}/>{source.agent} · {source.profile}</label>):<p className="muted">没有可补采来源。先在上方把本机来源改为“保存脱敏后的正文”。</p>}</fieldset>
      <div className="backfillFields"><label>从此日期起（香港时间）<input type="date" value={from} onChange={event=>{setFrom(event.target.value);changeScope()}}/></label><label>到此日期前（香港时间）<input type="date" value={through} onChange={event=>{setThrough(event.target.value);changeScope()}}/></label><label>可选：工作目录前缀<input value={cwdPrefix} onChange={event=>{setCwdPrefix(event.target.value);changeScope()}} placeholder="留空表示所选来源内全部目录"/></label></div>
      <p className="muted">日期留空表示所选来源内所有仍保留的历史。日期范围按开始包含、结束不包含计算；例如补采 9 月 26 日，结束日期填 9 月 27 日。</p>
      <button type="submit" disabled={busy||!candidates.length}>先预览可恢复范围</button>
    </form>
    {preview&&<div className="backfillPreview" role="status"><h3>补采预览</h3><p>在所选范围内找到 {count(preview.eligible_messages)} 条可能恢复正文的消息，来自 {preview.items.reduce((sum,item)=>sum+item.units,0)} 个原生记录单元。</p>{preview.items.map(item=><p key={item.source_id}>{item.agent}：{count(item.eligible_messages)} 条消息 / {count(item.units)} 个记录单元</p>)}<p>{preview.warning||'仅从现存原生日志恢复；原日志未保存、已删除或已排除的内容无法补造。'}</p><button onClick={()=>void start()} disabled={busy}>确认补采这一范围</button><button className="secondary" onClick={()=>setPreview(null)}>取消</button></div>}
    {error&&<p className="inlineError" role="alert">{error}</p>}{notice&&<p className="notice" role="status">{notice}</p>}
    {status.length>0&&<div className="backfillJobs"><h3>补采状态</h3>{status.map(item=><div key={item.source_id}><b>{item.agent} · {item.status}</b><small>已检查 {count(item.units_done)} / {count(item.units_total)} 个记录单元；已排队事实 {count(item.facts_queued)} 条{item.finished_at?` · 完成 ${new Date(item.finished_at).toLocaleString('zh-CN')}`:''}{item.error?` · 错误：${item.error}`:''}</small></div>)}</div>}
  </section>
}
