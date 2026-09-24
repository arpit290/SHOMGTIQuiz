# Phase 4 Scope — Arena RPG

Phase 4 turns the playable prototype into an event-operator-ready application for one shared college-event match.

## Added

### Admin operations
- Live online / acted / waiting counts
- Round action-progress bar
- Searchable 200-player table with row selection
- Player operator tools:
  - Eliminate
  - Restore
  - Set health
  - Set health/attack/speed together
  - Move a player to any zone
  - Grant an item
- Event controls:
  - Manual supply drop
  - Manual hazard
  - Clear all hazards
  - Broadcast an announcement to all connected players
- Existing start / pause / resume / end round / reset controls remain

### Reliability
- Player WebSocket reconnects automatically
- Admin WebSocket reconnects automatically
- Connection status is visible in the UI
- Reconnected players receive a fresh authoritative state snapshot
- The backend keeps one serialized checkpoint of the single game
- Checkpoints are written atomically after meaningful state changes and round resolution
- A game recovered from an active server restart is placed into PAUSED rather than auto-resumed

### Event operation model
- One shared game only
- One central FastAPI game engine
- One action queue/lock model through the existing shared state lock
- No per-player simulation loops
- No spectator application
