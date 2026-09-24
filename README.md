# The Arena — Phase 4

A college-event prototype for a **single shared, text-first multiplayer arena game** designed for roughly **200 participants in one match**.

Phase 4 focuses on making the existing game practical to operate live: stronger admin tooling, real-time connection status, player-state corrections, event announcements, and a lightweight restart checkpoint.

## Stack

- FastAPI + Python backend
- React + TypeScript + Vite frontend
- WebSockets for real-time updates
- In-memory live game state
- JSON checkpoint for restart recovery

## Phase 4 includes

### Existing game systems
- One shared game: `main_game`
- 200-player cap
- 13 zones: 12 outer zones + central Cornucopia
- Global timed rounds (15 seconds by default)
- Backend-authoritative deadlines
- One action per player per round
- Timeout and late-submission elimination
- Opening / Main / Final / Game Over phases
- MOVE, SEARCH, REST, HIDE, SCOUT, ATTACK, USE_ITEM, WAIT
- Combat, inventory, loot, supply drops, and arena hazards

### Admin dashboard improvements
- Live `ONLINE`, `ACTED`, and `WAITING` counts
- Action-progress bar for the current round
- Searchable player table with selectable rows
- Manual player operations:
  - Eliminate a player
  - Restore a player
  - Set health
  - Set health + attack + speed
  - Move a player to another zone
  - Grant a specific item
- Manual event controls:
  - Supply drop
  - Arena hazard
  - Clear hazards
  - Broadcast an announcement to all players
- Connection indicator for the admin WebSocket

### Reconnection
- Player WebSocket reconnects automatically after a disconnect
- Admin WebSocket reconnects automatically
- Reconnection status is shown to the user/admin
- Reconnected clients receive a fresh authoritative state snapshot
- Refreshing a page does not create a new player as long as the stored session remains valid

### Restart recovery
The live game is still held in memory, but Phase 4 also writes a lightweight checkpoint to:

```text
arena_state.json
```

Configure the path with:

```text
ARENA_STATE_FILE=/path/to/arena_state.json
```

The checkpoint contains the shared game, players, inventory, zones, event log, timers, and player session tokens needed for reconnection.

For safety, the server **does not automatically resume an ACTIVE game after a restart**. It restores the saved match as `PAUSED` and records a `GAME_RECOVERED` event so the admin can review the situation and press Resume.

The checkpoint is written atomically to avoid leaving a half-written JSON file.

## Run the backend

```bash
cd backend
python -m venv .venv

# Windows
.venv\\Scripts\\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# Set a real event token before the event.
# PowerShell example:
# $env:ADMIN_TOKEN="your-strong-admin-token"

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

Defaults:

```text
MAX_PLAYERS=200
ADMIN_TOKEN=change-me
ROUND_DURATION_SECONDS=15
FINAL_PLAYER_THRESHOLD=20
SUPPLY_DROP_INTERVAL=3
HAZARD_INTERVAL=4
HAZARD_DAMAGE=8
MAX_INVENTORY=6
ARENA_STATE_FILE=arena_state.json
```

## Recommended live-event setup

- Use a stable laptop/server connected to the same network as participants.
- Set a non-default `ADMIN_TOKEN`.
- Do not use auto-reload during the event.
- Keep the admin dashboard open on the organizer machine.
- Test the game from several phones before the event.
- Do a 200-client simulation or staged load test before the actual match.
- If the backend restarts, open the admin dashboard, review the recovered `PAUSED` state, and explicitly resume it.

## Notes

The project intentionally remains an event-scale prototype rather than a production MMO. The single shared lock protects state changes, one round loop manages the global timer, and WebSockets are used only for state/event synchronization.
