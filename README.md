# The Arena — Phase 2

A college-event prototype for a single-instance, text-first multiplayer arena game.

The project is designed for roughly **200 participants in one shared game** rather than multiple games or a production MMORPG architecture.

## Stack

- FastAPI + Python backend
- React + TypeScript + Vite frontend
- WebSockets for real-time updates
- In-memory game state for the live event

## Phase 2 includes

- One shared game (`main_game`)
- 200-player cap
- Player registration and session tokens
- 12 outer zones + central Cornucopia
- Zone graph and validated movement
- Health / Attack / Speed stats
- Global timed rounds (15 seconds by default)
- Backend-authoritative deadlines
- One action per player per round
- Timeout elimination
- Late submission elimination
- MOVE / SEARCH / REST / HIDE / SCOUT / WAIT
- OPENING / MAIN / FINAL / GAME_OVER phases
- Admin pause / resume / end-round / reset controls
- Live admin zone population overview
- Searchable admin player table
- Mobile-friendly player game view
- Reconnection handling

## Phase 3 will add

Combat, attacks, damage rules, items, inventory, supply drops, and richer random events.

## Run the backend

```bash
cd backend
python -m venv .venv

# Windows
.venv\\Scripts\\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# Optional: configure the event
# PowerShell:
# $env:ADMIN_TOKEN="your-strong-admin-token"
# $env:ROUND_DURATION_SECONDS="15"
# $env:FINAL_PLAYER_THRESHOLD="20"

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

For a college LAN, participants should open:

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
ADMIN_TOKEN=change-me
```

The active game is intentionally held in memory. This is appropriate for the event prototype, but the game will reset if the backend process restarts. For the actual event, keep the backend process on a stable host and avoid auto-reload.

## Important gameplay rule

The frontend timer is only a display. The FastAPI backend owns the actual round deadline, so changing browser time or disabling the JavaScript timer cannot extend a player's turn.

## Architecture

```text
React player clients (~200)
        |
        | REST + WebSocket
        v
     FastAPI
        |
        v
    Game State
        |
   Game Engine rules
   /      |       \\
movement rounds  actions
        |
        v
 Admin Dashboard
```
