import { type FormEvent, type ReactNode, useEffect, useMemo, useState } from 'react'
import { Link, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { adminAction, adminLogin, getAdminState, joinGame, adminWsUrl, playerWsUrl } from './api'
import type { AdminState, GameEvent, Player, PlayerGameState } from './types'

const PLAYER_STORAGE_KEY = 'arena_player_session'
const ADMIN_STORAGE_KEY = 'arena_admin_token'

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link to="/" className="brand">THE ARENA</Link>
        <nav>
          <Link to="/admin">ADMIN</Link>
        </nav>
      </header>
      <main>{children}</main>
    </div>
  )
}

function LandingPage() {
  return (
    <section className="hero panel">
      <p className="eyebrow">COLLEGE EVENT • SINGLE ARENA</p>
      <h1>THE ARENA</h1>
      <p className="hero-copy">A live, text-first survival game. Enter your name, join the lobby, and wait for the arena to open.</p>
      <div className="button-row">
        <Link to="/join" className="button button-primary">ENTER THE ARENA</Link>
        <Link to="/admin" className="button button-secondary">ADMIN CONTROL</Link>
      </div>
    </section>
  )
}

function JoinPage() {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError('')
    setLoading(true)
    try {
      const result = await joinGame(name)
      localStorage.setItem(PLAYER_STORAGE_KEY, JSON.stringify(result))
      navigate('/lobby')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to join')
    } finally {
      setLoading(false)
    }
  }

  return (
    <section className="narrow panel">
      <p className="eyebrow">PLAYER REGISTRATION</p>
      <h2>ENTER YOUR NAME</h2>
      <p className="muted">Choose the name you want displayed during the event.</p>
      <form onSubmit={handleSubmit} className="stack">
        <input autoFocus maxLength={32} value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" />
        <button disabled={loading || !name.trim()} className="button button-primary" type="submit">
          {loading ? 'JOINING…' : 'JOIN GAME'}
        </button>
      </form>
      {error && <div className="error">{error}</div>}
    </section>
  )
}

function EventFeed({ events }: { events: GameEvent[] }) {
  return (
    <div className="event-feed">
      <div className="section-heading">LIVE FEED</div>
      {events.length === 0 ? (
        <p className="muted">No events yet.</p>
      ) : (
        events.slice().reverse().map((event) => (
          <div key={event.id} className="event-item">
            <span className="event-time">{new Date(event.timestamp).toLocaleTimeString()}</span>
            <span>{event.message}</span>
          </div>
        ))
      )}
    </div>
  )
}

function PlayerLobby() {
  const navigate = useNavigate()
  const [state, setState] = useState<PlayerGameState | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw) {
      navigate('/join', { replace: true })
      return
    }
    const session = JSON.parse(raw) as { player: Player; sessionToken: string }
    const ws = new WebSocket(playerWsUrl(session.player.id, session.sessionToken))
    ws.onmessage = (message) => {
      const payload = JSON.parse(message.data) as { type: string; data: PlayerGameState }
      if (payload.type === 'GAME_STATE') {
        setState(payload.data)
        if (payload.data.status === 'ACTIVE') navigate('/game', { replace: true })
      }
    }
    ws.onerror = () => setError('Connection to the arena was lost. Refresh to reconnect.')
    return () => ws.close()
  }, [navigate])

  if (!state) return <LoadingState text={error || 'Connecting to the arena…'} />

  return (
    <section className="panel stack">
      <div>
        <p className="eyebrow">LOBBY</p>
        <h2>WELCOME, {state.player.name.toUpperCase()}</h2>
        <p className="muted">Your player ID is <strong>{state.player.id}</strong>.</p>
      </div>
      <div className="stat-grid">
        <Stat label="REGISTERED" value={`${state.playerCount}/${state.maxPlayers}`} />
        <Stat label="STATUS" value="WAITING" />
      </div>
      <EventFeed events={state.events} />
    </section>
  )
}

function PlayerGame() {
  const navigate = useNavigate()
  const [state, setState] = useState<PlayerGameState | null>(null)

  useEffect(() => {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw) {
      navigate('/join', { replace: true })
      return
    }
    const session = JSON.parse(raw) as { player: Player; sessionToken: string }
    const ws = new WebSocket(playerWsUrl(session.player.id, session.sessionToken))
    ws.onmessage = (message) => {
      const payload = JSON.parse(message.data) as { type: string; data: PlayerGameState }
      if (payload.type === 'GAME_STATE') setState(payload.data)
    }
    return () => ws.close()
  }, [navigate])

  if (!state) return <LoadingState text="Entering the arena…" />

  return (
    <section className="panel stack">
      <div className="game-header">
        <div>
          <p className="eyebrow">ACTIVE GAME</p>
          <h2>THE ARENA</h2>
        </div>
        <span className="status-pill">{state.status}</span>
      </div>
      <div className="stat-grid">
        <Stat label="PLAYER" value={state.player.name} />
        <Stat label="ID" value={state.player.id} />
        <Stat label="ALIVE" value={String(state.aliveCount)} />
      </div>
      <div className="placeholder-gameplay">
        <p className="eyebrow">PHASE 1</p>
        <h3>THE GAME SYSTEM IS READY</h3>
        <p>Movement, timed actions, combat, items, and zone mechanics are intentionally reserved for Phase 2.</p>
      </div>
      <EventFeed events={state.events} />
    </section>
  )
}

function AdminPage() {
  const [authenticated, setAuthenticated] = useState(Boolean(localStorage.getItem(ADMIN_STORAGE_KEY)))
  const [tokenInput, setTokenInput] = useState('')
  const [state, setState] = useState<AdminState | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function login(event: FormEvent) {
    event.preventDefault()
    setError('')
    setLoading(true)
    try {
      await adminLogin(tokenInput)
      localStorage.setItem(ADMIN_STORAGE_KEY, tokenInput)
      setAuthenticated(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!authenticated) return
    const token = localStorage.getItem(ADMIN_STORAGE_KEY)
    if (!token) return
    getAdminState(token).then(setState).catch(() => {
      localStorage.removeItem(ADMIN_STORAGE_KEY)
      setAuthenticated(false)
    })
    const ws = new WebSocket(adminWsUrl(token))
    ws.onmessage = (message) => {
      const payload = JSON.parse(message.data) as { type: string; data: AdminState }
      if (payload.type === 'ADMIN_STATE') setState(payload.data)
    }
    ws.onerror = () => setError('Admin live connection failed. Refresh to reconnect.')
    return () => ws.close()
  }, [authenticated])

  if (!authenticated) {
    return (
      <section className="narrow panel">
        <p className="eyebrow">ARENA CONTROL</p>
        <h2>ADMIN LOGIN</h2>
        <p className="muted">Use the admin token configured on the FastAPI server.</p>
        <form onSubmit={login} className="stack">
          <input type="password" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} placeholder="Admin token" />
          <button disabled={loading || !tokenInput} className="button button-primary" type="submit">{loading ? 'CHECKING…' : 'UNLOCK DASHBOARD'}</button>
        </form>
        {error && <div className="error">{error}</div>}
      </section>
    )
  }

  async function doAction(action: string) {
    const token = localStorage.getItem(ADMIN_STORAGE_KEY)
    if (!token) return
    setError('')
    try {
      const next = await adminAction(token, action)
      setState(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed')
    }
  }

  function logout() {
    localStorage.removeItem(ADMIN_STORAGE_KEY)
    setAuthenticated(false)
    setState(null)
  }

  if (!state) return <LoadingState text="Loading arena control…" />

  return (
    <section className="admin-layout">
      <div className="panel stack">
        <div className="game-header">
          <div>
            <p className="eyebrow">ARENA CONTROL</p>
            <h2>MAIN GAME</h2>
          </div>
          <button className="button button-secondary button-small" onClick={logout}>LOG OUT</button>
        </div>
        <div className="stat-grid">
          <Stat label="STATUS" value={state.status} />
          <Stat label="ALIVE" value={String(state.aliveCount)} />
          <Stat label="REGISTERED" value={`${state.playerCount}/${state.maxPlayers}`} />
        </div>
        <div className="button-row wrap">
          {state.status === 'LOBBY' && <button className="button button-primary" onClick={() => doAction('START_GAME')}>START GAME</button>}
          {state.status === 'ACTIVE' && <button className="button button-secondary" onClick={() => doAction('PAUSE_GAME')}>PAUSE</button>}
          {state.status === 'PAUSED' && <button className="button button-primary" onClick={() => doAction('RESUME_GAME')}>RESUME</button>}
          <button className="button button-danger" onClick={() => { if (window.confirm('Reset the entire arena? All registered players will be removed.')) void doAction('RESET_GAME') }}>RESET</button>
        </div>
        {error && <div className="error">{error}</div>}
      </div>

      <div className="panel">
        <div className="section-heading">PLAYERS</div>
        <div className="player-table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Connection</th></tr></thead>
            <tbody>
              {state.players.map((player) => (
                <tr key={player.id}>
                  <td>{player.id}</td>
                  <td>{player.name}</td>
                  <td><span className={player.alive ? 'tag alive' : 'tag dead'}>{player.alive ? 'ALIVE' : 'DEAD'}</span></td>
                  <td>{player.connected ? 'ONLINE' : 'OFFLINE'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <EventFeed events={state.events} />
      </div>
    </section>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><span>{label}</span><strong>{value}</strong></div>
}

function LoadingState({ text }: { text: string }) {
  return <section className="narrow panel"><div className="loading-dot" /><p>{text}</p></section>
}

function App() {
  const location = useLocation()
  const key = useMemo(() => location.pathname, [location.pathname])
  return (
    <Shell>
      <Routes key={key}>
        <Route path="/" element={<LandingPage />} />
        <Route path="/join" element={<JoinPage />} />
        <Route path="/lobby" element={<PlayerLobby />} />
        <Route path="/game" element={<PlayerGame />} />
        <Route path="/admin" element={<AdminPage />} />
      </Routes>
    </Shell>
  )
}

export default App
