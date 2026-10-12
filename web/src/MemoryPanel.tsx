import {useCallback,useEffect,useState} from 'react'
import './memory.css'

type Evidence={source_id:string,quote:string}
type Memory={id:string,text:string,kind:string,scope:string,state:string,heads:string[],evidence:Evidence[],updated_at?:string}
type Source={id:string,agent:string,device_id:string,role:string,body:string,occurred_at:string|null,scope:string}
type Status={available:boolean,configured:boolean,space_id:string|null,device_id:string,counts:Record<string,number>,last_import?:string|null,last_export?:string|null,jobs?:Record<string,number>,worker?:Record<string,unknown>,reason?:string}
type Route={enabled:boolean,endpoint:string,model:string,has_key?:boolean,daily_call_limit?:number,dimensions?:number,threshold?:number}
type Settings={enabled:boolean,model_daily_call_limit:number,sync_root:string|null,llm:Route,embedding:Route}
const labels:Record<string,string>={candidate:'待确认',active:'已确认',disabled:'已停用',deleted:'已删除',conflict:'版本冲突',pending:'等待父版本',rejected:'已拒绝'}
const kinds:Record<string,string>={fact:'事实',preference:'偏好',constraint:'约束',decision:'决定',task:'任务'}
async function api<T>(path:string,csrf:string,body?:object):Promise<T>{
  const response=await fetch(`/v1/memory/${path}`,{method:body?'POST':'GET',credentials:'same-origin',headers:body?{'Content-Type':'application/json','x-awb-csrf':csrf}:{},body:body?JSON.stringify(body):undefined})
  if(!response.ok){let detail='请求未完成';try{const data=await response.json();detail=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail)}catch{detail=String(response.status)}throw new Error(detail)}
  return response.json()
}
const time=(value:unknown)=>typeof value==='string'?new Date(value).toLocaleString('zh-CN'):'暂无记录'

export default function MemoryPanel({csrf}:{csrf:string}){
  const [status,setStatus]=useState<Status|null>(null)
  const [settings,setSettings]=useState<Settings|null>(null)
  const [items,setItems]=useState<Memory[]>([])
  const [total,setTotal]=useState(0)
  const [offset,setOffset]=useState(0)
  const [query,setQuery]=useState('')
  const [scope,setScope]=useState('')
  const [state,setState]=useState('')
  const [selected,setSelected]=useState<Memory|null>(null)
  const [sources,setSources]=useState<Source[]>([])
  const [history,setHistory]=useState<unknown[]>([])
  const [editor,setEditor]=useState('')
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  const [busy,setBusy]=useState(false)
  const [join,setJoin]=useState('')
  const [credential,setCredential]=useState('')
  const [syncRoot,setSyncRoot]=useState('')
  const [manual,setManual]=useState('')
  const [manualScope,setManualScope]=useState('personal')
  const [kind,setKind]=useState('fact')
  const [previewQuery,setPreviewQuery]=useState('')
  const [preview,setPreview]=useState<unknown>(null)
  const [config,setConfig]=useState<unknown>(null)
  const [modelKey,setModelKey]=useState('')
  const [embedKey,setEmbedKey]=useState('')
  const [advanced,setAdvanced]=useState(false)
  const [usage,setUsage]=useState<unknown>(null)

  const load=useCallback(async()=>{
    const current=await api<Status>('status',csrf);setStatus(current)
    if(current.available){const result=await api<{items:Memory[],total:number}>(`items?query=${encodeURIComponent(query)}&state=${encodeURIComponent(state)}&scope=${encodeURIComponent(scope)}&limit=30&offset=${offset}`,csrf);setItems(result.items);setTotal(result.total)}
  },[csrf,query,state,scope,offset])
  useEffect(()=>{void load().catch(e=>setError(String(e)))},[load])
  useEffect(()=>{void api<Settings>('settings',csrf).then(value=>{setSettings(value);setSyncRoot(value.sync_root||'')}).catch(e=>setError(String(e)));const timer=window.setInterval(()=>{void api<Status>('status',csrf).then(setStatus).catch(()=>{})},15000);return()=>window.clearInterval(timer)},[csrf])
  async function run(action:()=>Promise<void>){setBusy(true);setError('');setNotice('');try{await action();await load()}catch(e){setError(e instanceof Error?e.message:String(e))}finally{setBusy(false)}}
  async function open(item:Memory){await run(async()=>{const detail=await api<{memory:Memory,sources:Source[],operations:unknown[]}>(`items/${encodeURIComponent(item.id)}`,csrf);setSelected(detail.memory);setEditor(detail.memory.text);setSources(detail.sources||[]);setHistory(detail.operations||[])})}
  async function change(action:string){if(!selected)return;if(action==='delete'&&!window.confirm('删除后，这条记忆及相关证据将停止参与记忆召回，并在其他电脑收到删除事件后生效。同一条原文支持的其他记忆也可能停止召回。原会话归档、同步包和备份仍可能保留文字。继续删除？'))return;await run(async()=>{await api(`items/${encodeURIComponent(selected.id)}/change`,csrf,{action,parents:selected.heads,payload:['edit','resolve'].includes(action)?{text:editor,kind:selected.kind,state:'active'}:null});setSelected(null);setSources([]);setNotice('操作已保存；其他电脑导入后生效。')})}
  async function saveSettings(){if(!settings)return;await run(async()=>{setSettings(await api<Settings>('settings',csrf,{enabled:settings.enabled,model_daily_call_limit:settings.model_daily_call_limit||50,sync_root:syncRoot||'',llm:{enabled:settings.llm.enabled,endpoint:settings.llm.endpoint,model:settings.llm.model,daily_call_limit:settings.llm.daily_call_limit,...(modelKey?{api_key:modelKey}:{})},embedding:{enabled:settings.embedding.enabled,endpoint:settings.embedding.endpoint,model:settings.embedding.model,dimensions:settings.embedding.dimensions,threshold:settings.embedding.threshold,daily_call_limit:settings.embedding.daily_call_limit,...(embedKey?{api_key:embedKey}:{})}}));setModelKey('');setEmbedKey('');setNotice('本机设置已保存，模型密钥不会随 NAS 同步；新设置用于待处理和新消息，已处理历史不会自动重新计费。')})}
  function routeChange(name:'llm'|'embedding',key:string,value:string|number|boolean){setSettings(current=>current?{...current,[name]:{...current[name],[key]:value}}:current)}

  return <section className="mvpPanel memoryPanel" id="memory" data-dashboard-section>
    <div className="mvpPanelHead"><div><h2>共享记忆</h2><p>每台电脑本地可用，通过 NAS 同步；确认后的记忆可供多个 Agent 查询。</p></div><button disabled={busy} onClick={()=>void run(async()=>{await api('refresh',csrf,{});setNotice('已安排后台加工与同步。')})}>刷新记忆</button></div>
    {error&&<div className="memoryAlert" role="alert">{error.includes('stale')?'记忆已被更新，请重新打开详情后操作。':error}</div>}
    {notice&&<p className="memoryNotice" role="status">{notice}</p>}
    {status&&!status.available&&<p className="memoryAlert">记忆服务暂不可用（{status.reason}）；用量、时间和会话功能仍可使用。</p>}
    {status?.available&&!status.configured&&<div className="memorySetup"><h3>建立你的记忆空间</h3><p>在第一台电脑建立空间，另一台电脑使用私有加入凭据加入。凭据可以重复使用，只分享给自己的设备。</p><div className="memoryActions"><button disabled={busy} onClick={()=>void run(async()=>{await api('space',csrf,{mode:'create'});setNotice('空间已建立。设置同步目录并启用后台后开始加工。')})}>建立新空间</button></div><label>加入已有空间<textarea value={join} onChange={e=>setJoin(e.target.value)} placeholder="粘贴另一台电脑的私有加入凭据" autoComplete="off"/></label><button disabled={busy||!join.trim()} onClick={()=>void run(async()=>{await api('space',csrf,{mode:'join',join_code:join.trim()});setJoin('');setNotice('已加入。请使用两台电脑对应的同一个 NAS 同步根目录。')})}>加入空间</button></div>}
    {status?.configured&&(status as Status & {credentials_missing?:boolean}).credentials_missing&&<div className="memorySecret"><p>此副本从备份恢复，查询仍可用。重新加入同一空间后才能加工与同步。</p><textarea value={join} onChange={e=>setJoin(e.target.value)} aria-label="恢复空间加入凭据"/><button disabled={busy||!join.trim()} onClick={()=>void run(async()=>{await api('space',csrf,{mode:'join',join_code:join.trim()});setJoin('');setNotice('空间凭据已恢复。')})}>恢复空间凭据</button></div>}
    {status?.configured&&<>
      <div className="memoryStats">{[['active','已确认'],['candidate','待确认'],['conflict','冲突'],['sources','证据来源']].map(([id,label])=><div key={id}><span>{label}</span><b>{status.counts?.[id]||0}</b></div>)}</div>
      <div className="memorySync"><span>空间 {status.space_id?.slice(0,8)} · 本机 {status.device_id?.slice(0,8)}</span><span>最近导入 {time(status.last_import)} · 最近加工 {time(status.worker?.last_success)}</span><span>后台 {settings?.enabled?'已启用':'未启用'} · 待加工 {status.jobs?.pending||0} · 待补版本 {status.counts?.pending||0}</span><button disabled={busy} onClick={()=>void run(async()=>{const value=await api<{join_code:string}>('join-credential',csrf,{});setCredential(value.join_code)})}>显示加入凭据</button></div>
      {credential&&<div className="memorySecret"><p>此凭据允许加入整个空间，请私下传给自己的另一台电脑。</p><textarea readOnly value={credential} aria-label="私有加入凭据"/><button onClick={()=>void navigator.clipboard.writeText(credential).then(()=>setNotice('已复制加入凭据'))}>复制凭据</button><button onClick={()=>setCredential('')}>隐藏</button></div>}
      <div className="memoryFilters"><label>搜索<input value={query} onChange={e=>{setQuery(e.target.value);setOffset(0)}} placeholder="例如：客户版、交付要求"/></label><label>范围<input value={scope} onChange={e=>{setScope(e.target.value);setOffset(0)}} placeholder="全部范围"/></label><label>状态<select value={state} onChange={e=>{setState(e.target.value);setOffset(0)}}><option value="">全部</option>{Object.entries(labels).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label></div>
      <div className="memoryWorkspace"><div className="memoryList">{items.map(item=><button key={item.id} className={selected?.id===item.id?'selected':''} onClick={()=>void open(item)}><div><span className={`memoryBadge ${item.state}`}>{labels[item.state]||item.state}</span><small>{kinds[item.kind]||item.kind} · {item.scope}</small></div><p>{item.text||'记忆内容已隐藏'}</p></button>)}{!items.length&&<p className="mvpEmpty">暂无匹配记忆。启用后台后会产生待确认候选，也可以手动添加。</p>}<div className="memoryActions"><button disabled={!offset||busy} onClick={()=>setOffset(Math.max(0,offset-30))}>上一页</button><span>{Math.min(offset+1,total)}–{Math.min(offset+30,total)} / {total}</span><button disabled={offset+30>=total||busy} onClick={()=>setOffset(offset+30)}>下一页</button></div></div>
      <div className="memoryDetail">{selected?<><div className="memoryDetailTitle"><h3>{labels[selected.state]} · {kinds[selected.kind]}</h3><small>{selected.scope}</small></div><textarea aria-label="记忆内容" value={editor} onChange={e=>setEditor(e.target.value)} disabled={selected.state==='deleted'}/><div className="memoryActions">{selected.state==='candidate'&&<><button disabled={busy} onClick={()=>void change('confirm')}>确认记忆</button><button disabled={busy} onClick={()=>void change('reject')}>拒绝候选</button></>}{['active','candidate'].includes(selected.state)&&<><button disabled={busy||!editor.trim()} onClick={()=>void change('edit')}>保存更正</button><button disabled={busy} onClick={()=>void change('disable')}>停用</button></>}{selected.state==='disabled'&&<button disabled={busy} onClick={()=>void change('enable')}>恢复</button>}{selected.state==='conflict'&&<button disabled={busy||!editor.trim()} onClick={()=>void change('resolve')}>确认此内容并解决已见冲突</button>}{selected.state!=='deleted'&&<button className="memoryDanger" disabled={busy} onClick={()=>void change('delete')}>删除记忆</button>}</div><h4>原文证据</h4>{selected.evidence?.map((e,index)=><blockquote key={`${e.source_id}-${index}`}>{e.quote}<small>来源 {e.source_id.slice(0,12)}</small></blockquote>)}{sources.map(source=><details key={source.id}><summary>{source.agent} · {source.role} · {time(source.occurred_at)}</summary><pre>{source.body}</pre></details>)}<details><summary>版本记录（{history.length}）</summary><pre>{JSON.stringify(history,null,2)}</pre></details></>:<p className="mvpEmpty">选择一条记忆，查看证据和版本。</p>}</div></div>
      <details className="memoryAdd"><summary>手动添加记忆</summary><label>内容<textarea value={manual} onChange={e=>setManual(e.target.value)} maxLength={4000}/></label><div className="memoryFilters"><label>类型<select value={kind} onChange={e=>setKind(e.target.value)}>{Object.entries(kinds).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label><label>范围<input value={manualScope} onChange={e=>setManualScope(e.target.value)} maxLength={120}/></label><button disabled={busy||!manual.trim()||!manualScope.trim()} onClick={()=>void run(async()=>{await api('propose',csrf,{text:manual,kind,scope:manualScope});setManual('');setNotice('已添加待确认候选，请查看证据后确认。')})}>添加候选</button></div></details>
      <details className="memoryRecall"><summary>预览 Agent 的记忆查询</summary><div className="memoryFilters"><input aria-label="召回问题" value={previewQuery} onChange={e=>setPreviewQuery(e.target.value)} placeholder="客户版文档有哪些交付要求？"/><button disabled={busy||!previewQuery.trim()} onClick={()=>void run(async()=>{setPreview(await api('recall',csrf,{query:previewQuery,scope:manualScope,limit:6}))})}>查询</button><button disabled={busy} onClick={()=>void run(async()=>{setConfig(await api(`agent-config?scope=${encodeURIComponent(manualScope)}`,csrf))})}>获取 MCP 配置</button></div><p>Agent 只读配置允许的范围；候选、冲突和停用项不会作为已确认事实使用。</p>{preview!==null&&<pre>{JSON.stringify(preview,null,2)}</pre>}{config!==null&&<pre>{JSON.stringify(config,null,2)}</pre>}</details>
    </>}
    {settings&&<details className="memorySettings" open={advanced} onToggle={e=>setAdvanced(e.currentTarget.open)}><summary>记忆后台与模型设置</summary><label className="memoryCheckbox"><input type="checkbox" checked={settings.enabled} onChange={e=>setSettings({...settings,enabled:e.target.checked})} disabled={!status?.configured}/>启用本机记忆加工与同步</label><label>本机每日辅助调用总上限<input type="number" min={1} max={10000} value={settings.model_daily_call_limit||50} onChange={e=>setSettings({...settings,model_daily_call_limit:Number(e.target.value)})}/></label><label>NAS 同步根目录<input value={syncRoot} onChange={e=>setSyncRoot(e.target.value)} placeholder="选用现有看板对应的同步目录"/></label><p>各电脑填写同一 NAS 文件夹对应的本地路径。共享的是事件包，每台电脑保留独立数据库。</p>
      {(['llm','embedding'] as const).map(name=><fieldset key={name}><legend>{name==='llm'?'模型提炼（可选）':'语义检索（可选）'}</legend><label className="memoryCheckbox"><input type="checkbox" checked={settings[name]?.enabled||false} onChange={e=>routeChange(name,'enabled',e.target.checked)}/>启用{name==='llm'?'模型提炼':'向量服务'}</label><p>启用后，相关对话片段或查询会发送给所配置的服务。不启用仍可使用规则候选和关键词检索。</p><div className="memoryFilters"><label>接口地址<input value={settings[name]?.endpoint||''} onChange={e=>routeChange(name,'endpoint',e.target.value)} placeholder="https://服务地址/v1"/></label><label>模型<input value={settings[name]?.model||''} onChange={e=>routeChange(name,'model',e.target.value)}/></label><label>密钥<input type="password" autoComplete="new-password" value={name==='llm'?modelKey:embedKey} onChange={e=>name==='llm'?setModelKey(e.target.value):setEmbedKey(e.target.value)} placeholder={settings[name]?.has_key?'已保存；留空保持':'仅保存于本机'}/></label><label>每日最多调用<input type="number" min={1} max={10000} value={settings[name]?.daily_call_limit||50} onChange={e=>routeChange(name,'daily_call_limit',Number(e.target.value))}/></label>{name==='embedding'&&<><label>向量维度<input type="number" min={1} max={8192} value={settings.embedding.dimensions||1024} onChange={e=>routeChange(name,'dimensions',Number(e.target.value))}/></label><label>相似度阈值<input type="number" min={0} max={1} step={0.01} value={settings.embedding.threshold??0.7} onChange={e=>routeChange(name,'threshold',Number(e.target.value))}/></label></>}</div></fieldset>)}
      <div className="memoryActions"><button disabled={busy} onClick={()=>void saveSettings()}>保存本机设置</button><button disabled={busy} onClick={()=>void run(async()=>{const result=await api<{retried:number}>('retry-failed',csrf,{});setNotice(`已重新安排 ${result.retried} 项失败任务，调用预算保持不变。`)})}>重试失败任务</button><button onClick={()=>void run(async()=>setUsage(await api('model-usage',csrf)))}>查看辅助模型用量</button></div><p>总上限覆盖提炼和向量请求，每个接口的上限同时生效；按本机计算，不是费用上限。记忆辅助调用独立记录，避免重复计入原看板。</p>{usage!==null&&<pre>{JSON.stringify(usage,null,2)}</pre>}
    </details>}
  </section>
}
