# The Arena — Phase 1

A college-event prototype for a single-instance, text-first multiplayer arena game.

## Phase 1 includes

- One shared game instance (`main_game`)
- FastAPI backend
- React + TypeScript frontend (Vite)
- Player registration by name
- Unique player IDs (`P-001`, `P-002`, ...)
- 200-player cap
- Lobby state
- Admin token login
- Admin dashboard
- Real-time player/admin WebSockets
- Connection status tracking
- Live event feed
- Start / pause / resume / reset controls
- Mobile-friendly UI

## Not in Phase 1

Zones, movement, health/attack/speed mechanics, timers, combat, items, elimination rules, and round resolution are intentionally left for Phase 2.

## Run the backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# Optional: set ADMIN_TOKEN before starting.
# PowerShell: $env:ADMIN_TOKEN="my-secret"
# macOS/Linux: export ADMIN_TOKEN="my-secret"

uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Default admin token is `change-me` if you do not set `ADMIN_TOKEN`.

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
npm run dev
```

Open `http://localhost:5173`.

For a college LAN, run both servers on a laptop/server whose LAN IP is reachable by participants, then have participants open `http://YOUR-LAN-IP:5173`. The frontend derives the backend/WebSocket host from the browser hostname, so it does not assume participants are running the backend on their own device.

## Event notes

For a real event, use a strong admin token and run the backend on a machine/server reachable by participants on the same network or through your deployment host.

Phase 1 stores the active game in memory. This is intentional for the prototype; Phase 2 can add restart-safe persistence if needed.
