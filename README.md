# The Arena — Phase 4

A college-event prototype for a **single shared, text-first multiplayer arena game** designed for exactly **24 participants in one match**.

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
- 24-player cap
- 7 zones: 6 hazard zones + central Cornucopia
- Global timed rounds (15 seconds by default)
- Backend-authoritative deadlines
- One action per player per round
- Cornucopia-only item pickup with a dedicated GRAB ITEM action
- Timeout and late-submission elimination
- Opening / Main / Final / Game Over phases
- MOVE, REST, ATTACK, USE_ITEM, WAIT, and Cornucopia-only GRAB ITEM (SCOUT is removed)
- Engaged two-player battles with ATTACK / DEFEND / RUN turns; each completed combat turn counts toward the round and can auto-advance the round
- Combat, inventory, loot, supply drops, and six admin-controlled arena hazards

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
- Live `/spectate` view protected by the admin token

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

## Run the tunnel

```bash
cloudflared.exe tunnel --url http://127.0.0.1:5173 --protocol http2
```

For a college LAN, participants can open the link that the above thing gives us.


The frontend derives the backend/WebSocket host from the browser hostname unless `VITE_API_BASE` / `VITE_WS_BASE` are explicitly set.

## Event configuration

Defaults:

```text
MAX_PLAYERS=24
ADMIN_TOKEN=change-me
ROUND_DURATION_SECONDS=15
BATTLE_TURN_DURATION_SECONDS=15
FINAL_PLAYER_THRESHOLD=20
SUPPLY_DROP_INTERVAL=3
MAX_INVENTORY=1
ARENA_STATE_FILE=arena_state.json
```

## Recommended live-event setup

- Use a stable laptop/server connected to the same network as participants.
- Set a non-default `ADMIN_TOKEN`.
- Do not use auto-reload during the event.
- Keep the admin dashboard open on the organizer machine.
- Test the game from several phones before the event.
- Do a 24-client simulation or staged load test before the actual match.
- If the backend restarts, open the admin dashboard, review the recovered `PAUSED` state, and explicitly resume it.

## Notes

The project intentionally remains an event-scale prototype rather than a production MMO. The single shared lock protects state changes, one round loop manages the global timer, and WebSockets are used only for state/event synchronization.

## Player stats, phone UI and personal feed (latest changes)

- **Registration:** the arena accepts at most **24 players**. During registration, each tribute chooses **M** or **F** alongside their one-HIGH / one-MID / one-LOW stat split. District assignment is server-side: the first male gets District 1, the next male District 2, and so on through District 12; females fill the matching District 1–12 slots independently.
- **Attack** scales damage dealt. **Defense** passively scales damage taken (0.6x at +40%, 1.4x at -40%). **Agility** drives RUN escape chance and scales arena-hazard damage. It replaces the old `speed` stat (the admin `speed` field is still accepted as an alias for agility).
- **Player screen** is a single phone-sized screen with no scrolling; the action controls, inventory, movement, target picker and personal feed stay on the same view.
- **Live feed** only contains events the player did, that happened to them, or arena-wide notices (announcements, game start/pause/resume/over). Opponents' simultaneous battle choices are never leaked.

## One-item bag, phone screen and Hunger Games theme (latest changes)

- **One item at a time.** `MAX_INVENTORY` now defaults to `1`. At the Cornucopia the item you would receive is shown to you (`offeredItem` in the player state). With an empty hand `GRAB_ITEM` takes it; while holding something it becomes a **swap** (your item goes to the back of the pile, so nothing is lost or duplicated). Not pressing SWAP is the "keep" option. Using your item frees the slot.
- **No tabs.** The player screen is a single phone-sized layout: zone + round + timer, an HP bar, your last result, the action buttons, an item box, a target picker, the Cornucopia offer (when there), every adjacent zone as a move button, and a live feed in the bottom third. It was checked in Chromium at 320x480 through 430x900 with no overflow or page scroll.
- **Theme.** Everything except `/admin` is styled in a Hunger Games palette (charred black, Capitol gold, flame orange, ember red, arena green) via a `.hg` class that `Shell` puts on the page for non-admin routes. Admin styling is unchanged. Headings use a Trajan/Cinzel-style serif with system fallbacks.


## Current item pool

The only items in the arena are the following twelve. Players can carry one item at a time; consumables can be used as normal actions or as a battle move, while passive gear is active automatically.

1. **Medkit** — consumable; heals 50% of maximum HP.
2. **Shiny Sword** — +40% Attack.
3. **Golden Apple** — consumable; +50% Attack, Defense and Agility for 5 turns.
4. **Shadow Cloak** — 35% higher dodge chance.
5. **Titan Shield** — 40% less damage in a fight; breaks after 7 uses.
6. **Hunter's Feather** — +50% Agility; +50% damage taken in fights.
7. **Phoenix Ashes** — revives at 35% max HP on death, then is destroyed.
8. **Heart of Iron** — +60% max HP; -50% Agility.
9. **Serpentine Dagger** — 0.75x Attack; successful hits apply the Poisoned effect used by the Poison Fog.
10. **Berserker Gauntlets** — +30% Attack below 50% HP; +75% Attack below 15% HP.
11. **Adventurer's Boots** — move up to two ring areas left/right; 40% less environmental damage.
12. **Crown Of Blood** — each kill while wearing it adds 30% to all stats; it can only be equipped and moved while at the Cornucopia.

## Current arena hazards and round rules

The six outer zones are fixed to these hazards; the admin can activate or deactivate each one during the match:

1. **Lightning Strikes** — every turn you begin here, there is a chance to be hit; Agility affects the outcome and damage.
2. **Tracker Jacker Wasps** — at turn start there is a chance to be stung for 30% damage; Agility affects the outcome and damage.
3. **Blood Rain** — applies FEAR, reducing Attack, Defense and Agility by 30% for five turns.
4. **Poison Fog** — escalates through 7%, 15% and 25% poison damage stages, with the specified warning messages and two-turn poison duration per stage.
5. **Tidal Wave** — a shared three-turn counter gives the vibration warning, distant-wave warning, then a 50% wave hit reduced by Agility before resetting.
6. **Monkey Mutations** — Monkey Mutts attack each turn for a random 5–20% of max HP, reduced by Agility.

A round now advances immediately when every living participant has completed their action. This also applies to battle turns: both combatants must resolve their current combat choice, after which a still-active battle carries into the next round. When the round timer expires instead, unresolved battle choices auto-defend and players who failed to act are eliminated as before.
