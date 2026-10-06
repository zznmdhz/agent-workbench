import {useEffect,useRef,useState} from 'react'

type State={version:string,pid:number,collector_state:string,last_success:string|null,last_attempt:string|null,
  error:string|null,paused:boolean,tray_visible:boolean,autostart:boolean,scan_interval_seconds:number,
  scan_count:number,build:{commit:string,built_at:string|null}}

export function statusIcon(color:'green'|'amber'|'red'){
  const fill={green:'#208879',amber:'#e39a30',red:'#cb4242'}[color]
  // A disconnected server cannot serve a red SVG; keep it in the loaded page.
  return 'data:image/svg+xml,'+encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="${fill}"/><path d="M16 6v20M6 16h20M9 9l14 14M9 23L23 9" stroke="white" stroke-width="2"/></svg>`)
}

export default function BackgroundStatus({csrf,onRefresh,onReconnect}:{csrf:string,onRefresh:()=>void,onReconnect:()=>void}){
  const [state,setState]=useState<State|null>(null)
  const [online,setOnline]=useState(true)
  const [message,setMessage]=useState('')
  const lastCount=useRef(-1)
  const lastPid=useRef(0)
  const lastVersion=useRef('')
  useEffect(()=>{
    let cancelled=false
    async function poll(){
      try{
        const response=await fetch('/v1/local/background',{signal:AbortSignal.timeout(4000)})
        if(!response.ok)throw new Error('status unavailable')
        const next=await response.json() as State
        if(cancelled)return
        if(lastVersion.current&&lastVersion.current!==next.version){window.location.reload();return}
        lastVersion.current=next.version
        setState(next);setOnline(true)
        if(lastPid.current&&lastPid.current!==next.pid)onReconnect()
        if(lastCount.current>=0&&lastCount.current!==next.scan_count)onRefresh()
        lastCount.current=next.scan_count;lastPid.current=next.pid
      }catch{if(!cancelled)setOnline(false)}
    }
    void poll();const timer=window.setInterval(()=>void poll(),5000)
    return()=>{cancelled=true;window.clearInterval(timer)}
  },[]) // Callbacks only request fresh data; polling lifetime is the page lifetime.
  const stale=state?.last_success&&Date.now()-Date.parse(state.last_success)>Math.max(300,state.scan_interval_seconds*3)*1000
  const color=!online?'red':stale||!state?.last_success||['error','warning','paused','starting','scanning'].includes(state?.collector_state||'starting')?'amber':'green'
  const label=!online?'后台未连接':!state?'正在确认后台状态':state.paused?'后台运行 · 采集已暂停':
    state.collector_state==='scanning'?'后台运行 · 正在采集':state.error?'后台运行 · 采集异常':stale?'后台运行 · 数据更新延迟':'后台运行中'
  useEffect(()=>{
    const icon=document.querySelector<HTMLLinkElement>('link[rel="icon"]')
    if(icon)icon.href=statusIcon(color)
    document.title=`${online?'●':'○'} ${label} · Agent Workbench`
  },[color,label,online])
  async function action(name:string){
    setMessage('')
    try{
      const response=await fetch(`/v1/local/background/${name}`,{method:'POST',headers:{'x-awb-csrf':csrf}})
      if(!response.ok)throw new Error(await response.text())
      setMessage(name==='restart'?'正在重启，页面将自动重新连接':'操作已提交')
      if(name==='pause'||name==='resume')setState(await response.json())
    }catch(e){setMessage(`操作失败：${String(e).slice(0,100)}`)}
  }
  return <div className={`backgroundStatus ${color}`} role="status">
    <span className="backgroundDot" aria-hidden="true"/><strong>{label}</strong>
    {state?.last_success&&<small>最近采集 {new Date(state.last_success).toLocaleString('zh-CN',{hour12:false})}</small>}
    <details className="backgroundMenu"><summary>后台管理</summary><div>
      <p>关闭浏览器后，后台仍会继续采集。</p>
      <p>登录自动启动：{state?.autostart?'已开启':'未注册'} · 通知区图标：{state?.tray_visible?'已显示':'未显示'}</p>
      {state?.build&&<p>v{state.version} · 构建 {state.build.commit.slice(0,8)}</p>}
      {state?.error&&<p>{state.error}</p>}
      <button onClick={()=>void action(state?.paused?'resume':'pause')} disabled={!online}>{state?.paused?'恢复采集':'暂停采集'}</button>
      <button onClick={()=>void action('restart')} disabled={!online}>重启后台</button>
      {message&&<p>{message}</p>}
      {!online&&<p>请从开始菜单打开 Agent Workbench。登录时后台会自动启动；本页会自动重连。</p>}
    </div></details>
  </div>
}
