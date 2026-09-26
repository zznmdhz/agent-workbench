export type SessionIdentity = {
  id: string
  title?: string | null
  display_title?: string | null
  preview?: string | null
  agent: string
  last_activity?: string | null
  cwd?: string | null
}

export function sessionLabel(session: SessionIdentity): string {
  const supplied = session.display_title?.trim() || session.title?.trim() || session.preview?.trim()
  if (supplied) return supplied.replace(/\s+/g, ' ').slice(0, 90)
  const agent = session.agent === 'codex' ? 'Codex' : session.agent === 'hermes' ? 'Hermes' : session.agent
  const day = session.last_activity?.slice(0, 10) || '日期未知'
  return `${agent} · ${day} · ${session.id.slice(0, 8)}`
}

export function workingDirectoryName(session: SessionIdentity): string | null {
  return session.cwd?.split(/[\\/]/).filter(Boolean).at(-1) || null
}
