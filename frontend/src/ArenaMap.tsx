import { useMemo, useState } from 'react'
import type { SpectatePlayer, Zone } from './types'

const SIZE = 600
const C = SIZE / 2
const R_OUT = 285
const R_IN = 84
const OUTER_IDS = ['zone_1', 'zone_2', 'zone_3', 'zone_4', 'zone_5', 'zone_6']

// Angles are degrees clockwise from 12 o'clock. Zone N spans [(N-1)*60, N*60].
const pt = (r: number, deg: number) => {
  const a = (deg * Math.PI) / 180
  return { x: C + r * Math.sin(a), y: C - r * Math.cos(a) }
}

function wedgePath(index: number) {
  const a0 = index * 60
  const a1 = a0 + 60
  const p0 = pt(R_OUT, a0), p1 = pt(R_OUT, a1), p2 = pt(R_IN, a1), p3 = pt(R_IN, a0)
  return `M ${p0.x} ${p0.y} A ${R_OUT} ${R_OUT} 0 0 1 ${p1.x} ${p1.y} L ${p2.x} ${p2.y} A ${R_IN} ${R_IN} 0 0 0 ${p3.x} ${p3.y} Z`
}

type Slot = { x: number; y: number }

// Pack n tokens of radius tr into a wedge (annular sector), inner rows first.
function wedgeSlots(index: number, n: number, tr: number): Slot[] | null {
  const gap = 3
  const step = tr * 2 + gap
  const center = index * 60 + 30
  const rMin = R_IN + tr + 10
  const rMax = R_OUT - 62 // leave room for the zone label at the outer edge
  const slots: Slot[] = []
  for (let r = rMin; r <= rMax && slots.length < n; r += step) {
    // available arc: 60deg minus the margin needed so tokens clear the spokes
    const margin = Math.asin(Math.min(1, (tr + 6) / r)) * (180 / Math.PI)
    const span = 60 - margin * 2
    if (span <= 0) continue
    const arc = (span * Math.PI * r) / 180
    const fit = Math.max(1, Math.floor(arc / step) + 1)
    const count = Math.min(fit, n - slots.length)
    const used = count > 1 ? ((count - 1) * step * 180) / (Math.PI * r) : 0
    for (let k = 0; k < count; k++) {
      const deg = center - used / 2 + (count > 1 ? (k * used) / (count - 1) : 0)
      slots.push(pt(r, deg))
    }
  }
  return slots.length >= n ? slots : null
}

// Pack n tokens into the central Cornucopia circle using concentric rings.
function hubSlots(n: number, tr: number): Slot[] | null {
  const gap = 3
  const step = tr * 2 + gap
  const slots: Slot[] = []
  if (n === 1) return [{ x: C, y: C }]
  const limit = R_IN - tr - 6
  for (let r = n <= 6 ? step * 0.62 : step * 0.95; r <= limit && slots.length < n; r += step) {
    const fit = Math.max(1, Math.floor((2 * Math.PI * r) / step))
    const count = Math.min(fit, n - slots.length)
    const offset = slots.length * 0.7
    for (let k = 0; k < count; k++) {
      const deg = offset * 20 + (k * 360) / count
      slots.push(pt(r, deg))
    }
  }
  if (slots.length < n && n > 6) {
    // try a center token too
    if (slots.length + 1 === n) slots.push({ x: C, y: C })
  }
  return slots.length >= n ? slots : null
}

function layout(ids: string[], pack: (n: number, tr: number) => Slot[] | null, maxR: number): { slots: Slot[]; tr: number } {
  const n = ids.length
  for (let tr = maxR; tr >= 8; tr -= 1) {
    const slots = pack(n, tr)
    if (slots) return { slots, tr }
  }
  return { slots: pack(n, 8) ?? [], tr: 8 }
}

type Props = {
  zones: Zone[]
  players: SpectatePlayer[]
}

export function ArenaMap({ zones, players }: Props) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const zoneById = useMemo(() => new Map(zones.map((z) => [z.id, z])), [zones])

  const placed = useMemo(() => {
    const groups = new Map<string, SpectatePlayer[]>()
    for (const p of players) {
      if (!p.alive) continue
      const key = groups.has(p.zoneId) || zoneById.has(p.zoneId) || p.zoneId === 'cornucopia' ? p.zoneId : 'cornucopia'
      const list = groups.get(key) ?? []
      list.push(p)
      groups.set(key, list)
    }
    const out: { player: SpectatePlayer; x: number; y: number; tr: number }[] = []
    groups.forEach((list, zoneId) => {
      list.sort((a, b) => a.district - b.district || a.gender.localeCompare(b.gender))
      const ids = list.map((p) => p.id)
      const idx = OUTER_IDS.indexOf(zoneId)
      const { slots, tr } = idx >= 0
        ? layout(ids, (n, r) => wedgeSlots(idx, n, r), 21)
        : layout(ids, hubSlots, 20)
      list.forEach((player, i) => {
        const s = slots[i] ?? { x: C, y: C }
        out.push({ player, x: s.x, y: s.y, tr })
      })
    })
    return out
  }, [players, zoneById])

  const selected = players.find((p) => p.id === selectedId) ?? null
  const aliveInCorn = placed.filter((t) => t.player.zoneId === 'cornucopia').length
  const cornucopia = zoneById.get('cornucopia')

  return (
    <div className="arena-map">
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="arena-map-svg" role="img" aria-label="Live arena map">
        {OUTER_IDS.map((id, i) => {
          const zone = zoneById.get(id)
          const label = pt(R_OUT - 30, i * 60 + 30)
          const count = placed.filter((t) => t.player.zoneId === id).length
          return (
            <g key={id} className={`arena-wedge ${zone?.hazard ? 'hazard' : ''}`}>
              <path d={wedgePath(i)} className="arena-wedge-shape" />
              <text x={label.x} y={label.y - 6} className="arena-zone-name" textAnchor="middle">ZONE {i + 1}</text>
              <text x={label.x} y={label.y + 8} className="arena-zone-hazard" textAnchor="middle">
                {(zone?.hazardName ?? '').toUpperCase()}
              </text>
              <text x={label.x} y={label.y + 21} className="arena-zone-count" textAnchor="middle">
                {count > 0 ? `${count} ALIVE` : ''}{zone?.hazard ? `${count > 0 ? ' · ' : ''}HAZARD ON` : ''}
              </text>
            </g>
          )
        })}

        <circle cx={C} cy={C} r={R_IN} className="arena-hub" />
        <circle cx={C} cy={C} r={R_OUT} className="arena-rim" />
        {OUTER_IDS.map((id, i) => {
          const a = pt(R_OUT, i * 60), b = pt(R_IN, i * 60)
          return <line key={id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className="arena-spoke" />
        })}
        <text x={C} y={C - R_IN + 17} textAnchor="middle" className="arena-hub-label">CORNUCOPIA</text>
        {aliveInCorn === 0 && (
          <text x={C} y={C + 6} textAnchor="middle" className="arena-hub-empty">{cornucopia?.lootCount ? `${cornucopia.lootCount} loot` : 'empty'}</text>
        )}

        {placed.map(({ player, x, y, tr }) => {
          const inBattle = Boolean(player.battle)
          return (
            <g
              key={player.id}
              className={`arena-token ${inBattle ? 'battle' : ''} ${player.gender === 'F' ? 'f' : 'm'} ${selectedId === player.id ? 'selected' : ''}`}
              style={{ transform: `translate(${x}px, ${y}px)` }}
              onClick={() => setSelectedId(selectedId === player.id ? null : player.id)}
            >
              <title>{`${player.name} · District ${player.district} · ${player.health}/${player.maxHealth} HP`}</title>
              <circle r={tr} className="arena-token-disc" />
              <text className="arena-token-num" textAnchor="middle" dominantBaseline="central" style={{ fontSize: `${Math.max(9, tr * 1.05)}px` }}>
                {player.district}
              </text>
            </g>
          )
        })}
      </svg>

      <div className="arena-map-caption">
        {selected ? (
          <span><strong>{selected.name}</strong> · D{selected.district} {selected.gender} · {selected.zoneName} · {selected.health}/{selected.maxHealth} HP{selected.battle ? ' · IN BATTLE' : ''}</span>
        ) : (
          <span className="muted">Tap a circle to identify a tribute.</span>
        )}
      </div>
      <div className="arena-legend">
        <span><i className="dot m" /> Male tribute</span>
        <span><i className="dot f" /> Female tribute</span>
        <span><i className="dot b" /> In battle</span>
        <span><i className="dot h" /> Active hazard</span>
      </div>
    </div>
  )
}
