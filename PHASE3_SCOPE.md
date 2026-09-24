# Phase 3 Scope

Phase 3 extends the Phase 2 single-game college event prototype into a basic survival/combat game.

## Added gameplay

- Same 13-zone arena and global timed rounds from Phase 2
- `ATTACK` action against another alive player in the same zone
- Simple damage calculation based on Attack with bounded randomness
- Speed-based dodge chance
- Hidden players get an additional dodge bonus
- 10% critical-hit chance with a small bonus
- Elimination when health reaches zero
- Kill counter credited to the attacker
- `USE_ITEM` action
- Private player inventory
- Five item types:
  - Medkit: +30 health
  - Food: +12 health
  - Weapon: +3 Attack permanently
  - Armor: next incoming hit reduced by 8 damage
  - Speed Boost: +3 Speed permanently
- Search now has a chance to discover items
- Cornucopia starts stocked with 8 items
- Automatic supply drops every 3 rounds
- Automatic arena hazards every 4 rounds
- Hazard damage is applied at round end
- Admin can manually trigger a supply drop or hazard
- Admin sees inventory counts and active hazards

## Important resolution rules

- Players still get exactly one action per round.
- Attack resolution happens when the action reaches the backend, under the shared game-state lock.
- An attack can eliminate a player before that player submits their own action.
- Late actions are rejected and eliminate the player, as in Phase 2.
- Missed actions are eliminated at round resolution.
- Hazard damage is applied after timeout eliminations and before the next round begins.
- A player who becomes the sole survivor immediately ends the game.

## Intentionally not included yet

- Complex combat trees
- Ranged weapons
- Trading
- Teams
- Multiple game instances
- Spectator UI
- Persistent database
- Production authentication
- Advanced animation or graphics
