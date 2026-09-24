# The Arena — Phase 2

Phase 2 turns the Phase 1 lobby prototype into a playable timed arena foundation.

## Added

- Exactly 13 zones: 12 outer zones + central Cornucopia
- Zone graph and movement validation
- Player stats: Health, Attack, Speed
- Starting player placement across the 12 outer zones
- Global round system
- Configurable round timer (default 15 seconds)
- Backend-authoritative action deadlines
- One action per living player per round
- Timeout elimination
- Late-action elimination
- Simple actions: MOVE, SEARCH, REST, HIDE, SCOUT, WAIT
- Opening / Main / Final / Game Over phases
- Final-phase transition at 20 or fewer living players
- Pause/resume with remaining timer preserved
- Admin END ROUND control
- Live zone population dashboard
- Player search table with stats, zone, action, status, connection
- Mobile-friendly player action interface
- Reconnection support
- WebSocket public-event updates without broadcasting full private state to every player on every action
- 200-player registration/load test

## Intentionally reserved for Phase 3

- Combat / ATTACK action
- Damage and defensive mechanics involving other players
- Items and inventory
- Supply drops
- Complex random events
- Advanced elimination interactions


## Recommended event defaults

Use a 15-second round during normal play. Before the event, test a full 200-player run on the same machine/network you plan to use.
