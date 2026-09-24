# The Arena — Phase 3

A college-event prototype for a **single-instance, text-first multiplayer arena game** designed for roughly **200 participants in one shared match**.

Phase 3 adds the first real survival systems: combat, items, inventory, supply drops, and arena hazards.

## Stack

- FastAPI + Python backend
- React + TypeScript + Vite frontend
- WebSockets for real-time updates
- In-memory game state for the live event

## Phase 3 includes

### Core game

- One shared game: `main_game`
- 200-player cap
- 13 zones: 12 outer zones + central Cornucopia
- Global timed rounds (15 seconds by default)
- Backend-authoritative deadlines
- One action per player per round
- Timeout and late-submission elimination
- Opening / Main / Final / Game Over phases

### Actions

- MOVE
- SEARCH
- REST
- HIDE
- SCOUT
- ATTACK
- USE_ITEM
- WAIT

### Combat

- Attack only against an alive player in the same zone
- Damage is based on Attack with a small random modifier
- Speed influences dodge chance
- HIDDEN players get an extra dodge bonus
- Small critical-hit chance
- Armor can reduce the next incoming hit
- Health reaching zero causes elimination
- Killer receives +1 kill

### Items

- Medkit: restore 30 HP
- Food: restore 12 HP
- Weapon: permanently +3 Attack
- Armor: next incoming hit reduced by 8
- Speed Boost: permanently +3 Speed
- Inventory limit: 6 by default
- Search can find random loot
- Cornucopia starts with 8 items
- Automatic supply drops every 3 rounds

### Arena events

- Automatic arena hazards every 4 rounds
- Default hazard damage: 8 HP at round end
- Final phase can use two hazard zones instead of one
- Admin can manually trigger a supply drop
- Admin can manually trigger a hazard

### Admin dashboard

- Live game status
- Round and timer
- Alive / registered counts
- Zone population
- Loot count per zone
- Active hazard zones
- Searchable 200-player table
- Player HP / Attack / Speed
- Item count and kills
- Live event feed
- Start / Pause / Resume / End Round / Reset
- Manual supply drop / hazard controls

## Run the backend

```bash
cd backend
python -m venv .venv

# Windows
.venv\\Scripts\\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# Optional event configuration
# PowerShell:
# $env:ADMIN_TOKEN="your-strong-admin-token"
# $env:ROUND_DURATION_SECONDS="15"
# $env:FINAL_PLAYER_THRESHOLD="20"
# $env:SUPPLY_DROP_INTERVAL="3"
# $env:HAZARD_INTERVAL="4"
# $env:HAZARD_DAMAGE="8"
# $env:MAX_INVENTORY="6"

uvicorn app:app --host 0.0.0.0 --port 8000
```

Run tests:

```bash
cd backend
pytest
```

## Run the frontend

In another terminal:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0
```

For a college LAN, participants can open:

```text
http://YOUR-LAN-IP:5173
```

The frontend derives the backend/WebSocket host from the browser hostname unless `VITE_API_BASE` / `VITE_WS_BASE` are explicitly set.

## Event configuration

Default values:

```text
MAX_PLAYERS=200
ROUND_DURATION_SECONDS=15
FINAL_PLAYER_THRESHOLD=20
SUPPLY_DROP_INTERVAL=3
HAZARD_INTERVAL=4
HAZARD_DAMAGE=8
MAX_INVENTORY=6
ADMIN_TOKEN=change-me
```

The live game state is intentionally held in memory. This is appropriate for the event prototype, but the match resets if the backend process restarts.

For the actual event:

- Set a non-default `ADMIN_TOKEN`.
- Run the backend on a stable machine.
- Avoid auto-reload during the live match.
- Test from the same Wi-Fi/LAN that participants will use.

## Game-state privacy

Players receive their own full player state, inventory, and currently visible same-zone opponents. Admin receives the full game state. Public event messages are small and do not expose another player's private inventory.

## Concurrency model

All player actions mutate the shared game state under one `asyncio.Lock`. This is deliberately simple for an event-sized game and prevents simultaneous requests from corrupting player/zone state.

The backend does not run one simulation loop per participant. It runs one lightweight round timer for the entire match.

## Phase 3 test status

The backend test suite contains 18 tests covering:

- 13-zone setup
- Stats and inventory
- Movement
- Search and loot
- Combat and elimination
- Items
- Armor
- Admin events
- Timed round resolution
- Pause / resume
- 200-player registration

## Next phase

Phase 4 should focus on real-time/admin polish: richer admin controls, better event orchestration, reconnect UX, and more event-specific presentation. Advanced combat complexity is not necessary unless the college event rules require it.
