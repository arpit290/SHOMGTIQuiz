import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { adminAction, adminLogin, adminWsUrl, getAdminState, getPlayerState, getSpectateState, joinGame, playerWsUrl, spectateWsUrl, submitAction, type AdminActionOptions, type Gender, type StatChoice, type StatLevel } from './api'
import { ArenaMap } from './ArenaMap'
import type { AdminState, Battle, GameEvent, InventoryItem, Player, PlayerGameState, SpectateState, VisibleOpponent, Zone } from './types'

const fmtStat = (value: number) => (Math.round(value * 10) / 10).toString()

const PLAYER_STORAGE_KEY = 'arena_player_session'
const ADMIN_STORAGE_KEY = 'arena_admin_token'
const SPECTATE_STORAGE_KEY = 'arena_spectate_token'

type StoredSession = { player: Player; sessionToken: string }

function Emblem({ size = 28, className = '' }: { size?: number; className?: string }) {
  return (
    <svg className={`emblem ${className}`} width={size} height={size} viewBox="0 0 100 100" role="img" aria-label="The Arena emblem">
      <defs>
        <linearGradient id="emb-gold" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#f6d774" /><stop offset="1" stopColor="#b8801a" />
        </linearGradient>
        <linearGradient id="emb-fire" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0" stopColor="#c81d25" /><stop offset=".55" stopColor="#ff6a1a" /><stop offset="1" stopColor="#ffd25a" />
        </linearGradient>
      </defs>
      <circle cx="50" cy="50" r="46" fill="none" stroke="url(#emb-gold)" strokeWidth="5" />
      <circle cx="50" cy="50" r="38" fill="#120d0a" stroke="url(#emb-gold)" strokeWidth="1.5" />
      <path d="M50 14c3 15 21 22 19 43-1 15-10 25-19 27-9-2-18-12-19-27-1-11 6-17 9-26 4 5 7 3 10-17z" fill="url(#emb-fire)" />
      <path d="M50 46c2 8 11 12 10 23-1 7-5 11-10 12-5-1-9-5-10-12-1-7 5-9 6-15 3 3 4 1 4-8z" fill="#fff1b8" opacity=".85" />
      <path d="M8 62 L92 38" stroke="url(#emb-gold)" strokeWidth="2.5" strokeLinecap="round" opacity=".9" />
      <path d="M92 38 L82 36 M92 38 L85 46" stroke="url(#emb-gold)" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  )
}

function Shell({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()
  const themed = !pathname.startsWith('/admin')
  return (
    <div className={`app-shell ${themed ? 'hg' : ''}`}>
      <header className="topbar">
        <Link to="/" className="brand">{themed && <Emblem size={26} />}THE ARENA</Link>
        <nav><Link to="/">Arena</Link></nav>
      </header>
      <main>{children}</main>
    </div>
  )
}

function ExistingSessionRedirect() {
  const navigate = useNavigate()

  useEffect(() => {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw) return

    try {
      const session = JSON.parse(raw) as StoredSession
      void getPlayerState(session.player.id, session.sessionToken)
        .then((state: PlayerGameState) => {
          navigate(state.status === 'LOBBY' ? '/lobby' : '/game', { replace: true })
        })
        .catch(() => {
          localStorage.removeItem(PLAYER_STORAGE_KEY)
        })
    } catch {
      localStorage.removeItem(PLAYER_STORAGE_KEY)
    }
  }, [navigate])

  return null
}

function LandingPage() {
  return (
    <>
      <ExistingSessionRedirect />
      <section className="hero panel">
      <Emblem size={104} className="hero-emblem" />
      <p className="eyebrow">TRIBUTES OF THE COLLEGE // ONE SHARED ARENA</p>
      <h1>THE<br />ARENA</h1>
      <p className="hero-copy">Volunteer as tribute. Choose your strengths, make your moves, and be the last one standing.</p>
      <div className="button-row">
        <Link className="button button-primary" to="/join">VOLUNTEER AS TRIBUTE</Link>
      </div>
      <p className="hero-motto">MAY THE ODDS BE EVER IN YOUR FAVOR</p>
      </section>
    </>
  )
}

const STAT_ROWS: { key: keyof StatChoice; label: string; hint: string }[] = [
  { key: 'attack', label: 'ATTACK', hint: 'How hard you hit.' },
  { key: 'defense', label: 'DEFENSE', hint: 'How much of an enemy hit you shrug off.' },
  { key: 'agility', label: 'AGILITY', hint: 'Escaping fights and resisting zone hazards.' },
]
const STAT_LEVELS: { id: StatLevel; label: string; sub: string }[] = [
  { id: 'LOW', label: 'LOW', sub: '−40%' },
  { id: 'MID', label: 'MID', sub: 'BASE' },
  { id: 'HIGH', label: 'HIGH', sub: '+40%' },
]
type StatPicks = Record<keyof StatChoice, StatLevel | null>

function JoinPage() {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [step, setStep] = useState<'NAME' | 'STATS'>('NAME')
  const [picks, setPicks] = useState<StatPicks>({ attack: null, defense: null, agility: null })
  const [gender, setGender] = useState<Gender | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  function handleNameSubmit(event: FormEvent) {
    event.preventDefault()
    setError('')
    const specialPaths: Record<string, string> = {
      trinav: '/trinav',
      chewie: '/chewie',
      arpit: '/arpit',
    }
    const specialPath = specialPaths[name.trim().toLowerCase()]
    if (specialPath) {
      navigate(specialPath)
      return
    }
    setStep('STATS')
  }

  // Each level can be held by exactly one stat: picking a level someone else holds takes it over.
  function pick(stat: keyof StatChoice, level: StatLevel) {
    setPicks((current) => {
      const next = { ...current }
      const alreadyHere = current[stat] === level
      ;(Object.keys(next) as (keyof StatChoice)[]).forEach((key) => {
        if (next[key] === level) next[key] = null
      })
      next[stat] = alreadyHere ? null : level
      return next
    })
  }

  const complete = gender !== null && picks.attack !== null && picks.defense !== null && picks.agility !== null

  async function handleJoin() {
    if (!complete) return
    setError('')
    setLoading(true)
    try {
      const result = await joinGame(name, picks as StatChoice, gender as Gender)
      localStorage.setItem(PLAYER_STORAGE_KEY, JSON.stringify(result))
      navigate('/lobby')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to join')
      setStep('NAME')
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <ExistingSessionRedirect />
      <section className="narrow panel">
        {step === 'NAME' ? (
          <>
            <p className="eyebrow">THE REAPING // TRIBUTE REGISTRATION</p>
            <h2>NAME YOUR TRIBUTE</h2>
            <p className="muted">One name per participant. The reaping closes when the Gamemakers start the game.</p>
            <form onSubmit={handleNameSubmit} className="stack">
              <input autoFocus maxLength={32} value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" />
              <button disabled={!name.trim()} className="button button-primary" type="submit">CONTINUE</button>
            </form>
          </>
        ) : (
          <div className="stat-picker">
            <p className="eyebrow">STEP 2 // {name.trim().toUpperCase()}</p>
            <h2>CHOOSE YOUR STRENGTHS</h2>
            <p className="muted">Choose your gender, then give one stat <strong>HIGH</strong>, one <strong>MID</strong> and one <strong>LOW</strong>. Each district receives one M and one F.</p>
            <div className="gender-picker">
              <div className="stat-pick-label"><strong>GENDER</strong><small>Your district pair is assigned when you register.</small></div>
              <div className="gender-options">
                {(['M', 'F'] as const).map((option) => (
                  <button key={option} type="button" className={`gender-btn ${gender === option ? 'selected' : ''}`} onClick={() => setGender(option)}>
                    {option === 'M' ? 'MALE' : 'FEMALE'}
                  </button>
                ))}
              </div>
            </div>
            {STAT_ROWS.map((row) => (
              <div key={row.key} className="stat-pick-row">
                <div className="stat-pick-label"><strong>{row.label}</strong><small>{row.hint}</small></div>
                <div className="stat-pick-options">
                  {STAT_LEVELS.map((level) => (
                    <button
                      key={level.id}
                      type="button"
                      className={`stat-pick-btn ${picks[row.key] === level.id ? 'selected' : ''}`}
                      onClick={() => pick(row.key, level.id)}
                    >
                      <span>{level.label}</span><small>{level.sub}</small>
                    </button>
                  ))}
                </div>
              </div>
            ))}
            <div className="button-row stat-picker-actions">
              <button type="button" className="button button-secondary" disabled={loading} onClick={() => setStep('NAME')}>BACK</button>
              <button type="button" className="button button-primary" disabled={!complete || loading} onClick={() => void handleJoin()}>
                {loading ? 'JOINING…' : 'JOIN GAME'}
              </button>
            </div>
          </div>
        )}
        {error && <div className="error">{error}</div>}
      </section>
    </>
  )
}

function EventFeed({ events, compact = false }: { events: GameEvent[]; compact?: boolean }) {
  return (
    <div className={`event-feed ${compact ? 'compact' : ''}`}>
      <div className="section-heading">LIVE FEED</div>
      {events.length === 0 ? <p className="muted">No events yet.</p> : events.slice().reverse().map((event) => (
        <div key={event.id} className={`event-item t-${event.type}`}>
          <span className="event-time">{new Date(event.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}</span>
          <span>{event.message}</span>
        </div>
      ))}
    </div>
  )
}

function usePlayerSocket() {
  const navigate = useNavigate()
  const [state, setState] = useState<PlayerGameState | null>(null)
  const [error, setError] = useState('')
  const [connectionStatus, setConnectionStatus] = useState<'CONNECTING' | 'CONNECTED' | 'RECONNECTING'>('CONNECTING')

  useEffect(() => {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw) {
      navigate('/', { replace: true })
      return
    }
    let session: StoredSession
    try {
      session = JSON.parse(raw) as StoredSession
    } catch {
      localStorage.removeItem(PLAYER_STORAGE_KEY)
      navigate('/', { replace: true })
      return
    }

    let closedByEffect = false
    let ws: WebSocket | null = null
    let reconnectTimer: number | undefined

    const connect = () => {
      if (closedByEffect) return
      setConnectionStatus(reconnectTimer ? 'RECONNECTING' : 'CONNECTING')
      ws = new WebSocket(playerWsUrl(session.player.id, session.sessionToken))
      ws.onopen = () => { setError(''); setConnectionStatus('CONNECTED') }
      ws.onmessage = (message) => {
        const payload = JSON.parse(message.data) as { type: string; data: PlayerGameState | GameEvent }
        if (payload.type === 'RESET') {
          localStorage.removeItem(PLAYER_STORAGE_KEY)
          navigate('/join', { replace: true })
          return
        }
        if (payload.type === 'GAME_STATE') setState(payload.data as PlayerGameState)
        if (payload.type === 'PUBLIC_EVENT') {
          setState((current) => {
            if (!current) return current
            const incoming = payload.data as GameEvent
            if (current.events.some((existing) => existing.id === incoming.id)) return current
            return { ...current, events: [...current.events, incoming].slice(-30) }
          })
        }
      }
      ws.onerror = () => { setError('Connection interrupted. Reconnecting…'); setConnectionStatus('RECONNECTING') }
      ws.onclose = () => {
        if (!closedByEffect) {
          setConnectionStatus('RECONNECTING')
          reconnectTimer = window.setTimeout(connect, 1200)
        }
      }
    }

    connect()
    return () => {
      closedByEffect = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [navigate])

  return { state, error, connectionStatus }
}

function PlayerLobby() {
  const navigate = useNavigate()
  const { state, error, connectionStatus } = usePlayerSocket()

  useEffect(() => {
    if (state?.status === 'ACTIVE' || state?.status === 'PAUSED' || state?.status === 'GAME_OVER') {
      navigate('/game', { replace: true })
    }
  }, [navigate, state?.status])

  if (!state) return <LoadingState text={error || 'Connecting to the arena…'} />

  return (
    <section className="panel stack">
      <div>
        <div className="connection-line"><span className={`connection-dot ${connectionStatus.toLowerCase()}`} /> {connectionStatus === 'CONNECTED' ? 'LIVE CONNECTION' : 'RECONNECTING'}</div>
        <p className="eyebrow">LOBBY</p>
        <h2>WELCOME, {state.player.name.toUpperCase()}</h2>
        <p className="muted">Your player ID is <strong>{state.player.id}</strong>. Keep this page open.</p>
      </div>
      <div className="stat-grid">
        <Stat label="DISTRICT" value={`D${state.player.district} · ${state.player.gender}`} />
        <Stat label="REGISTERED" value={`${state.playerCount}/${state.maxPlayers}`} />
        <Stat label="STATUS" value="WAITING" />
      </div>
      <div className="waiting-panel">
        <div className="loading-dot" />
        <div><strong>Waiting for the Gamemakers to open the arena.</strong><p className="muted">Once the games begin, your zone, supplies and first timed round will appear here.</p></div>
      </div>
      <EventFeed events={state.events} />
    </section>
  )
}

function Countdown({ deadline, serverNow, durationSeconds, paused = false, compact = false }: { deadline: string | null; serverNow: string; durationSeconds: number; paused?: boolean; compact?: boolean }) {
  const offsetMs = useMemo(() => new Date(serverNow).getTime() - Date.now(), [serverNow])
  const [remaining, setRemaining] = useState(0)

  useEffect(() => {
    const update = () => {
      if (paused || !deadline) {
        setRemaining(0)
        return
      }
      setRemaining(Math.max(0, new Date(deadline).getTime() - (Date.now() + offsetMs)))
    }
    update()
    const id = window.setInterval(update, 100)
    return () => window.clearInterval(id)
  }, [deadline, offsetMs, paused])

  const seconds = Math.ceil(remaining / 1000)
  const fraction = paused ? 0 : Math.min(1, remaining / Math.max(1000, durationSeconds * 1000))

  const urgent = seconds <= 5 && seconds > 0
  if (compact) {
    return (
      <div className={`timer ${urgent ? 'urgent' : ''} ${paused ? 'is-paused' : ''}`} aria-label="Time remaining">
        <span className="timer-num">{paused ? '—' : seconds}</span>
        <span className="timer-track"><span style={{ width: `${Math.max(0, fraction * 100)}%` }} /></span>
      </div>
    )
  }

  return (
    <div className={`countdown ${urgent ? 'urgent' : ''} ${paused ? 'is-paused' : ''}`}>
      <div className="countdown-label">{paused ? 'PAUSED' : 'TIME REMAINING'}</div>
      <div className="countdown-number">{paused ? '—' : seconds}</div>
      <div className="countdown-track"><span style={{ width: `${Math.max(0, fraction * 100)}%` }} /></div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><span>{label}</span><strong>{value}</strong></div>
}

const ITEM_BLURB: Record<string, string> = {
  MEDKIT: 'heal 50% HP',
  SHINY_SWORD: '+40% ATK',
  GOLDEN_APPLE: '+50% stats · 5T',
  SHADOW_CLOAK: '+35% dodge chance',
  TITAN_SHIELD: '−40% fight DMG · 7 uses',
  HUNTERS_FEATHER: '+50% AGI · +50% fight DMG',
  PHOENIX_ASHES: 'revive at 35% HP',
  HEART_OF_IRON: '+60% max HP · −50% AGI',
  SERPENTINE_DAGGER: '0.75x ATK · poison on hit',
  BERSERKER_GAUNTLETS: '+30% / +75% ATK when low HP',
  ADVENTURERS_BOOTS: '2-step travel · −40% env DMG',
  CROWN_OF_BLOOD: '+30% all stats per kill',
}

function HpBar({ player }: { player: Player }) {
  const pct = Math.max(0, Math.min(100, (player.health / player.maxHealth) * 100))
  return (
    <div className={`hp ${pct <= 25 ? 'low' : ''}`}>
      <span className="hp-label">HP</span>
      <div className="hp-track"><span style={{ width: `${pct}%` }} /></div>
      <strong className="hp-value">{player.health}<small>/{player.maxHealth}</small></strong>
    </div>
  )
}

function BattlePanel({ battle, opponentName, serverNow, durationSeconds, disabled, inventoryItem, onAction }: {
  battle: Battle
  opponentName: string
  serverNow: string
  durationSeconds: number
  disabled: boolean
  inventoryItem?: InventoryItem
  onAction: (action: string) => void
}) {
  const options = [
    { id: 'BATTLE_ATTACK', label: 'ATTACK', detail: 'Strike', tone: 'atk' },
    { id: 'BATTLE_DEFEND', label: 'DEFEND', detail: 'Brace', tone: 'def' },
    { id: 'BATTLE_RUN', label: 'RUN', detail: 'Escape', tone: 'run' },
    ...(inventoryItem && ['MEDKIT', 'GOLDEN_APPLE'].includes(inventoryItem.type)
      ? [{ id: 'BATTLE_USE_ITEM', label: 'USE ITEM', detail: inventoryItem.name, tone: 'item' }]
      : []),
  ]
  return (
    <div className="pg-battle">
      <div className="pg-battle-top">
        <div>
          <span className="pg-eyebrow">ENGAGED · TURN {battle.turn}</span>
          <strong>vs {opponentName}</strong>
        </div>
        <Countdown compact deadline={battle.deadline} serverNow={serverNow} durationSeconds={durationSeconds} />
      </div>
      <div className={`pg-battle-actions ${options.length > 3 ? 'has-item' : ''}`}>
        {options.map((option) => (
          <button key={option.id} className={`pg-btn battle ${option.tone} ${battle.yourAction === option.id ? 'selected' : ''}`} disabled={disabled} onClick={() => onAction(option.id)}>
            <strong>{option.label}</strong><small>{option.detail}</small>
          </button>
        ))}
      </div>
      <p className="pg-note">
        {battle.yourAction ? `You chose ${battle.yourAction.replace('BATTLE_', '')}. ` : 'Pick your move. '}
        {battle.opponentActionSubmitted ? 'Opponent has locked in.' : 'Opponent is thinking…'}
      </p>
    </div>
  )
}

function PlayerGame() {
  const { state, error, connectionStatus } = usePlayerSocket()
  const [actionError, setActionError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [targetId, setTargetId] = useState('')
  const [hitShake, setHitShake] = useState(false)
  const previousHealth = useRef<number | null>(null)
  const seenHitEvents = useRef<Set<string>>(new Set())

  const opponents = state?.visibleOpponents ?? []

  useEffect(() => {
    if (!state) return
    const previous = previousHealth.current
    const newHitEvent = state.events.some((event) => {
      if (!['BATTLE_HIT', 'HAZARD_DAMAGE'].includes(event.type) || seenHitEvents.current.has(event.id)) return false
      seenHitEvents.current.add(event.id)
      return true
    })
    if (previous !== null && state.player.health < previous && newHitEvent) {
      setHitShake(false)
      requestAnimationFrame(() => setHitShake(true))
      window.setTimeout(() => setHitShake(false), 520)
    }
    previousHealth.current = state.player.health
    if (seenHitEvents.current.size > 200) {
      const keep = state.events.map((event) => event.id)
      seenHitEvents.current = new Set(keep)
    }
  }, [state])
  useEffect(() => { setActionError('') }, [state?.round])
  useEffect(() => {
    if (!opponents.some((opponent) => opponent.id === targetId)) setTargetId(opponents[0]?.id ?? '')
  }, [opponents, targetId])

  if (!state) return <LoadingState text={error || 'Entering the arena…'} />

  const player = state.player
  const isFinished = state.status === 'GAME_OVER'
  const isPaused = state.status === 'PAUSED'
  const playable = state.status === 'ACTIVE' && player.alive && !player.actionTaken && !state.battle
  const can = (action: string) => playable && !submitting && state.availableActions.includes(action)
  const held = player.inventory[0]
  const offered = state.offeredItem
  const chosen = player.currentAction

  async function doAction(action: string, options?: { targetZoneId?: string; targetPlayerId?: string; itemId?: string }) {
    const raw = localStorage.getItem(PLAYER_STORAGE_KEY)
    if (!raw || submitting) return
    const session = JSON.parse(raw) as StoredSession
    setActionError('')
    setSubmitting(true)
    try {
      await submitAction(session.sessionToken, session.player.id, action, options?.targetZoneId, options?.targetPlayerId, options?.itemId)
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Action failed')
    } finally {
      setSubmitting(false)
    }
  }

  const opponentName = opponents.find((opponent) => opponent.id === player.battleOpponentId)?.name ?? 'your opponent'

  let controls: ReactNode
  if (!player.alive) {
    controls = <div className="pg-card end"><span className="pg-eyebrow">A CANNON SOUNDS</span><strong>You have fallen.</strong><p>{player.lastResult}</p></div>
  } else if (isFinished) {
    controls = <div className="pg-card victor"><span className="pg-eyebrow">GAME OVER</span><strong>{state.winnerId === player.id ? 'VICTOR OF THE ARENA.' : 'THE GAMES HAVE ENDED.'}</strong><p>{player.lastResult}</p></div>
  } else if (isPaused) {
    controls = <div className="pg-card"><span className="pg-eyebrow">PAUSED</span><strong>Stand by.</strong><p>The Gamemakers paused the arena. Your timer is frozen.</p></div>
  } else if (state.battle) {
    controls = (
      <BattlePanel
        battle={state.battle}
        inventoryItem={held}
        opponentName={opponentName}
        serverNow={state.serverNow}
        durationSeconds={state.battleTurnDurationSeconds}
        disabled={submitting || Boolean(state.battle.yourAction) || !state.availableActions.some((action) => action.startsWith('BATTLE_'))}
        onAction={(action) => void doAction(action)}
      />
    )
  } else {
    controls = (
      <>
        <div className="pg-actions">
          {(['REST', 'WAIT'] as const).map((action) => (
            <button key={action} className={`pg-btn ${chosen === action ? 'selected' : ''}`} disabled={!can(action)} onClick={() => void doAction(action)}>{action}</button>
          ))}
        </div>

        <div className={`pg-box ${held ? '' : 'empty'}`}>
          <div className="pg-box-text">
            <span className="pg-eyebrow">ITEM</span>
            {held ? <strong>{held.name} <small>{ITEM_BLURB[held.type] ?? ''}{held.type === 'TITAN_SHIELD' ? ` · ${held.usesRemaining ?? 7} uses left` : ''}</small></strong> : <strong className="dim">Empty hands</strong>}
          </div>
          {held && ['MEDKIT', 'GOLDEN_APPLE'].includes(held.type) && <button className={`pg-btn small ${chosen === 'USE_ITEM' ? 'selected' : ''}`} disabled={!can('USE_ITEM')} onClick={() => void doAction('USE_ITEM', { itemId: held.id })}>USE</button>}
        </div>

        <div className={`pg-box foes ${opponents.length === 0 ? 'alone' : ''}`}>
          <span className="pg-eyebrow">IN YOUR ZONE · {opponents.length}</span>
          {opponents.length === 0 ? <strong className="dim">Alone here</strong> : (
            <div className="pg-attack-row">
              <select value={targetId} onChange={(e) => setTargetId(e.target.value)} disabled={!can('ATTACK')}>
                {opponents.map((opponent: VisibleOpponent) => <option key={opponent.id} value={opponent.id}>{opponent.name} · {opponent.health} HP</option>)}
              </select>
              <button className="pg-btn danger small" disabled={!can('ATTACK') || !targetId} onClick={() => void doAction('ATTACK', { targetPlayerId: targetId })}>ATTACK</button>
            </div>
          )}
        </div>

        {offered && (
          <div className="pg-offer">
            <div className="pg-box-text">
              <span className="pg-eyebrow">{held ? 'SWAP OR KEEP YOURS' : 'CORNUCOPIA'}</span>
              <strong>{offered.name} <small>{ITEM_BLURB[offered.type] ?? ''}</small></strong>
            </div>
            <button className={`pg-btn small grab ${chosen === 'GRAB_ITEM' ? 'selected' : ''}`} disabled={!can('GRAB_ITEM')} onClick={() => void doAction('GRAB_ITEM')}>{held ? 'SWAP' : 'TAKE'}</button>
          </div>
        )}

        <div className="pg-move">
          <span className="pg-eyebrow">MOVE TO</span>
          <div className={`pg-move-grid ${state.adjacentZones.length > 4 ? 'many' : ''}`}>
            {state.adjacentZones.map((zone) => (
              <button key={zone.id} className="pg-btn move" disabled={!can('MOVE')} onClick={() => void doAction('MOVE', { targetZoneId: zone.id })}>{zone.name}</button>
            ))}
          </div>
        </div>
      </>
    )
  }

  return (
    <div className={`pg ${player.alive && !isFinished && !isPaused ? 'capped' : ''} ${hitShake ? 'hit-shake' : ''}`}>
      <header className="pg-head">
        <div className="pg-zone">
          <span className="pg-eyebrow">ROUND {state.round} · {state.phase} · D{player.district} · {player.gender} · {state.aliveCount} ALIVE</span>
          <strong>{state.currentZone.name}</strong>
        </div>
        <Countdown compact deadline={state.roundDeadline} serverNow={state.serverNow} durationSeconds={state.roundDurationSeconds} paused={isPaused} />
      </header>

      <HpBar player={player} />

      {connectionStatus !== 'CONNECTED' && <div className="pg-alert conn">Reconnecting… your state will resync.</div>}
      {state.zoneHazard && player.alive && !isFinished && (
        <div className="pg-alert">HAZARD · {state.hazardName}{state.hazardDescription ? ` · ${state.hazardDescription}` : ''}</div>
      )}
      <p className="pg-status">{player.lastResult}</p>

      <div className="pg-controls">{controls}</div>
      {actionError && <div className="pg-error" role="alert">{actionError}</div>}

      <section className="pg-feed"><EventFeed events={state.events} compact /></section>
    </div>
  )
}

function AdminPage() {
  const [authenticated, setAuthenticated] = useState(Boolean(localStorage.getItem(ADMIN_STORAGE_KEY)))
  const [tokenInput, setTokenInput] = useState('')
  const [state, setState] = useState<AdminState | null>(null)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [selectedPlayerId, setSelectedPlayerId] = useState('')
  const [operatorValue, setOperatorValue] = useState('50')
  const [operatorAttack, setOperatorAttack] = useState('10')
  const [operatorDefense, setOperatorDefense] = useState('10')
  const [operatorAgility, setOperatorAgility] = useState('10')
  const [operatorItem, setOperatorItem] = useState('MEDKIT')
  const [operatorZone, setOperatorZone] = useState('')
  const [announcement, setAnnouncement] = useState('')
  const [adminConnection, setAdminConnection] = useState<'CONNECTING' | 'CONNECTED' | 'RECONNECTING'>('CONNECTING')

  async function login(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await adminLogin(tokenInput)
      localStorage.setItem(ADMIN_STORAGE_KEY, tokenInput)
      setAuthenticated(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    }
  }

  useEffect(() => {
    if (!authenticated) return
    const token = localStorage.getItem(ADMIN_STORAGE_KEY)
    if (!token) return

    let closed = false
    let ws: WebSocket | null = null
    let reconnectTimer: number | undefined

    const load = async () => {
      try {
        const data = await getAdminState(token) as AdminState
        if (!closed) setState(data)
      } catch {
        localStorage.removeItem(ADMIN_STORAGE_KEY)
        setAuthenticated(false)
      }
    }

    const connect = () => {
      if (closed) return
      setAdminConnection(reconnectTimer ? 'RECONNECTING' : 'CONNECTING')
      ws = new WebSocket(adminWsUrl(token))
      ws.onopen = () => { setError(''); setAdminConnection('CONNECTED') }
      ws.onmessage = (message) => {
        const payload = JSON.parse(message.data) as { type: string; data: AdminState }
        if (payload.type === 'ADMIN_STATE') setState(payload.data)
      }
      ws.onerror = () => { setError('Admin live connection interrupted. Reconnecting…'); setAdminConnection('RECONNECTING') }
      ws.onclose = () => { if (!closed) { setAdminConnection('RECONNECTING'); reconnectTimer = window.setTimeout(connect, 1200) } }
    }

    void load()
    connect()
    return () => {
      closed = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [authenticated])

  async function doAction(action: string, options: AdminActionOptions = {}) {
    const token = localStorage.getItem(ADMIN_STORAGE_KEY)
    if (!token) return
    setError('')
    try {
      const next = await adminAction(token, action, options) as AdminState
      setState(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Action failed')
    }
  }

  const selectedPlayer = state?.players.find((player) => player.id === selectedPlayerId) ?? null

  useEffect(() => {
    if (selectedPlayer) setOperatorZone(selectedPlayer.zoneId)
  }, [selectedPlayerId, selectedPlayer?.zoneId])

  async function runPlayerAction(action: string) {
    if (!selectedPlayer) return
    if (action === 'ELIMINATE_PLAYER' && !window.confirm(`Eliminate ${selectedPlayer.name}?`)) return
    if (action === 'MOVE_PLAYER') {
      const destination = operatorZone || selectedPlayer.zoneId
      await doAction(action, { targetPlayerId: selectedPlayer.id, targetZoneId: destination })
      return
    }
    if (action === 'GIVE_ITEM') {
      await doAction(action, { targetPlayerId: selectedPlayer.id, itemType: operatorItem })
      return
    }
    if (action === 'SET_HEALTH') {
      await doAction(action, { targetPlayerId: selectedPlayer.id, value: Number(operatorValue) })
      return
    }
    if (action === 'SET_STATS') {
      await doAction(action, { targetPlayerId: selectedPlayer.id, value: Number(operatorValue), attack: Number(operatorAttack), defense: Number(operatorDefense), agility: Number(operatorAgility) })
      return
    }
    await doAction(action, { targetPlayerId: selectedPlayer.id, value: Number(operatorValue) })
  }

  function logout() {
    localStorage.removeItem(ADMIN_STORAGE_KEY)
    setAuthenticated(false)
    setState(null)
  }

  const filteredPlayers = useMemo(() => {
    if (!state) return []
    const term = search.trim().toLowerCase()
    return state.players.filter((player) => !term || player.name.toLowerCase().includes(term) || player.id.toLowerCase().includes(term) || player.zoneName.toLowerCase().includes(term))
  }, [search, state])

  if (!authenticated) {
    return (
      <section className="narrow panel">
        <p className="eyebrow">ARENA CONTROL</p>
        <h2>ADMIN LOGIN</h2>
        <p className="muted">Use the admin token configured on the FastAPI server.</p>
        <form onSubmit={login} className="stack">
          <input type="password" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} placeholder="Admin token" />
          <button disabled={!tokenInput} className="button button-primary" type="submit">UNLOCK DASHBOARD</button>
        </form>
        {error && <div className="error">{error}</div>}
      </section>
    )
  }

  if (!state) return <LoadingState text="Loading arena control…" />

  return (
    <section className="admin-layout">
      <div className="panel stack">
        <div className="game-header">
          <div><div className="connection-line"><span className={`connection-dot ${adminConnection.toLowerCase()}`} /> {adminConnection === 'CONNECTED' ? 'LIVE ADMIN CONNECTION' : 'ADMIN RECONNECTING'}</div><p className="eyebrow">ARENA CONTROL</p><h2>MAIN GAME</h2></div>
          <button className="button button-secondary button-small" onClick={logout}>LOG OUT</button>
        </div>
        <div className="admin-metrics">
          <Stat label="STATUS" value={state.status} />
          <Stat label="PHASE" value={state.phase} />
          <Stat label="ROUND" value={String(state.round)} />
          <Stat label="ALIVE" value={String(state.aliveCount)} />
          <Stat label="ONLINE" value={String(state.onlineCount)} />
          <Stat label="REGISTERED" value={`${state.playerCount}/${state.maxPlayers}`} />
          <Stat label="ACTED" value={`${state.actedCount}/${state.aliveCount}`} />
          <Stat label="WAITING" value={String(state.waitingCount)} />
        </div>
        {(state.status === 'ACTIVE' || state.status === 'PAUSED') && <div className="round-progress">
          <div className="round-progress-top"><span>ACTION PROGRESS</span><strong>{state.actedCount}/{state.aliveCount} alive players acted</strong></div>
          <div className="round-progress-track"><span style={{ width: `${state.aliveCount ? (state.actedCount / state.aliveCount) * 100 : 0}%` }} /></div>
        </div>}
        <div className="button-row wrap">
          {state.status === 'LOBBY' && <button className="button button-primary" onClick={() => void doAction('START_GAME')}>START GAME</button>}
          {state.status === 'ACTIVE' && <button className="button button-secondary" onClick={() => void doAction('PAUSE_GAME')}>PAUSE</button>}
          {state.status === 'ACTIVE' && <button className="button button-secondary" onClick={() => void doAction('END_ROUND')}>END ROUND</button>}
          {state.status === 'PAUSED' && <button className="button button-primary" onClick={() => void doAction('RESUME_GAME')}>RESUME</button>}
          <button className="button button-danger" onClick={() => { if (window.confirm('Reset the entire arena? All players will be removed.')) void doAction('RESET_GAME') }}>RESET</button>
        </div>
        {(state.status === 'ACTIVE' || state.status === 'PAUSED') && <Countdown deadline={state.roundDeadline} serverNow={state.serverNow} durationSeconds={state.roundDurationSeconds} paused={state.status === 'PAUSED'} />}
        {error && <div className="error">{error}</div>}
      </div>

      {(state.status === 'ACTIVE' || state.status === 'PAUSED') && (
        <div className="panel admin-events-controls">
          <div>
            <div className="section-heading">EVENT CONTROLS</div>
            <p className="muted">Manual controls are useful if the organizers want to push the arena along during the live event.</p>
          </div>
          <div className="event-control-row">
            <button className="button button-secondary" onClick={() => void doAction('SPAWN_SUPPLY_DROP')}>DROP SUPPLY</button>
            <button className="button button-secondary" onClick={() => void doAction('CLEAR_HAZARDS')}>DEACTIVATE ALL HAZARDS</button>
          </div>
          <div className="event-control-row">
            <input value={announcement} onChange={(e) => setAnnouncement(e.target.value)} placeholder="Announcement visible to every player" maxLength={240} />
            <button className="button button-primary" disabled={!announcement.trim()} onClick={() => { void doAction('BROADCAST_ANNOUNCEMENT', { message: announcement.trim() }); setAnnouncement('') }}>BROADCAST</button>
          </div>
        </div>
      )}

      <div className="panel operator-panel">
        <div>
          <div className="section-heading">PLAYER OPERATOR TOOLS</div>
          <p className="muted">Select a participant to correct an event mistake or administer a manual arena intervention.</p>
        </div>
        <div className="operator-grid">
          <div className="operator-selected">
            <label>SELECTED PLAYER</label>
            <select value={selectedPlayerId} onChange={(e) => setSelectedPlayerId(e.target.value)}>
              <option value="">Choose a player</option>
              {state.players.map((player) => <option key={player.id} value={player.id}>{player.id} — {player.name}</option>)}
            </select>
            {selectedPlayer && <div className="selected-player-summary"><strong>{selectedPlayer.name}</strong><span>{selectedPlayer.id} · {selectedPlayer.zoneName} · {selectedPlayer.health}/{selectedPlayer.maxHealth} HP</span></div>}
          </div>
          <div className="operator-values">
            <label>HEALTH</label><input type="number" min="0" max="500" value={operatorValue} onChange={(e) => setOperatorValue(e.target.value)} />
            <label>ATTACK</label><input type="number" min="1" max="100" step="0.1" value={operatorAttack} onChange={(e) => setOperatorAttack(e.target.value)} />
            <label>DEFENSE</label><input type="number" min="1" max="100" step="0.1" value={operatorDefense} onChange={(e) => setOperatorDefense(e.target.value)} />
            <label>AGILITY</label><input type="number" min="1" max="100" step="0.1" value={operatorAgility} onChange={(e) => setOperatorAgility(e.target.value)} />
            <label>ITEM</label><select value={operatorItem} onChange={(e) => setOperatorItem(e.target.value)}>{['MEDKIT','SHINY_SWORD','GOLDEN_APPLE','SHADOW_CLOAK','TITAN_SHIELD','HUNTERS_FEATHER','PHOENIX_ASHES','HEART_OF_IRON','SERPENTINE_DAGGER','BERSERKER_GAUNTLETS','ADVENTURERS_BOOTS','CROWN_OF_BLOOD'].map((item) => <option key={item}>{item}</option>)}</select>
            <label>DESTINATION</label><select value={operatorZone || selectedPlayer?.zoneId || ''} onChange={(e) => setOperatorZone(e.target.value)}>{state.zones.map((zone) => <option key={zone.id} value={zone.id}>{zone.name}</option>)}</select>
          </div>
          <div className="button-row wrap operator-actions">
            <button className="button button-danger" disabled={!selectedPlayer} onClick={() => void runPlayerAction('ELIMINATE_PLAYER')}>ELIMINATE</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('RESTORE_PLAYER')}>RESTORE</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('SET_HEALTH')}>SET HP</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('SET_STATS')}>SET STATS</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('MOVE_PLAYER')}>MOVE PLAYER</button>
            <button className="button button-secondary" disabled={!selectedPlayer} onClick={() => void runPlayerAction('GIVE_ITEM')}>GIVE ITEM</button>
          </div>
        </div>
      </div>

      <div className="zone-dashboard panel">
        <div className="section-heading">ARENA ZONES</div>
        <div className="zone-grid">
          {state.zones.map((zone) => <ZoneAdminCard key={zone.id} zone={zone} onToggleHazard={(zoneId) => void doAction('TOGGLE_HAZARD', { targetZoneId: zoneId })} />)}
        </div>
      </div>

      <div className="panel">
        <div className="table-toolbar"><div className="section-heading">PLAYERS</div><input className="table-search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search player, ID or zone" /></div>
        <div className="player-table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Name</th><th>District</th><th>Gender</th><th>Zone</th><th>HP</th><th>ATK</th><th>DEF</th><th>AGI</th><th>Items</th><th>Kills</th><th>Action</th><th>Status</th><th>Connection</th></tr></thead>
            <tbody>{filteredPlayers.map((player) => (
              <tr key={player.id} className={selectedPlayerId === player.id ? 'selected-row' : ''} onClick={() => setSelectedPlayerId(player.id)}>
                <td>{player.id}</td><td>{player.name}</td><td>D{player.district}</td><td>{player.gender}</td><td>{player.zoneName}</td><td>{player.health}</td><td>{fmtStat(player.attack)}</td><td>{fmtStat(player.defense)}</td><td>{fmtStat(player.agility)}</td>
                <td>{player.inventory.length}</td><td>{player.kills}</td>
                <td>{player.currentAction ?? (player.actionTaken ? 'DONE' : 'WAITING')}</td>
                <td><span className={player.alive ? 'tag alive' : 'tag dead'}>{player.alive ? player.statusEffect : 'DEAD'}</span></td>
                <td>{player.connected ? 'ONLINE' : 'OFFLINE'}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </div>

      <div className="panel"><EventFeed events={state.events} compact /></div>
    </section>
  )
}

function SpectatePage() {
  const [authenticated, setAuthenticated] = useState(Boolean(localStorage.getItem(SPECTATE_STORAGE_KEY)))
  const [tokenInput, setTokenInput] = useState('')
  const [state, setState] = useState<SpectateState | null>(null)
  const [error, setError] = useState('')
  const [connectionStatus, setConnectionStatus] = useState<'CONNECTING' | 'CONNECTED' | 'RECONNECTING'>('CONNECTING')

  async function login(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await adminLogin(tokenInput)
      localStorage.setItem(SPECTATE_STORAGE_KEY, tokenInput)
      setAuthenticated(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    }
  }

  useEffect(() => {
    if (!authenticated) return
    const token = localStorage.getItem(SPECTATE_STORAGE_KEY)
    if (!token) return

    let closed = false
    let ws: WebSocket | null = null
    let reconnectTimer: number | undefined

    const load = async () => {
      try {
        const data = await getSpectateState(token) as SpectateState
        if (!closed) setState(data)
      } catch {
        localStorage.removeItem(SPECTATE_STORAGE_KEY)
        setAuthenticated(false)
      }
    }

    const connect = () => {
      if (closed) return
      setConnectionStatus(reconnectTimer ? 'RECONNECTING' : 'CONNECTING')
      ws = new WebSocket(spectateWsUrl(token))
      ws.onopen = () => { setError(''); setConnectionStatus('CONNECTED') }
      ws.onmessage = (message) => {
        const payload = JSON.parse(message.data) as { type: string; data: SpectateState }
        if (payload.type === 'SPECTATE_STATE') setState(payload.data)
      }
      ws.onerror = () => { setError('Live spectator connection interrupted. Reconnecting…'); setConnectionStatus('RECONNECTING') }
      ws.onclose = () => { if (!closed) { setConnectionStatus('RECONNECTING'); reconnectTimer = window.setTimeout(connect, 1200) } }
    }

    void load()
    connect()
    return () => {
      closed = true
      if (reconnectTimer) window.clearTimeout(reconnectTimer)
      ws?.close()
    }
  }, [authenticated])

  function logout() {
    localStorage.removeItem(SPECTATE_STORAGE_KEY)
    setAuthenticated(false)
    setState(null)
  }

  if (!authenticated) {
    return (
      <section className="narrow panel">
        <p className="eyebrow">LIVE SPECTATOR ACCESS</p>
        <h2>UNLOCK THE ARENA FEED</h2>
        <p className="muted">Enter the admin token to open the live spectator view.</p>
        <form onSubmit={login} className="stack">
          <input type="password" value={tokenInput} onChange={(e) => setTokenInput(e.target.value)} placeholder="Admin token" />
          <button disabled={!tokenInput} className="button button-primary" type="submit">WATCH LIVE</button>
        </form>
        {error && <div className="error">{error}</div>}
      </section>
    )
  }

  if (!state) return <LoadingState text="Opening live spectator feed…" />

  const playerById = new Map(state.players.map((player) => [player.id, player]))
  const activeBattles = state.battles.filter((battle) => playerById.get(battle.playerAId)?.alive && playerById.get(battle.playerBId)?.alive)
  const orderedPlayers = state.players.slice().sort((a, b) => Number(b.alive) - Number(a.alive) || Number(Boolean(b.battle)) - Number(Boolean(a.battle)) || a.name.localeCompare(b.name))

  return (
    <section className="spectate-layout">
      <div className="panel spectate-hero">
        <div className="game-header">
          <div>
            <div className="connection-line"><span className={`connection-dot ${connectionStatus.toLowerCase()}`} /> {connectionStatus === 'CONNECTED' ? 'LIVE SPECTATOR FEED' : 'SPECTATOR RECONNECTING'}</div>
            <p className="eyebrow">THE ARENA // SPECTATE</p>
            <h2>WATCH THE ARENA UNFOLD</h2>
          </div>
          <button className="button button-secondary button-small" onClick={logout}>LOCK</button>
        </div>
        <div className="spectate-metrics">
          <Stat label="STATUS" value={state.status} />
          <Stat label="PHASE" value={state.phase} />
          <Stat label="ROUND" value={String(state.round)} />
          <Stat label="ALIVE" value={`${state.aliveCount}/${state.playerCount}`} />
          <Stat label="BATTLES" value={String(activeBattles.length)} />
        </div>
      </div>

      <div className="panel spectate-spotlight">
        <div className="section-heading">BATTLES IN PROGRESS <span className="heading-count">{activeBattles.length}</span></div>
        {activeBattles.length === 0 ? <div className="spectate-empty"><strong>No clashes right now.</strong><span>The feed is quiet… for the moment.</span></div> : (
          <div className="battle-spotlight-grid">
            {activeBattles.map((battle) => {
              const a = playerById.get(battle.playerAId)
              const b = playerById.get(battle.playerBId)
              return (
                <div className="battle-spotlight-card" key={battle.id}>
                  <div className="battle-spotlight-top"><span>TURN {battle.turn}</span><strong>{a?.zoneName ?? 'ARENA'}</strong></div>
                  <div className="versus-row">
                    <SpectateCombatant player={a} />
                    <span className="versus-mark">VS</span>
                    <SpectateCombatant player={b} />
                  </div>
                  <div className="battle-spotlight-status"><span>{battle.actions?.[battle.playerAId] ? battle.actions[battle.playerAId].replace('BATTLE_', '') : 'THINKING'}</span><span>{battle.actions?.[battle.playerBId] ? battle.actions[battle.playerBId].replace('BATTLE_', '') : 'THINKING'}</span></div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="section-heading">LIVE ARENA MAP</div>
        <div className="arena-map-with-legend">
          <ArenaMap zones={state.zones} players={state.players} />
          <div className="arena-player-legend">
            <div className="arena-player-legend-heading">TRIBUTES</div>
            {state.players
              .slice()
              .sort((a, b) => Number(b.alive) - Number(a.alive) || a.district - b.district || a.gender.localeCompare(b.gender))
              .map((player) => (
              <div key={player.id} className={`arena-player-legend-item ${!player.alive ? 'dead' : ''} ${player.battle ? 'battle' : ''}`}>
                <span className={`arena-player-legend-dot ${player.gender === 'F' ? 'f' : 'm'} ${player.battle ? 'battle' : ''}`}>
                  {player.district}
                </span>
                <span className="arena-player-legend-name">{player.name}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="section-heading">ALL PLAYERS <span className="heading-count">{state.playerCount}</span></div>
        <div className="spectate-player-grid">
          {orderedPlayers.map((player) => <SpectatePlayerCard key={player.id} player={player} />)}
        </div>
      </div>

      <div className="panel"><EventFeed events={state.events} compact /></div>
    </section>
  )
}

function SpectateCombatant({ player }: { player?: SpectateState['players'][number] }) {
  if (!player) return <div className="spectate-combatant"><strong>UNKNOWN</strong><span>—</span></div>
  return <div className="spectate-combatant"><strong>{player.name}</strong><span>{player.health}/{player.maxHealth} HP · ATK {fmtStat(player.attack)}</span></div>
}

function SpectatePlayerCard({ player }: { player: SpectateState['players'][number] }) {
  const health = `${Math.max(0, Math.min(100, (player.health / player.maxHealth) * 100))}%`
  return (
    <div className={`spectate-player-card ${player.alive ? 'alive' : 'dead'} ${player.battle ? 'in-battle' : ''}`}>
      <div className="spectate-player-top"><strong>{player.name}</strong><span>{player.alive ? (player.battle ? 'IN BATTLE' : player.statusEffect) : 'ELIMINATED'}</span></div>
      <div className="spectate-player-zone">D{player.district} · {player.gender} · {player.zoneName} · {player.id}</div>
      <div className="spectate-health"><span style={{ width: health }} /></div>
      <div className="spectate-player-stats"><span>{player.health}/{player.maxHealth} HP</span><span>ATK {fmtStat(player.attack)}</span><span>DEF {fmtStat(player.defense)}</span><span>AGI {fmtStat(player.agility)}</span><span>{player.inventory.length} ITEM{player.inventory.length === 1 ? '' : 'S'}</span></div>
      {player.battle && <div className="spectate-battle-note">Turn {player.battle.turn} · {player.battle.yourAction ? player.battle.yourAction.replace('BATTLE_', '') : 'CHOOSING'}</div>}
      <p>{player.lastResult}</p>
    </div>
  )
}

function ZoneAdminCard({ zone, onToggleHazard }: { zone: Zone; onToggleHazard?: (zoneId: string) => void }) {
  const count = zone.playerCount ?? 0
  const loot = zone.lootCount ?? 0
  return (
    <div className={`zone-card ${zone.id === 'cornucopia' ? 'cornucopia' : ''} ${zone.hazard ? 'hazard' : ''}`}>
      <div className="zone-card-top"><strong>{zone.name}</strong><span>{count}</span></div>
      <small>{zone.description}</small>
      {zone.hazardName && <div className="zone-hazard-name">{zone.hazardName}</div>}
      <div className="zone-flags"><span>{loot} loot</span>{zone.hazard && <span className="hazard-flag">ACTIVE</span>}</div>
      {onToggleHazard && zone.id !== 'cornucopia' && (
        <button className={`button button-small ${zone.hazard ? 'button-danger' : 'button-secondary'}`} onClick={() => onToggleHazard(zone.id)}>
          {zone.hazard ? 'DEACTIVATE HAZARD' : 'ACTIVATE HAZARD'}
        </button>
      )}
    </div>
  )
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
        <Route path="/spectate" element={<SpectatePage />} />
        <Route path="/trinav" element={<section />} />
        <Route path="/chewie" element={<section />} />
        <Route path="/arpit" element={<section />} />
        <Route path="*" element={<LandingPage />} />
      </Routes>
    </Shell>
  )
}

export default App
