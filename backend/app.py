from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from enum import Enum
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "250"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
ROUND_DURATION_SECONDS = max(5, int(os.getenv("ROUND_DURATION_SECONDS", "30")))
BATTLE_TURN_DURATION_SECONDS = max(5, int(os.getenv("BATTLE_TURN_DURATION_SECONDS", "15")))
FINAL_PLAYER_THRESHOLD = max(2, int(os.getenv("FINAL_PLAYER_THRESHOLD", "20")))
SUPPLY_DROP_INTERVAL = max(1, int(os.getenv("SUPPLY_DROP_INTERVAL", "3")))
HAZARD_INTERVAL = max(1, int(os.getenv("HAZARD_INTERVAL", "4")))
HAZARD_DAMAGE = max(1, int(os.getenv("HAZARD_DAMAGE", "8")))
MAX_INVENTORY = max(1, int(os.getenv("MAX_INVENTORY", "6")))
STATE_FILE = Path(os.getenv("ARENA_STATE_FILE", "arena_state.json"))

rng = random.Random()


# ---------------------------------------------------------------------------
# Game rules / arena definition
# ---------------------------------------------------------------------------

class GameStatus(str, Enum):
    LOBBY = "LOBBY"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    GAME_OVER = "GAME_OVER"


class GamePhase(str, Enum):
    OPENING = "OPENING"
    MAIN = "MAIN"
    FINAL = "FINAL"
    GAME_OVER = "GAME_OVER"


@dataclass(frozen=True)
class ZoneDefinition:
    id: str
    name: str
    description: str
    connected_zones: tuple[str, ...]


@dataclass(frozen=True)
class ItemDefinition:
    type: str
    name: str
    description: str


@dataclass
class Item:
    id: str
    type: str
    name: str
    description: str

    def dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "description": self.description,
        }


@dataclass
class Battle:
    id: str
    player_a_id: str
    player_b_id: str
    started_round: int
    turn_number: int = 1
    actions: dict[str, str] = field(default_factory=dict)
    deadline: datetime | None = None
    paused_remaining_seconds: float | None = None

    def participant_ids(self) -> tuple[str, str]:
        return self.player_a_id, self.player_b_id


ITEM_DEFINITIONS: dict[str, ItemDefinition] = {
    "MEDKIT": ItemDefinition("MEDKIT", "Medkit", "Restore 30 health."),
    "FOOD": ItemDefinition("FOOD", "Food", "Restore 12 health."),
    "WEAPON": ItemDefinition("WEAPON", "Weapon", "Use once to permanently gain +3 Attack."),
    "ARMOR": ItemDefinition("ARMOR", "Armor", "Use to become Armored until your next round; the next hit against you is reduced by 8 damage."),
    "SPEED_BOOST": ItemDefinition("SPEED_BOOST", "Speed Boost", "Use once to permanently gain +3 Speed."),
}


# Twelve outer zones form a ring; the Cornucopia is the central hub.
OUTER_ZONE_IDS = tuple(f"zone_{index}" for index in range(1, 13))


def build_zones() -> dict[str, ZoneDefinition]:
    zones: dict[str, ZoneDefinition] = {}
    total = len(OUTER_ZONE_IDS)

    descriptions = {
        1: "Dry woodland with long sight lines.",
        2: "Dense brush and broken ground.",
        3: "A narrow ridge overlooking the arena.",
        4: "Tall grass and scattered cover.",
        5: "A shaded forest with limited visibility.",
        6: "Rocky terrain and a shallow ravine.",
        7: "Open scrubland with little cover.",
        8: "A damp woodland close to the arena edge.",
        9: "A quiet clearing surrounded by trees.",
        10: "Uneven ground with several natural hiding spots.",
        11: "A sparse forest crossed by a narrow trail.",
        12: "A wind-exposed zone near the arena boundary.",
    }

    for index, zone_id in enumerate(OUTER_ZONE_IDS, start=1):
        previous_id = OUTER_ZONE_IDS[(index - 2) % total]
        next_id = OUTER_ZONE_IDS[index % total]
        zones[zone_id] = ZoneDefinition(
            id=zone_id,
            name=f"Zone {index}",
            description=descriptions[index],
            connected_zones=(previous_id, next_id, "cornucopia"),
        )

    zones["cornucopia"] = ZoneDefinition(
        id="cornucopia",
        name="Cornucopia",
        description="The central arena structure and its surrounding open ground.",
        connected_zones=OUTER_ZONE_IDS,
    )
    return zones


ZONES = build_zones()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Player:
    id: str
    name: str
    health: int = 100
    max_health: int = 100
    attack: int = 10
    speed: int = 10
    zone_id: str = "zone_1"
    alive: bool = True
    connected: bool = False
    current_action: str | None = None
    action_taken: bool = False
    action_deadline: datetime | None = None
    status_effect: str = "NORMAL"
    last_result: str = "Waiting for the game to begin."
    joined_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    session_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    kills: int = 0
    inventory: list[Item] = field(default_factory=list)
    battle_id: str | None = None
    battle_opponent_id: str | None = None
    battle_action: str | None = None

    def inventory_dict(self) -> list[dict[str, str]]:
        return [item.dict() for item in self.inventory]

    def public_dict(self) -> dict[str, Any]:
        # This object is only sent to the player themselves (or the admin).
        return {
            "id": self.id,
            "name": self.name,
            "health": self.health,
            "maxHealth": self.max_health,
            "attack": self.attack,
            "speed": self.speed,
            "zoneId": self.zone_id,
            "zoneName": ZONES[self.zone_id].name,
            "alive": self.alive,
            "connected": self.connected,
            "currentAction": self.current_action,
            "actionTaken": self.action_taken,
            "actionDeadline": self.action_deadline.isoformat() if self.action_deadline else None,
            "statusEffect": self.status_effect,
            "lastResult": self.last_result,
            "kills": self.kills,
            "inventory": self.inventory_dict(),
            "battleId": self.battle_id,
            "battleOpponentId": self.battle_opponent_id,
            "battleAction": self.battle_action,
        }

    def admin_dict(self) -> dict[str, Any]:
        return {
            **self.public_dict(),
            "joinedAt": self.joined_at,
        }


@dataclass
class GameState:
    game_id: str = "main_game"
    status: GameStatus = GameStatus.LOBBY
    phase: GamePhase = GamePhase.OPENING
    round_number: int = 0
    round_deadline: datetime | None = None
    paused_remaining_seconds: float | None = None
    winner_id: str | None = None
    players: dict[str, Player] = field(default_factory=dict)
    event_log: list[dict[str, Any]] = field(default_factory=list)
    zone_items: dict[str, list[Item]] = field(default_factory=lambda: {zone_id: [] for zone_id in ZONES})
    hazard_zones: set[str] = field(default_factory=set)
    battles: dict[str, Battle] = field(default_factory=dict)

    @property
    def alive_count(self) -> int:
        return sum(player.alive for player in self.players.values())

    @property
    def zone_counts(self) -> dict[str, int]:
        counts = {zone_id: 0 for zone_id in ZONES}
        for player in self.players.values():
            if player.alive:
                counts[player.zone_id] += 1
        return counts

    def add_event(self, message: str, event_type: str = "INFO") -> dict[str, Any]:
        event = {
            "id": secrets.token_hex(6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "message": message,
        }
        self.event_log.append(event)
        if len(self.event_log) > 2000:
            del self.event_log[:-2000]
        return event


# One shared game for the entire college event.
game = GameState()
state_lock = asyncio.Lock()
round_task: asyncio.Task[None] | None = None


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self.player_connections: dict[str, set[WebSocket]] = {}
        self.admin_connections: set[WebSocket] = set()
        self.spectator_connections: set[WebSocket] = set()

    async def connect_player(self, player_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.player_connections.setdefault(player_id, set()).add(websocket)

    def disconnect_player(self, player_id: str, websocket: WebSocket) -> bool:
        connections = self.player_connections.get(player_id)
        if not connections:
            return False
        connections.discard(websocket)
        if connections:
            return False
        self.player_connections.pop(player_id, None)
        return True

    async def connect_admin(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.admin_connections.add(websocket)

    def disconnect_admin(self, websocket: WebSocket) -> None:
        self.admin_connections.discard(websocket)

    async def connect_spectator(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.spectator_connections.add(websocket)

    def disconnect_spectator(self, websocket: WebSocket) -> None:
        self.spectator_connections.discard(websocket)

    async def send_player(self, player_id: str, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.player_connections.get(player_id, set())):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_player(player_id, ws)

    async def broadcast_players(self, payload: dict[str, Any]) -> None:
        targets = [ws for sockets in self.player_connections.values() for ws in sockets]
        dead: list[tuple[str, WebSocket]] = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                owner = next((pid for pid, sockets in self.player_connections.items() if ws in sockets), None)
                if owner:
                    dead.append((owner, ws))
        for player_id, ws in dead:
            self.disconnect_player(player_id, ws)

    async def close_all_players(self, payload: dict[str, Any] | None = None) -> None:
        sockets = [ws for connections in self.player_connections.values() for ws in connections]
        if payload is not None:
            for ws in sockets:
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass
        for ws in sockets:
            try:
                await ws.close(code=1000)
            except Exception:
                pass
        self.player_connections.clear()

    async def broadcast_admin(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.admin_connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_admin(ws)

    async def broadcast_spectators(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.spectator_connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_spectator(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# FastAPI setup
# ---------------------------------------------------------------------------

async def round_loop() -> None:
    """Single lightweight loop that advances the shared event timer."""
    while True:
        await asyncio.sleep(0.20)
        should_sync = False
        async with state_lock:
            if game.status == GameStatus.ACTIVE:
                battle_expired = _resolve_expired_battles_locked()
                round_expired = (
                    game.round_deadline is not None
                    and datetime.now(timezone.utc) >= game.round_deadline
                )
                if battle_expired or round_expired:
                    if round_expired and game.status == GameStatus.ACTIVE:
                        _resolve_round_locked()
                    _write_checkpoint_locked()
                    should_sync = True

        if should_sync:
            await sync_everyone_full()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global round_task
    async with state_lock:
        _restore_from_checkpoint()
        _write_checkpoint_locked()
    round_task = asyncio.create_task(round_loop())
    yield
    if round_task:
        round_task.cancel()
        try:
            await round_task
        except asyncio.CancelledError:
            pass
        round_task = None


app = FastAPI(title="Arena RPG API", version="0.4.0", lifespan=lifespan)
cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class JoinRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class AdminLoginRequest(BaseModel):
    token: str


class AdminActionRequest(BaseModel):
    action: str
    targetZoneId: str | None = None
    targetPlayerId: str | None = None
    itemType: str | None = None
    message: str | None = Field(default=None, max_length=240)
    reason: str | None = Field(default=None, max_length=160)
    value: int | None = Field(default=None, ge=0, le=500)
    attack: int | None = Field(default=None, ge=1, le=100)
    speed: int | None = Field(default=None, ge=1, le=100)


class PlayerActionRequest(BaseModel):
    playerId: str = Field(min_length=1, max_length=16)
    action: str
    targetZoneId: str | None = None
    targetPlayerId: str | None = None
    itemId: str | None = None


# ---------------------------------------------------------------------------
# Game engine helpers
# ---------------------------------------------------------------------------

ACTION_SET = {"MOVE", "REST", "SCOUT", "ATTACK", "USE_ITEM", "WAIT", "GRAB_ITEM"}
BATTLE_ACTION_SET = {"BATTLE_ATTACK", "BATTLE_DEFEND", "BATTLE_RUN"}


def clean_name(name: str) -> str:
    return " ".join(name.strip().split())


def next_player_id() -> str:
    index = len(game.players) + 1
    while f"P-{index:03d}" in game.players:
        index += 1
    return f"P-{index:03d}"


def make_item(item_type: str) -> Item:
    definition = ITEM_DEFINITIONS[item_type]
    return Item(
        id=f"I-{secrets.token_hex(5)}",
        type=definition.type,
        name=definition.name,
        description=definition.description,
    )


def random_loot_type() -> str:
    return rng.choices(
        population=["FOOD", "MEDKIT", "WEAPON", "ARMOR", "SPEED_BOOST"],
        weights=[28, 24, 20, 12, 16],
        k=1,
    )[0]


def nearby_opponents(player: Player) -> list[Player]:
    return [
        other
        for other in game.players.values()
        if other.alive and other.id != player.id and other.zone_id == player.zone_id
    ]


def get_battle_locked(player: Player) -> Battle | None:
    if not player.battle_id:
        return None
    battle = game.battles.get(player.battle_id)
    if not battle:
        player.battle_id = None
        player.battle_opponent_id = None
        player.battle_action = None
    return battle


def battle_dict(battle: Battle | None, *, viewer_id: str | None = None) -> dict[str, Any] | None:
    if battle is None:
        return None
    payload = {
        "id": battle.id,
        "playerAId": battle.player_a_id,
        "playerBId": battle.player_b_id,
        "turn": battle.turn_number,
        "deadline": battle.deadline.isoformat() if battle.deadline else None,
        "startedRound": battle.started_round,
        "yourAction": battle.actions.get(viewer_id) if viewer_id else None,
        "opponentActionSubmitted": bool(viewer_id and any(pid != viewer_id for pid in battle.actions)),
    }
    if viewer_id is None:
        payload["actions"] = dict(battle.actions)
    return payload


def visible_opponent_dict(player: Player) -> list[dict[str, Any]]:
    return [
        {
            "id": other.id,
            "name": other.name,
            "statusEffect": other.status_effect,
            "health": other.health,
            "maxHealth": other.max_health,
        }
        for other in nearby_opponents(player)
    ]


def available_actions(player: Player) -> list[str]:
    if not player.alive or game.status != GameStatus.ACTIVE:
        return []

    battle = get_battle_locked(player)
    if battle:
        if player.battle_action:
            return []
        return sorted(BATTLE_ACTION_SET)

    if player.action_taken:
        return []

    actions = {"MOVE", "REST", "SCOUT", "WAIT"}
    if nearby_opponents(player):
        actions.add("ATTACK")
    if player.inventory:
        actions.add("USE_ITEM")
    if player.zone_id == "cornucopia" and game.zone_items["cornucopia"] and len(player.inventory) < MAX_INVENTORY:
        actions.add("GRAB_ITEM")
    return sorted(actions)


def public_zone_dict(zone: ZoneDefinition, *, include_counts: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": zone.id,
        "name": zone.name,
        "description": zone.description,
        "connectedZones": list(zone.connected_zones),
    }
    if include_counts:
        payload["playerCount"] = game.zone_counts[zone.id]
        payload["lootCount"] = len(game.zone_items[zone.id])
        payload["hazard"] = zone.id in game.hazard_zones
    return payload


def player_state(player: Player) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    battle = get_battle_locked(player)
    return {
        "gameId": game.game_id,
        "status": game.status.value,
        "phase": game.phase.value,
        "round": game.round_number,
        "roundDurationSeconds": ROUND_DURATION_SECONDS,
        "battleTurnDurationSeconds": BATTLE_TURN_DURATION_SECONDS,
        "serverNow": now.isoformat(),
        "roundDeadline": game.round_deadline.isoformat() if game.round_deadline else None,
        "player": player.public_dict(),
        "availableActions": available_actions(player),
        "currentZone": public_zone_dict(ZONES[player.zone_id]),
        "adjacentZones": [public_zone_dict(ZONES[zone_id]) for zone_id in ZONES[player.zone_id].connected_zones],
        "visibleOpponents": visible_opponent_dict(player),
        "zoneLootCount": len(game.zone_items[player.zone_id]),
        "zoneHazard": player.zone_id in game.hazard_zones,
        "hazardDamage": HAZARD_DAMAGE,
        "playerCount": len(game.players),
        "aliveCount": game.alive_count,
        "maxPlayers": MAX_PLAYERS,
        "winnerId": game.winner_id,
        "events": game.event_log[-30:],
        "battle": battle_dict(battle, viewer_id=player.id),
    }


def admin_state() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    alive_players = [p for p in game.players.values() if p.alive]
    acted_count = sum(1 for player in alive_players if player.action_taken)
    online_count = sum(1 for player in game.players.values() if player.connected)
    return {
        "gameId": game.game_id,
        "status": game.status.value,
        "phase": game.phase.value,
        "round": game.round_number,
        "roundDurationSeconds": ROUND_DURATION_SECONDS,
        "serverNow": now.isoformat(),
        "roundDeadline": game.round_deadline.isoformat() if game.round_deadline else None,
        "playerCount": len(game.players),
        "aliveCount": game.alive_count,
        "onlineCount": online_count,
        "actedCount": acted_count,
        "waitingCount": max(0, game.alive_count - acted_count),
        "maxPlayers": MAX_PLAYERS,
        "winnerId": game.winner_id,
        "zones": [public_zone_dict(zone, include_counts=True) for zone in ZONES.values()],
        "players": [p.admin_dict() for p in game.players.values()],
        "events": game.event_log[-120:],
        "battles": [battle_dict(battle) for battle in game.battles.values()],
    }


def spectate_state() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    players = []
    for player in game.players.values():
        players.append({
            **player.public_dict(),
            "battle": battle_dict(get_battle_locked(player)),
        })
    return {
        "gameId": game.game_id,
        "status": game.status.value,
        "phase": game.phase.value,
        "round": game.round_number,
        "roundDurationSeconds": ROUND_DURATION_SECONDS,
        "battleTurnDurationSeconds": BATTLE_TURN_DURATION_SECONDS,
        "serverNow": now.isoformat(),
        "roundDeadline": game.round_deadline.isoformat() if game.round_deadline else None,
        "playerCount": len(game.players),
        "aliveCount": game.alive_count,
        "maxPlayers": MAX_PLAYERS,
        "winnerId": game.winner_id,
        "zones": [public_zone_dict(zone, include_counts=True) for zone in ZONES.values()],
        "players": players,
        "battles": [battle_dict(battle) for battle in game.battles.values()],
        "events": game.event_log[-160:],
    }



def _state_dict_locked() -> dict[str, Any]:
    return {
        "gameId": game.game_id,
        "status": game.status.value,
        "phase": game.phase.value,
        "round": game.round_number,
        "roundDeadline": game.round_deadline.isoformat() if game.round_deadline else None,
        "pausedRemainingSeconds": game.paused_remaining_seconds,
        "winnerId": game.winner_id,
        "eventLog": game.event_log[-2000:],
        "hazardZones": sorted(game.hazard_zones),
        "battles": [
            {
                "id": battle.id,
                "playerAId": battle.player_a_id,
                "playerBId": battle.player_b_id,
                "startedRound": battle.started_round,
                "turnNumber": battle.turn_number,
                "actions": dict(battle.actions),
                "deadline": battle.deadline.isoformat() if battle.deadline else None,
                "pausedRemainingSeconds": battle.paused_remaining_seconds,
            }
            for battle in game.battles.values()
        ],
        "zoneItems": {
            zone_id: [item.dict() for item in items]
            for zone_id, items in game.zone_items.items()
        },
        "players": [
            {
                **player.admin_dict(),
                "sessionToken": player.session_token,
            }
            for player in game.players.values()
        ],
    }


def _write_checkpoint_locked() -> None:
    """Write the single-game checkpoint atomically so an event restart can recover safely."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(_state_dict_locked(), indent=2), encoding="utf-8")
    os.replace(temporary, STATE_FILE)


def _restore_from_checkpoint() -> None:
    """Load the last checkpoint. Never auto-resume a live round after a server restart."""
    if not STATE_FILE.exists():
        return

    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    game.players.clear()
    for items in game.zone_items.values():
        items.clear()
    game.event_log.clear()
    game.hazard_zones.clear()
    game.battles.clear()

    try:
        game.status = GameStatus(raw.get("status", GameStatus.LOBBY.value))
        game.phase = GamePhase(raw.get("phase", GamePhase.OPENING.value))
    except ValueError:
        game.status = GameStatus.LOBBY
        game.phase = GamePhase.OPENING

    game.round_number = int(raw.get("round", 0) or 0)
    game.winner_id = raw.get("winnerId")
    game.event_log.extend(raw.get("eventLog", [])[-2000:])
    game.hazard_zones.update(zone_id for zone_id in raw.get("hazardZones", []) if zone_id in ZONES)

    for zone_id, serialized_items in raw.get("zoneItems", {}).items():
        if zone_id not in game.zone_items:
            continue
        for data in serialized_items:
            try:
                definition = ITEM_DEFINITIONS[data["type"]]
                game.zone_items[zone_id].append(
                    Item(
                        id=str(data["id"]),
                        type=definition.type,
                        name=definition.name,
                        description=definition.description,
                    )
                )
            except (KeyError, TypeError):
                continue

    for data in raw.get("players", []):
        try:
            joined_at = str(data.get("joinedAt") or datetime.now(timezone.utc).isoformat())
            player = Player(
                id=str(data["id"]),
                name=str(data["name"]),
                health=int(data.get("health", 100)),
                max_health=int(data.get("maxHealth", 100)),
                attack=int(data.get("attack", 10)),
                speed=int(data.get("speed", 10)),
                zone_id=str(data.get("zoneId", "zone_1")),
                alive=bool(data.get("alive", True)),
                connected=False,
                current_action=data.get("currentAction"),
                action_taken=bool(data.get("actionTaken", False)),
                action_deadline=None,
                status_effect=str(data.get("statusEffect", "NORMAL")),
                last_result=str(data.get("lastResult", "Recovered from the previous server session.")),
                joined_at=joined_at,
                session_token=str(data.get("sessionToken") or secrets.token_urlsafe(24)),
                kills=int(data.get("kills", 0)),
                battle_id=data.get("battleId"),
                battle_opponent_id=data.get("battleOpponentId"),
                battle_action=data.get("battleAction"),
            )
            if player.zone_id not in ZONES:
                player.zone_id = "zone_1"
            for item_data in data.get("inventory", []):
                item_type = item_data.get("type")
                if item_type in ITEM_DEFINITIONS:
                    definition = ITEM_DEFINITIONS[item_type]
                    player.inventory.append(Item(
                        id=str(item_data["id"]),
                        type=definition.type,
                        name=definition.name,
                        description=definition.description,
                    ))
            game.players[player.id] = player
        except (KeyError, TypeError, ValueError):
            continue

    for data in raw.get("battles", []):
        try:
            battle = Battle(
                id=str(data["id"]),
                player_a_id=str(data["playerAId"]),
                player_b_id=str(data["playerBId"]),
                started_round=int(data.get("startedRound", game.round_number or 1)),
                turn_number=int(data.get("turnNumber", 1)),
                actions={str(pid): str(action) for pid, action in (data.get("actions") or {}).items()},
                deadline=None,
                paused_remaining_seconds=(float(data["pausedRemainingSeconds"]) if data.get("pausedRemainingSeconds") is not None else None),
            )
            if battle.player_a_id in game.players and battle.player_b_id in game.players:
                game.battles[battle.id] = battle
        except (KeyError, TypeError, ValueError):
            continue

    for player in game.players.values():
        if player.battle_id not in game.battles:
            player.battle_id = None
            player.battle_opponent_id = None
            player.battle_action = None

    saved_deadline = raw.get("roundDeadline")
    game.round_deadline = None
    game.paused_remaining_seconds = None

    if game.status == GameStatus.ACTIVE:
        try:
            deadline = datetime.fromisoformat(saved_deadline) if saved_deadline else None
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds() if deadline else ROUND_DURATION_SECONDS
        except (TypeError, ValueError):
            remaining = ROUND_DURATION_SECONDS
        game.paused_remaining_seconds = max(1.0, min(float(remaining), float(ROUND_DURATION_SECONDS)))
        game.status = GameStatus.PAUSED
        for player in game.players.values():
            player.connected = False
            player.action_deadline = None
        game.add_event(
            "The arena was recovered after a server restart and is paused for admin review.",
            "GAME_RECOVERED",
        )
        for battle in game.battles.values():
            battle.deadline = None
    elif game.status == GameStatus.PAUSED:
        game.paused_remaining_seconds = float(raw.get("pausedRemainingSeconds") or ROUND_DURATION_SECONDS)
        for player in game.players.values():
            player.connected = False
            player.action_deadline = None
        for battle in game.battles.values():
            battle.deadline = None
    else:
        for player in game.players.values():
            player.connected = False
            player.action_deadline = None
        game.battles.clear()


def assign_starting_zones_locked() -> None:
    game.battles.clear()
    for index, player in enumerate(game.players.values()):
        player.zone_id = OUTER_ZONE_IDS[index % len(OUTER_ZONE_IDS)]
        player.health = player.max_health
        player.attack = 10
        player.speed = 10
        player.alive = True
        player.action_taken = False
        player.current_action = None
        player.action_deadline = None
        player.status_effect = "NORMAL"
        player.last_result = f"You begin in {ZONES[player.zone_id].name}."
        player.kills = 0
        player.inventory.clear()
        player.battle_id = None
        player.battle_opponent_id = None
        player.battle_action = None

    for zone_id in game.zone_items:
        game.zone_items[zone_id].clear()
    game.hazard_zones.clear()

    # Small opening cache so the central area is worth contesting early.
    for _ in range(8):
        game.zone_items["cornucopia"].append(make_item(random_loot_type()))
    game.add_event("The Cornucopia has been stocked with supplies.", "SUPPLY_DROP")


def _set_phase_locked() -> None:
    if game.alive_count <= 1:
        game.phase = GamePhase.GAME_OVER
    elif game.round_number <= 1:
        game.phase = GamePhase.OPENING
    elif game.alive_count <= FINAL_PLAYER_THRESHOLD:
        game.phase = GamePhase.FINAL
    else:
        game.phase = GamePhase.MAIN


def _spawn_supply_drops_locked() -> None:
    eligible = [zone_id for zone_id in OUTER_ZONE_IDS if zone_id not in game.hazard_zones]
    if not eligible:
        eligible = list(OUTER_ZONE_IDS)
    drop_count = 2 if game.phase == GamePhase.FINAL else 3
    chosen_zones = rng.sample(eligible, k=min(drop_count, len(eligible)))
    for zone_id in chosen_zones:
        item_count = rng.randint(1, 2)
        for _ in range(item_count):
            game.zone_items[zone_id].append(make_item(random_loot_type()))
    names = ", ".join(ZONES[zone_id].name for zone_id in chosen_zones)
    game.add_event(f"Supply drops have landed in {names}.", "SUPPLY_DROP")


def _spawn_hazards_locked() -> None:
    game.hazard_zones.clear()
    eligible = list(OUTER_ZONE_IDS)
    hazard_count = 2 if game.phase == GamePhase.FINAL else 1
    for zone_id in rng.sample(eligible, k=min(hazard_count, len(eligible))):
        game.hazard_zones.add(zone_id)
    names = ", ".join(ZONES[zone_id].name for zone_id in game.hazard_zones)
    game.add_event(
        f"Arena hazard active in {names}. Players there will take {HAZARD_DAMAGE} damage at round end.",
        "ARENA_HAZARD",
    )


def _begin_round_locked() -> None:
    if game.alive_count <= 1:
        _finish_game_locked()
        return

    game.round_deadline = datetime.now(timezone.utc) + timedelta(seconds=ROUND_DURATION_SECONDS)
    game.paused_remaining_seconds = None
    _set_phase_locked()

    for player in game.players.values():
        if player.alive:
            if get_battle_locked(player):
                player.current_action = "BATTLE"
                player.action_taken = True
                player.action_deadline = game.battles[player.battle_id].deadline if player.battle_id in game.battles else game.round_deadline
                if player.status_effect == "ARMORED":
                    player.status_effect = "NORMAL"
                player.last_result = f"Battle with {game.players[player.battle_opponent_id].name} continues. Choose a combat move." if player.battle_opponent_id in game.players else "Your battle continues."
                continue
            player.current_action = None
            player.action_taken = False
            player.action_deadline = game.round_deadline
            if player.status_effect == "ARMORED":
                # Armor lasts through the round in which it was used; it is cleared
                # at the next round start if it survived unused.
                player.status_effect = "NORMAL"
            player.last_result = f"Round {game.round_number} has begun. Choose your action."
        else:
            player.action_deadline = None

    if game.round_number > 1 and game.round_number % SUPPLY_DROP_INTERVAL == 0:
        _spawn_supply_drops_locked()
    if game.round_number > 1 and game.round_number % HAZARD_INTERVAL == 0:
        _spawn_hazards_locked()
    else:
        game.hazard_zones.clear()

    game.add_event(
        f"Round {game.round_number} has begun. Players have {ROUND_DURATION_SECONDS} seconds to act.",
        "ROUND_STARTED",
    )


def _clear_battle_players_locked(battle: Battle, *, ended_action_round: bool) -> None:
    for player_id in battle.participant_ids():
        player = game.players.get(player_id)
        if not player:
            continue
        player.battle_id = None
        player.battle_opponent_id = None
        player.battle_action = None
        player.current_action = None
        player.action_deadline = game.round_deadline if game.status == GameStatus.ACTIVE else None
        player.action_taken = ended_action_round


def _end_battle_locked(battle_id: str, *, reason: str | None = None) -> None:
    battle = game.battles.pop(battle_id, None)
    if not battle:
        return
    ended_action_round = game.round_number == battle.started_round
    _clear_battle_players_locked(battle, ended_action_round=ended_action_round)
    if reason:
        game.add_event(reason, "BATTLE_ENDED")


def _finish_game_locked() -> None:
    alive_players = [p for p in game.players.values() if p.alive]
    if len(alive_players) == 1:
        game.winner_id = alive_players[0].id
        alive_players[0].last_result = "You are the last player standing."
        game.add_event(f"{alive_players[0].name} is the last player standing.", "GAME_OVER")
    else:
        game.winner_id = None
        game.add_event("The game has ended with no player left standing.", "GAME_OVER")
    game.status = GameStatus.GAME_OVER
    game.phase = GamePhase.GAME_OVER
    game.round_deadline = None
    game.hazard_zones.clear()
    game.battles.clear()

    for player in game.players.values():
        player.action_deadline = None
        player.action_taken = True
        player.battle_id = None
        player.battle_opponent_id = None
        player.battle_action = None
        player.current_action = None


def _eliminate_player_locked(player: Player, reason: str, *, killer: Player | None = None) -> dict[str, Any]:
    if not player.alive:
        return game.add_event(f"{player.name} was already eliminated.", "INFO")
    player.alive = False
    player.health = 0
    player.action_deadline = None
    player.action_taken = True
    player.current_action = None
    player.status_effect = "ELIMINATED"
    player.last_result = reason
    if player.battle_id:
        _end_battle_locked(
            player.battle_id,
            reason=f"The battle involving {player.name} ended because a combatant was eliminated.",
        )
    if killer is not None and killer.id != player.id:
        killer.kills += 1
    return game.add_event(reason, "PLAYER_ELIMINATED")


def _apply_hazard_damage_locked() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not game.hazard_zones:
        return events

    for player in list(game.players.values()):
        if not player.alive or player.zone_id not in game.hazard_zones:
            continue
        old_health = player.health
        player.health = max(0, player.health - HAZARD_DAMAGE)
        player.last_result = f"The hazard in {ZONES[player.zone_id].name} dealt {old_health - player.health} damage."
        events.append(game.add_event(
            f"{player.name} took {old_health - player.health} hazard damage in {ZONES[player.zone_id].name}.",
            "HAZARD_DAMAGE",
        ))
        if player.health <= 0:
            events.append(_eliminate_player_locked(
                player,
                f"{player.name} ({player.id}) was eliminated by the arena hazard in {ZONES[player.zone_id].name}.",
            ))
    return events


def _resolve_round_locked() -> None:
    if game.status != GameStatus.ACTIVE:
        return

    missed: list[Player] = [
        player
        for player in game.players.values()
        if player.alive and not player.action_taken and not player.battle_id
    ]

    for player in missed:
        _eliminate_player_locked(
            player,
            f"{player.name} ({player.id}) failed to act before the timer expired and was eliminated.",
        )

    _apply_hazard_damage_locked()

    if game.alive_count <= 1:
        _finish_game_locked()
        return

    game.round_number += 1
    _begin_round_locked()


def _validate_player_action_locked(
    player: Player,
    action: str,
    target_zone_id: str | None,
    target_player_id: str | None,
    item_id: str | None,
) -> tuple[bool, str]:
    action = action.upper()

    if game.status != GameStatus.ACTIVE:
        return False, "The game is not currently accepting player actions."
    if not player.alive:
        return False, "You have been eliminated."

    battle = get_battle_locked(player)
    if battle:
        if action not in BATTLE_ACTION_SET:
            return False, "You are engaged in battle. Only ATTACK, DEFEND, or RUN are available."
        if player.battle_action:
            return False, "You have already chosen a combat move for this battle turn."
        if battle.deadline and datetime.now(timezone.utc) >= battle.deadline:
            _resolve_expired_battles_locked()
            return False, "That battle turn has already resolved."
        return True, ""

    if player.action_taken:
        return False, "You have already acted this round."
    if game.round_deadline and datetime.now(timezone.utc) >= game.round_deadline:
        _eliminate_player_locked(
            player,
            f"{player.name} ({player.id}) submitted after the timer expired and was eliminated.",
        )
        return False, "The timer expired. You have been eliminated."
    if action not in ACTION_SET:
        return False, "That action is not available."

    if action == "MOVE":
        if not target_zone_id or target_zone_id not in ZONES:
            return False, "Choose a destination zone."
        if target_zone_id not in ZONES[player.zone_id].connected_zones:
            return False, "You cannot move directly to that zone."

    if action == "ATTACK":
        if not target_player_id:
            return False, "Choose a player to attack."
        target = game.players.get(target_player_id)
        if not target or not target.alive:
            return False, "That player is not available."
        if target.id == player.id:
            return False, "You cannot attack yourself."
        if target.zone_id != player.zone_id:
            return False, "That player is no longer in your zone."
        if target.battle_id:
            return False, "That player is already engaged in battle."

    if action == "GRAB_ITEM":
        if player.zone_id != "cornucopia":
            return False, "You can only grab items at the Cornucopia."
        if not game.zone_items["cornucopia"]:
            return False, "The Cornucopia is empty."
        if len(player.inventory) >= MAX_INVENTORY:
            return False, "Your inventory is full."

    if action == "USE_ITEM":
        if not item_id:
            return False, "Choose an item to use."
        item = next((candidate for candidate in player.inventory if candidate.id == item_id), None)
        if item is None:
            return False, "That item is no longer in your inventory."
        if item.type == "MEDKIT" and player.health >= player.max_health:
            return False, "Your health is already full."
        if item.type == "FOOD" and player.health >= player.max_health:
            return False, "Your health is already full."

    return True, ""


def _attack_damage_locked(attacker: Player, defender: Player, *, defense: bool = False) -> tuple[int, bool, int]:
    speed_edge = max(-3, min(6, (attacker.speed - defender.speed) // 3))
    raw = max(1, attacker.attack + rng.randint(-2, 4) + speed_edge)
    critical = rng.random() < 0.12
    if critical:
        raw += max(3, attacker.attack // 2)

    reduction = 0
    if defense:
        reduction += max(5, int(raw * 0.55))
    if defender.status_effect == "ARMORED":
        reduction += 8
        defender.status_effect = "NORMAL"

    return max(1, raw - reduction), critical, reduction


def _resolve_battle_turn_locked(battle: Battle) -> list[dict[str, Any]]:
    player_a = game.players.get(battle.player_a_id)
    player_b = game.players.get(battle.player_b_id)
    if not player_a or not player_b or not player_a.alive or not player_b.alive:
        _end_battle_locked(battle.id)
        return []

    action_a = battle.actions.get(player_a.id)
    action_b = battle.actions.get(player_b.id)
    if not action_a or not action_b:
        return []

    events: list[dict[str, Any]] = []
    if action_a == "BATTLE_RUN" or action_b == "BATTLE_RUN":
        runners = [(player_a, player_b, action_a), (player_b, player_a, action_b)]
        for runner, opponent, runner_action in runners:
            if runner_action != "BATTLE_RUN":
                continue
            chance = max(0.20, min(0.85, 0.50 + (runner.speed - opponent.speed) * 0.04))
            success = rng.random() < chance
            if success:
                runner.last_result = f"You escaped from {opponent.name}."
                opponent.last_result = f"{runner.name} escaped from the battle."
                event = game.add_event(
                    f"{runner.name} escaped the battle with {opponent.name}.",
                    "BATTLE_RUN",
                )
                events.append(event)
                _end_battle_locked(battle.id)
                return events
            runner.last_result = f"You tried to run from {opponent.name}, but failed."
            opponent.last_result = f"{runner.name} tried to run, but failed."
            events.append(game.add_event(
                f"{runner.name} failed to escape {opponent.name}.",
                "BATTLE_RUN_FAILED",
            ))

        if action_a == action_b == "BATTLE_RUN":
            # Neither runner got away; the fight advances to another turn.
            pass
        elif action_a == "BATTLE_RUN":
            # A failed runner is exposed to an attacking opponent; a defender
            # simply holds position and a second run opportunity is created.
            if action_b == "BATTLE_ATTACK" and player_a.alive:
                damage, critical, reduction = _attack_damage_locked(player_b, player_a)
                player_a.health = max(0, player_a.health - damage)
                player_b.last_result = f"You caught {player_a.name} while they ran for {damage} damage."
                player_a.last_result = f"You failed to run and took {damage} damage from {player_b.name}."
                events.append(game.add_event(
                    f"{player_b.name} struck {player_a.name} for {damage} damage as they tried to run.{(' Critical hit.' if critical else '')}{(' Armor helped.' if reduction else '')}",
                    "BATTLE_HIT",
                ))
                if player_a.health <= 0:
                    events.append(_eliminate_player_locked(player_a, f"{player_a.name} ({player_a.id}) was eliminated by {player_b.name} in battle.", killer=player_b))
                    if game.alive_count <= 1:
                        _finish_game_locked()
                    return events
        elif action_b == "BATTLE_RUN" and action_a == "BATTLE_ATTACK" and player_b.alive:
            damage, critical, reduction = _attack_damage_locked(player_a, player_b)
            player_b.health = max(0, player_b.health - damage)
            player_a.last_result = f"You caught {player_b.name} while they ran for {damage} damage."
            player_b.last_result = f"You failed to run and took {damage} damage from {player_a.name}."
            events.append(game.add_event(
                f"{player_a.name} struck {player_b.name} for {damage} damage as they tried to run.{(' Critical hit.' if critical else '')}{(' Armor helped.' if reduction else '')}",
                "BATTLE_HIT",
            ))
            if player_b.health <= 0:
                events.append(_eliminate_player_locked(player_b, f"{player_b.name} ({player_b.id}) was eliminated by {player_a.name} in battle.", killer=player_a))
                if game.alive_count <= 1:
                    _finish_game_locked()
                return events
    else:
        # Both combatants commit simultaneously. Defending reduces incoming
        # damage by 55%; armor adds its existing 8-point reduction once.
        if action_a == "BATTLE_ATTACK":
            damage, critical, reduction = _attack_damage_locked(player_a, player_b, defense=action_b == "BATTLE_DEFEND")
            player_b.health = max(0, player_b.health - damage)
            player_a.last_result = f"You attacked {player_b.name} for {damage} damage."
            player_b.last_result = f"{player_a.name} attacked you for {damage} damage."
            events.append(game.add_event(
                f"{player_a.name} attacked {player_b.name} for {damage} damage.{(' Critical hit.' if critical else '')}{(' Defense/armor reduced the hit.' if reduction else '')}",
                "BATTLE_HIT",
            ))
            if player_b.health <= 0:
                events.append(_eliminate_player_locked(player_b, f"{player_b.name} ({player_b.id}) was eliminated by {player_a.name} in battle.", killer=player_a))
                if game.alive_count <= 1:
                    _finish_game_locked()
                return events
        if action_b == "BATTLE_ATTACK" and player_a.alive and player_b.alive:
            damage, critical, reduction = _attack_damage_locked(player_b, player_a, defense=action_a == "BATTLE_DEFEND")
            player_a.health = max(0, player_a.health - damage)
            player_b.last_result = f"You attacked {player_a.name} for {damage} damage."
            player_a.last_result = f"{player_b.name} attacked you for {damage} damage."
            events.append(game.add_event(
                f"{player_b.name} attacked {player_a.name} for {damage} damage.{(' Critical hit.' if critical else '')}{(' Defense/armor reduced the hit.' if reduction else '')}",
                "BATTLE_HIT",
            ))
            if player_a.health <= 0:
                events.append(_eliminate_player_locked(player_a, f"{player_a.name} ({player_a.id}) was eliminated by {player_b.name} in battle.", killer=player_b))
                if game.alive_count <= 1:
                    _finish_game_locked()
                return events

        if action_a == action_b == "BATTLE_DEFEND" and player_a.alive and player_b.alive:
            player_a.last_result = f"You defended against {player_b.name}."
            player_b.last_result = f"You defended against {player_a.name}."
            events.append(game.add_event(
                f"{player_a.name} and {player_b.name} both held their ground.",
                "BATTLE_DEFEND",
            ))

    for player in (player_a, player_b):
        if player.alive:
            player.battle_action = None

    if not player_a.alive or not player_b.alive:
        _end_battle_locked(battle.id)
        return events

    battle.turn_number += 1
    battle.actions.clear()
    battle.deadline = datetime.now(timezone.utc) + timedelta(seconds=BATTLE_TURN_DURATION_SECONDS)
    for player in (player_a, player_b):
        player.current_action = "BATTLE"
        player.action_taken = True
        player.action_deadline = battle.deadline
        player.last_result = f"Battle turn {battle.turn_number}: choose ATTACK, DEFEND, or RUN."
    events.append(game.add_event(
        f"Battle between {player_a.name} and {player_b.name} moves to turn {battle.turn_number}.",
        "BATTLE_TURN",
    ))
    return events


def _resolve_expired_battles_locked() -> bool:
    changed = False
    now = datetime.now(timezone.utc)
    for battle in list(game.battles.values()):
        if battle.deadline is None or now < battle.deadline:
            continue
        for player_id in battle.participant_ids():
            if player_id not in battle.actions and game.players.get(player_id) and game.players[player_id].alive:
                battle.actions[player_id] = "BATTLE_DEFEND"
                game.players[player_id].battle_action = "BATTLE_DEFEND"
                game.players[player_id].last_result = "You hesitated, so you defended automatically."
        _resolve_battle_turn_locked(battle)
        changed = True
    return changed


def _use_item_locked(player: Player, item_id: str) -> dict[str, Any]:
    item_index = next((index for index, item in enumerate(player.inventory) if item.id == item_id), None)
    if item_index is None:
        raise HTTPException(status_code=409, detail="That item is no longer in your inventory.")

    item = player.inventory[item_index]
    if item.type == "MEDKIT":
        old_health = player.health
        player.health = min(player.max_health, player.health + 30)
        player.last_result = f"You used a Medkit and recovered {player.health - old_health} health."
    elif item.type == "FOOD":
        old_health = player.health
        player.health = min(player.max_health, player.health + 12)
        player.last_result = f"You ate and recovered {player.health - old_health} health."
    elif item.type == "WEAPON":
        player.attack += 3
        player.last_result = "You equipped the Weapon. Attack increased by 3."
    elif item.type == "ARMOR":
        player.status_effect = "ARMORED"
        player.last_result = "You prepared the Armor. The next hit against you will be reduced by 8 damage."
    elif item.type == "SPEED_BOOST":
        player.speed += 3
        player.last_result = "You used the Speed Boost. Speed increased by 3."
    else:
        raise HTTPException(status_code=409, detail="Unknown item.")

    player.inventory.pop(item_index)
    return game.add_event(f"{player.name} used a {item.name} in {ZONES[player.zone_id].name}.", "ITEM_USED")


def _grab_item_locked(player: Player) -> dict[str, Any]:
    if player.zone_id != "cornucopia":
        raise HTTPException(status_code=409, detail="Items can only be collected at the Cornucopia.")
    if len(player.inventory) >= MAX_INVENTORY:
        player.last_result = f"Your inventory is full ({MAX_INVENTORY} items)."
        raise HTTPException(status_code=409, detail="Your inventory is full.")
    if not game.zone_items["cornucopia"]:
        player.last_result = "The Cornucopia is empty."
        raise HTTPException(status_code=409, detail="The Cornucopia is empty.")

    item = game.zone_items["cornucopia"].pop(0)
    player.inventory.append(item)
    player.last_result = f"You grabbed a {item.name}. {item.description}"
    return game.add_event(f"{player.name} grabbed a {item.name} from the Cornucopia.", "ITEM_GRABBED")


def _apply_player_action_locked(
    player: Player,
    action: str,
    target_zone_id: str | None,
    target_player_id: str | None,
    item_id: str | None,
) -> dict[str, Any]:
    action = action.upper()
    valid, error = _validate_player_action_locked(player, action, target_zone_id, target_player_id, item_id)
    if not valid:
        raise HTTPException(status_code=409, detail=error)

    if action in BATTLE_ACTION_SET:
        battle = get_battle_locked(player)
        if not battle:
            raise HTTPException(status_code=409, detail="You are not currently in a battle.")
        player.battle_action = action
        player.current_action = action
        player.action_taken = True
        player.action_deadline = battle.deadline
        battle.actions[player.id] = action
        event = game.add_event(
            f"{player.name} chose {action.replace('BATTLE_', '')} in battle.",
            "BATTLE_ACTION",
        )
        if len(battle.actions) == 2:
            battle_events = _resolve_battle_turn_locked(battle)
            event = battle_events[-1] if battle_events else event
        return event

    player.current_action = action
    player.action_taken = True
    player.action_deadline = None

    if action == "MOVE":
        previous = player.zone_id
        player.zone_id = target_zone_id or player.zone_id
        player.last_result = f"You moved from {ZONES[previous].name} to {ZONES[player.zone_id].name}."
        event = game.add_event(f"{player.name} moved to {ZONES[player.zone_id].name}.", "PLAYER_MOVED")
    elif action == "REST":
        old_health = player.health
        player.health = min(player.max_health, player.health + 5)
        player.last_result = f"You rested and recovered {player.health - old_health} health."
        event = game.add_event(f"{player.name} rested in {ZONES[player.zone_id].name}.", "PLAYER_RESTED")
    elif action == "SCOUT":
        adjacent = ZONES[player.zone_id].connected_zones
        counts = [f"{ZONES[zone_id].name}: {game.zone_counts[zone_id]}" for zone_id in adjacent]
        player.last_result = "Nearby population: " + ", ".join(counts) + "."
        event = game.add_event(f"{player.name} scouted the area.", "PLAYER_SCOUTED")
    elif action == "ATTACK":
        target = game.players.get(target_player_id or "")
        if not target or not target.alive:
            raise HTTPException(status_code=409, detail="That player is no longer available.")
        battle = Battle(
            id=f"B-{secrets.token_hex(5)}",
            player_a_id=player.id,
            player_b_id=target.id,
            started_round=game.round_number,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=BATTLE_TURN_DURATION_SECONDS),
        )
        game.battles[battle.id] = battle
        player.battle_id = battle.id
        player.battle_opponent_id = target.id
        player.battle_action = None
        player.current_action = "BATTLE"
        player.action_taken = True
        player.action_deadline = battle.deadline
        target.battle_id = battle.id
        target.battle_opponent_id = player.id
        target.battle_action = None
        target.current_action = "BATTLE"
        target.action_taken = True
        target.action_deadline = battle.deadline
        player.last_result = f"You engaged {target.name}. Choose ATTACK, DEFEND, or RUN."
        target.last_result = f"{player.name} attacked you and you are now engaged. Choose ATTACK, DEFEND, or RUN."
        event = game.add_event(
            f"{player.name} engaged {target.name} in battle in {ZONES[player.zone_id].name}.",
            "BATTLE_STARTED",
        )
    elif action == "USE_ITEM":
        event = _use_item_locked(player, item_id or "")
    elif action == "GRAB_ITEM":
        event = _grab_item_locked(player)
    else:  # WAIT
        player.last_result = "You waited and watched your surroundings."
        event = game.add_event(f"{player.name} waited in {ZONES[player.zone_id].name}.", "PLAYER_WAITED")

    return event


def require_admin(x_admin_token: str | None) -> None:
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")


def require_player_token(player: Player, token: str | None) -> None:
    if not token or not secrets.compare_digest(token, player.session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid player session")


# ---------------------------------------------------------------------------
# State synchronization
# ---------------------------------------------------------------------------

async def sync_everyone_full() -> None:
    async with state_lock:
        player_snapshots = {
            player_id: player_state(player)
            for player_id, player in game.players.items()
        }
        admin_snapshot = admin_state()
        spectator_snapshot = spectate_state()

    await asyncio.gather(
        *(manager.send_player(pid, {"type": "GAME_STATE", "data": snapshot}) for pid, snapshot in player_snapshots.items()),
        manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot}),
        manager.broadcast_spectators({"type": "SPECTATE_STATE", "data": spectator_snapshot}),
    )


async def sync_action_result(player_id: str, event: dict[str, Any]) -> None:
    async with state_lock:
        snapshot = player_state(game.players[player_id]) if player_id in game.players else None
        admin_snapshot = admin_state()
        spectator_snapshot = spectate_state()

    sends = [
        manager.broadcast_players({"type": "PUBLIC_EVENT", "data": event}),
        manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot}),
        manager.broadcast_spectators({"type": "SPECTATE_STATE", "data": spectator_snapshot}),
    ]
    if snapshot is not None:
        sends.append(manager.send_player(player_id, {"type": "GAME_STATE", "data": snapshot}))
    await asyncio.gather(*sends)


async def sync_player_ids(player_ids: set[str]) -> None:
    async with state_lock:
        snapshots = {
            player_id: player_state(game.players[player_id])
            for player_id in player_ids
            if player_id in game.players
        }
    await asyncio.gather(*(
        manager.send_player(player_id, {"type": "GAME_STATE", "data": snapshot})
        for player_id, snapshot in snapshots.items()
    ))


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/game")
async def get_game() -> dict[str, Any]:
    async with state_lock:
        return {
            "gameId": game.game_id,
            "status": game.status.value,
            "phase": game.phase.value,
            "round": game.round_number,
            "roundDurationSeconds": ROUND_DURATION_SECONDS,
            "playerCount": len(game.players),
            "aliveCount": game.alive_count,
            "maxPlayers": MAX_PLAYERS,
            "zones": list(ZONES.keys()),
        }


@app.get("/api/zones")
async def get_zones() -> list[dict[str, Any]]:
    async with state_lock:
        return [public_zone_dict(zone, include_counts=True) for zone in ZONES.values()]


@app.get("/api/player/{player_id}/state")
async def get_player_session_state(player_id: str, x_player_token: str | None = Header(default=None)) -> dict[str, Any]:
    async with state_lock:
        player = game.players.get(player_id)
        if not player:
            raise HTTPException(status_code=404, detail="Player not found")
        require_player_token(player, x_player_token)
        return player_state(player)


@app.get("/api/spectate/state")
async def get_spectate_state(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(x_admin_token)
    async with state_lock:
        return spectate_state()


@app.post("/api/join")
async def join_game(request: JoinRequest) -> dict[str, Any]:
    name = clean_name(request.name)
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be blank")

    async with state_lock:
        if len(game.players) >= MAX_PLAYERS:
            raise HTTPException(status_code=409, detail="The game is full")
        if game.status != GameStatus.LOBBY:
            raise HTTPException(status_code=409, detail="Registration is closed")

        duplicate = next((p for p in game.players.values() if p.name.casefold() == name.casefold()), None)
        if duplicate:
            raise HTTPException(status_code=409, detail="That name is already registered")

        player = Player(id=next_player_id(), name=name)
        game.players[player.id] = player
        _write_checkpoint_locked()
        event = game.add_event(f"{player.name} joined the arena ({player.id}).", "PLAYER_JOINED")
        player_snapshot = player_state(player)
        current_game = {
            "gameId": game.game_id,
            "status": game.status.value,
            "phase": game.phase.value,
            "round": game.round_number,
            "playerCount": len(game.players),
            "maxPlayers": MAX_PLAYERS,
        }

    await asyncio.gather(
        manager.broadcast_players({"type": "PUBLIC_EVENT", "data": event}),
        manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_state()}),
        manager.broadcast_spectators({"type": "SPECTATE_STATE", "data": spectate_state()}),
    )

    return {
        "player": player_snapshot["player"],
        "sessionToken": player.session_token,
        "game": current_game,
    }


@app.post("/api/action")
async def player_action(
    request: PlayerActionRequest,
    x_player_token: str | None = Header(default=None),
) -> dict[str, Any]:
    async with state_lock:
        player = game.players.get(request.playerId)
        if not player:
            raise HTTPException(status_code=404, detail="Player not found")
        require_player_token(player, x_player_token)
        related_player_ids = {player.id}
        if request.action.upper() == "ATTACK" and request.targetPlayerId:
            related_player_ids.add(request.targetPlayerId)
        elif player.battle_opponent_id:
            related_player_ids.add(player.battle_opponent_id)
        event = _apply_player_action_locked(
            player,
            request.action,
            request.targetZoneId,
            request.targetPlayerId,
            request.itemId,
        )
        snapshot = player_state(player)
        _write_checkpoint_locked()

    await sync_action_result(player.id, event)
    await sync_player_ids(related_player_ids - {player.id})
    return snapshot


@app.post("/api/admin/login")
async def admin_login(request: AdminLoginRequest) -> dict[str, bool]:
    if not secrets.compare_digest(request.token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    return {"authenticated": True}


@app.get("/api/admin/state")
async def get_admin_state(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(x_admin_token)
    async with state_lock:
        return admin_state()


@app.post("/api/admin/action")
async def admin_action(
    request: AdminActionRequest,
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(x_admin_token)
    action = request.action.upper()
    reset_requested = False
    selected_event: dict[str, Any] | None = None

    async with state_lock:
        if action == "START_GAME":
            if game.status != GameStatus.LOBBY:
                raise HTTPException(status_code=409, detail="Game can only be started from the lobby")
            if len(game.players) < 2:
                raise HTTPException(status_code=409, detail="At least two players are required to start the arena")
            assign_starting_zones_locked()
            game.status = GameStatus.ACTIVE
            game.round_number = 1
            game.winner_id = None
            game.add_event("The arena has begun.", "GAME_STARTED")
            _begin_round_locked()
        elif action == "PAUSE_GAME":
            if game.status != GameStatus.ACTIVE:
                raise HTTPException(status_code=409, detail="Game is not active")
            if game.round_deadline:
                game.paused_remaining_seconds = max(0.0, (game.round_deadline - datetime.now(timezone.utc)).total_seconds())
            game.round_deadline = None
            now = datetime.now(timezone.utc)
            for battle in game.battles.values():
                if battle.deadline:
                    battle.paused_remaining_seconds = max(0.0, (battle.deadline - now).total_seconds())
                battle.deadline = None
            for player in game.players.values():
                if player.alive:
                    player.action_deadline = None
            game.status = GameStatus.PAUSED
            game.add_event("The arena has been paused by the admin.", "GAME_PAUSED")
        elif action == "RESUME_GAME":
            if game.status != GameStatus.PAUSED:
                raise HTTPException(status_code=409, detail="Game is not paused")
            remaining = game.paused_remaining_seconds or ROUND_DURATION_SECONDS
            game.round_deadline = datetime.now(timezone.utc) + timedelta(seconds=max(1, remaining))
            now = datetime.now(timezone.utc)
            for battle in game.battles.values():
                battle_remaining = battle.paused_remaining_seconds or BATTLE_TURN_DURATION_SECONDS
                battle.deadline = now + timedelta(seconds=max(1, battle_remaining))
                battle.paused_remaining_seconds = None
            for player in game.players.values():
                if player.alive:
                    player.action_deadline = game.battles[player.battle_id].deadline if player.battle_id in game.battles else (None if player.action_taken else game.round_deadline)
            game.status = GameStatus.ACTIVE
            game.paused_remaining_seconds = None
            game.add_event("The arena has resumed.", "GAME_RESUMED")
        elif action == "END_ROUND":
            if game.status != GameStatus.ACTIVE:
                raise HTTPException(status_code=409, detail="Game is not active")
            _resolve_round_locked()
        elif action == "SPAWN_SUPPLY_DROP":
            if game.status not in {GameStatus.ACTIVE, GameStatus.PAUSED}:
                raise HTTPException(status_code=409, detail="Supply drops can only be triggered during the game")
            if request.targetZoneId not in {None, "cornucopia"}:
                raise HTTPException(status_code=400, detail="Player supplies can only be placed at the Cornucopia.")
            game.zone_items["cornucopia"].append(make_item(random_loot_type()))
            selected_event = game.add_event("The admin dropped supplies at the Cornucopia.", "SUPPLY_DROP")
        elif action == "TRIGGER_HAZARD":
            if game.status not in {GameStatus.ACTIVE, GameStatus.PAUSED}:
                raise HTTPException(status_code=409, detail="Hazards can only be triggered during the game")
            target = request.targetZoneId
            if target is not None and target not in OUTER_ZONE_IDS:
                raise HTTPException(status_code=400, detail="Hazards can only target outer zones")
            if target:
                game.hazard_zones = {target}
            else:
                game.hazard_zones = {rng.choice(list(OUTER_ZONE_IDS))}
            names = ", ".join(ZONES[zone_id].name for zone_id in game.hazard_zones)
            selected_event = game.add_event(
                f"Admin activated an arena hazard in {names}. Players there will take {HAZARD_DAMAGE} damage at round end.",
                "ARENA_HAZARD",
            )
        elif action == "CLEAR_HAZARDS":
            game.hazard_zones.clear()
            selected_event = game.add_event("The admin cleared all active arena hazards.", "ARENA_HAZARD_CLEARED")
        elif action == "BROADCAST_ANNOUNCEMENT":
            message = " ".join((request.message or "").strip().split())
            if not message:
                raise HTTPException(status_code=400, detail="Enter an announcement message")
            selected_event = game.add_event(f"ARENA ANNOUNCEMENT: {message}", "ANNOUNCEMENT")
        elif action in {"ELIMINATE_PLAYER", "RESTORE_PLAYER", "MOVE_PLAYER", "SET_HEALTH", "SET_STATS", "GIVE_ITEM"}:
            if not request.targetPlayerId:
                raise HTTPException(status_code=400, detail="Choose a player")
            target = game.players.get(request.targetPlayerId)
            if not target:
                raise HTTPException(status_code=404, detail="Player not found")

            if action == "ELIMINATE_PLAYER":
                if game.status == GameStatus.LOBBY:
                    raise HTTPException(status_code=409, detail="The game has not started yet")
                reason = " ".join((request.reason or "Eliminated by the admin.").strip().split())
                selected_event = _eliminate_player_locked(target, f"{target.name} ({target.id}) was eliminated by the admin. {reason}")
                if game.alive_count <= 1 and game.status != GameStatus.GAME_OVER:
                    _finish_game_locked()
            elif action == "RESTORE_PLAYER":
                if game.status == GameStatus.GAME_OVER:
                    raise HTTPException(status_code=409, detail="The game is already over")
                if target.alive:
                    raise HTTPException(status_code=409, detail="That player is already alive")
                if target.battle_id:
                    _end_battle_locked(target.battle_id, reason=f"The battle involving {target.name} was cleared during admin restoration.")
                target.alive = True
                target.health = max(1, min(target.max_health, request.value or 50))
                target.status_effect = "NORMAL"
                target.last_result = "An admin restored you to the arena."
                target.action_taken = game.status != GameStatus.ACTIVE
                target.current_action = None
                target.action_deadline = None if game.status != GameStatus.ACTIVE else game.round_deadline
                selected_event = game.add_event(f"{target.name} ({target.id}) was restored to the arena by the admin.", "PLAYER_RESTORED")
            elif action == "MOVE_PLAYER":
                destination = request.targetZoneId
                if not destination or destination not in ZONES:
                    raise HTTPException(status_code=400, detail="Choose a destination zone")
                if target.battle_id:
                    _end_battle_locked(target.battle_id, reason=f"The battle involving {target.name} ended after an admin move.")
                if target.alive:
                    target.zone_id = destination
                else:
                    target.zone_id = destination
                target.last_result = f"An admin moved you to {ZONES[destination].name}."
                selected_event = game.add_event(f"Admin moved {target.name} ({target.id}) to {ZONES[destination].name}.", "PLAYER_MOVED_ADMIN")
            elif action == "SET_HEALTH":
                if target.battle_id:
                    _end_battle_locked(target.battle_id, reason=f"The battle involving {target.name} ended after an admin health change.")
                target.health = min(target.max_health, request.value or target.health)
                if target.health > 0 and not target.alive and game.status != GameStatus.GAME_OVER:
                    target.alive = True
                    target.status_effect = "NORMAL"
                if target.health == 0:
                    target.alive = False
                    target.status_effect = "ELIMINATED"
                target.last_result = f"An admin set your health to {target.health}."
                selected_event = game.add_event(f"Admin set {target.name} ({target.id}) health to {target.health}.", "PLAYER_HEALTH_SET")
            elif action == "SET_STATS":
                if request.attack is None and request.speed is None and request.value is None:
                    raise HTTPException(status_code=400, detail="Provide at least one stat value")
                if target.battle_id:
                    _end_battle_locked(target.battle_id, reason=f"The battle involving {target.name} ended after an admin stat change.")
                if request.value is not None:
                    target.health = min(target.max_health, request.value)
                if request.attack is not None:
                    target.attack = request.attack
                if request.speed is not None:
                    target.speed = request.speed
                if target.health > 0 and not target.alive and game.status != GameStatus.GAME_OVER:
                    target.alive = True
                    target.status_effect = "NORMAL"
                target.last_result = "An admin updated your arena stats."
                selected_event = game.add_event(f"Admin updated {target.name} ({target.id}) stats.", "PLAYER_STATS_SET")
            elif action == "GIVE_ITEM":
                item_type = (request.itemType or "").upper()
                if item_type not in ITEM_DEFINITIONS:
                    raise HTTPException(status_code=400, detail="Choose a valid item type")
                if len(target.inventory) >= MAX_INVENTORY:
                    raise HTTPException(status_code=409, detail="That player's inventory is full")
                item = make_item(item_type)
                target.inventory.append(item)
                target.last_result = f"An admin gave you a {item.name}."
                selected_event = game.add_event(f"Admin gave {target.name} ({target.id}) a {item.name}.", "ITEM_GRANTED")
        elif action == "RESET_GAME":
            reset_requested = True
            game.status = GameStatus.LOBBY
            game.phase = GamePhase.OPENING
            game.round_number = 0
            game.round_deadline = None
            game.paused_remaining_seconds = None
            game.winner_id = None
            game.battles.clear()
            game.players.clear()
            game.event_log.clear()
            for items in game.zone_items.values():
                items.clear()
            game.hazard_zones.clear()
            game.add_event("The arena has been reset. Waiting for players.", "GAME_RESET")
        else:
            raise HTTPException(status_code=400, detail="Unsupported admin action")

        _write_checkpoint_locked()
        snapshot = admin_state()

    if reset_requested:
        await manager.close_all_players({"type": "RESET"})
    if selected_event and action == "BROADCAST_ANNOUNCEMENT":
        await manager.broadcast_players({"type": "PUBLIC_EVENT", "data": selected_event})
    await sync_everyone_full()
    return snapshot


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------

@app.websocket("/ws/player/{player_id}")
async def player_ws(websocket: WebSocket, player_id: str, token: str | None = None) -> None:
    async with state_lock:
        player = game.players.get(player_id)
        valid = bool(player and token and secrets.compare_digest(token, player.session_token))
    if not valid:
        await websocket.close(code=1008)
        return

    await manager.connect_player(player_id, websocket)
    async with state_lock:
        player = game.players[player_id]
        player.connected = True
        snapshot = player_state(player)
        admin_snapshot = admin_state()
    await manager.send_player(player_id, {"type": "GAME_STATE", "data": snapshot})
    await manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot})
    async with state_lock:
        spectator_snapshot = spectate_state()
    await manager.broadcast_spectators({"type": "SPECTATE_STATE", "data": spectator_snapshot})

    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "PING":
                await websocket.send_json({"type": "PONG"})
    except WebSocketDisconnect:
        last_connection = manager.disconnect_player(player_id, websocket)
        if last_connection:
            async with state_lock:
                if player_id in game.players:
                    game.players[player_id].connected = False
                    _write_checkpoint_locked()
                    admin_snapshot = admin_state()
                    spectator_snapshot = spectate_state()
            await manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot})
            await manager.broadcast_spectators({"type": "SPECTATE_STATE", "data": spectator_snapshot})


@app.websocket("/ws/admin")
async def admin_ws(websocket: WebSocket, token: str | None = None) -> None:
    if not token or not secrets.compare_digest(token, ADMIN_TOKEN):
        await websocket.close(code=1008)
        return

    await manager.connect_admin(websocket)
    async with state_lock:
        snapshot = admin_state()
    await websocket.send_json({"type": "ADMIN_STATE", "data": snapshot})

    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "PING":
                await websocket.send_json({"type": "PONG"})
    except WebSocketDisconnect:
        manager.disconnect_admin(websocket)


@app.websocket("/ws/spectate")
async def spectate_ws(websocket: WebSocket, token: str | None = None) -> None:
    if not token or not secrets.compare_digest(token, ADMIN_TOKEN):
        await websocket.close(code=1008)
        return

    await manager.connect_spectator(websocket)
    async with state_lock:
        snapshot = spectate_state()
    await websocket.send_json({"type": "SPECTATE_STATE", "data": snapshot})

    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "PING":
                await websocket.send_json({"type": "PONG"})
    except WebSocketDisconnect:
        manager.disconnect_spectator(websocket)
