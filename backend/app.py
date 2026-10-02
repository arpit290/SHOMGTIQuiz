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
from typing import Any, Iterable, Literal

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_PLAYERS = 24
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
ROUND_DURATION_SECONDS = max(5, int(os.getenv("ROUND_DURATION_SECONDS", "30")))
BATTLE_TURN_DURATION_SECONDS = max(5, int(os.getenv("BATTLE_TURN_DURATION_SECONDS", "15")))
FINAL_PLAYER_THRESHOLD = max(2, int(os.getenv("FINAL_PLAYER_THRESHOLD", "20")))
MAX_INVENTORY = max(1, int(os.getenv("MAX_INVENTORY", "1")))
STATE_FILE = Path(os.getenv("ARENA_STATE_FILE", "arena_state.json"))

# Player stat system: every player picks exactly one HIGH, one MID and one LOW
# across attack / defense / agility. MID is the baseline, HIGH/LOW are +/-40%,
# and a tiny random multiplier keeps two players' stats from being identical.
STAT_BASELINE = 10.0
STAT_LEVEL_MULTIPLIERS = {"HIGH": 1.4, "MID": 1.0, "LOW": 0.6}
STAT_JITTER = float(os.getenv("STAT_JITTER", "0.04"))  # +/-4% by default
STAT_NAMES = ("attack", "defense", "agility")

# Fixed hazard rules for the six outer zones. Hazard effects are processed at
# the start of each round (the player's turn). The admin controls whether each
# hazard is active.
LIGHTNING_DAMAGE_PERCENT = 0.20
TRACKER_JACKER_DAMAGE_PERCENT = 0.30
POISON_DAMAGE_PERCENTS = {1: 0.07, 2: 0.15, 3: 0.25}
TIDAL_DAMAGE_PERCENT = 0.50
FEAR_STAT_MULTIPLIER = 0.70
FEAR_DURATION_TURNS = 5
POISON_DURATION_TURNS = 2

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
    hazard_name: str | None = None
    hazard_description: str | None = None


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
    uses_remaining: int | None = None

    def dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "description": self.description,
        }
        if self.uses_remaining is not None:
            payload["usesRemaining"] = self.uses_remaining
        return payload


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
    "MEDKIT": ItemDefinition("MEDKIT", "Medkit", "Consumable. Heal 50% of your maximum health."),
    "SHINY_SWORD": ItemDefinition("SHINY_SWORD", "Shiny Sword", "Increase Attack by 40%."),
    "GOLDEN_APPLE": ItemDefinition("GOLDEN_APPLE", "Golden Apple", "Consumable. Boost Attack, Defense and Agility by 50% for 5 turns."),
    "SHADOW_CLOAK": ItemDefinition("SHADOW_CLOAK", "Shadow Cloak", "Increase your chance to dodge an incoming attack by 35%."),
    "TITAN_SHIELD": ItemDefinition("TITAN_SHIELD", "Titan Shield", "Reduce damage taken in a fight by 40%. Breaks after 7 uses."),
    "HUNTERS_FEATHER": ItemDefinition("HUNTERS_FEATHER", "Hunter's Feather", "Gain 50% Agility, but take 50% more damage in a fight."),
    "PHOENIX_ASHES": ItemDefinition("PHOENIX_ASHES", "Phoenix Ashes", "When you die, revive with 35% maximum health. Destroyed on use."),
    "HEART_OF_IRON": ItemDefinition("HEART_OF_IRON", "Heart of Iron", "Increase maximum HP by 60%, but reduce Agility by 50%."),
    "SERPENTINE_DAGGER": ItemDefinition("SERPENTINE_DAGGER", "Serpentine Dagger", "0.75x Attack. Attacks apply the Poisoned status."),
    "BERSERKER_GAUNTLETS": ItemDefinition("BERSERKER_GAUNTLETS", "Berserker Gauntlets", "Gain 30% Attack below 50% HP and 75% Attack below 15% HP."),
    "ADVENTURERS_BOOTS": ItemDefinition("ADVENTURERS_BOOTS", "Adventurer's Boots", "Travel up to 2 areas left or right and take 40% less environmental damage."),
    "CROWN_OF_BLOOD": ItemDefinition("CROWN_OF_BLOOD", "Crown Of Blood", "Each kill while wearing it increases all combat stats by 30%. Must stay in the Cornucopia while equipped."),
}

CONSUMABLE_ITEM_TYPES = {"MEDKIT", "GOLDEN_APPLE"}


# Six outer zones form a ring; the Cornucopia is the central hub.
OUTER_ZONE_IDS = tuple(f"zone_{index}" for index in range(1, 7))


def build_zones() -> dict[str, ZoneDefinition]:
    zones: dict[str, ZoneDefinition] = {}
    total = len(OUTER_ZONE_IDS)

    definitions = {
        1: (
            "Lightning Strikes",
            "Every turn you begin here, there is a chance to be struck by lightning. Agility affects your odds and damage.",
        ),
        2: (
            "Tracker Jacker Wasps",
            "At the start of a turn there is a chance to be stung for 30% damage. Agility affects your odds and damage.",
        ),
        3: (
            "Blood Rain",
            "You are afflicted with fear: all stats are reduced by 30% for the next 5 turns.",
        ),
        4: (
            "Poison Fog",
            "Stay here and the poison escalates: 7% damage, then 15%, then 25% each turn while the poison lasts.",
        ),
        5: (
            "Tidal Wave",
            "Every third turn a huge wave hits everyone in the sector for 50% damage, reduced by agility.",
        ),
        6: (
            "Monkey Mutations",
            "Monkey Mutts attack every turn, dealing 5–20% damage. Agility reduces the hit.",
        ),
    }

    for index, zone_id in enumerate(OUTER_ZONE_IDS, start=1):
        previous_id = OUTER_ZONE_IDS[(index - 2) % total]
        next_id = OUTER_ZONE_IDS[index % total]
        hazard_name, hazard_description = definitions[index]
        zones[zone_id] = ZoneDefinition(
            id=zone_id,
            name=f"Zone {index}",
            description=hazard_description,
            connected_zones=(previous_id, next_id, "cornucopia"),
            hazard_name=hazard_name,
            hazard_description=hazard_description,
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
    gender: Literal["M", "F"] = "M"
    district: int = 1
    health: int = 100
    max_health: int = 100
    attack: float = STAT_BASELINE
    defense: float = STAT_BASELINE
    agility: float = STAT_BASELINE
    base_attack: float = STAT_BASELINE
    base_defense: float = STAT_BASELINE
    base_agility: float = STAT_BASELINE
    zone_id: str = "zone_1"
    alive: bool = True
    connected: bool = False
    current_action: str | None = None
    action_taken: bool = False
    round_action_complete: bool = False
    action_deadline: datetime | None = None
    status_effect: str = "NORMAL"
    fear_turns_remaining: int = 0
    poison_stage: int = 0
    poison_turns_remaining: int = 0
    last_result: str = "Waiting for the game to begin."
    joined_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    session_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    kills: int = 0
    inventory: list[Item] = field(default_factory=list)
    golden_apple_turns_remaining: int = 0
    crown_blood_stacks: int = 0
    battle_id: str | None = None
    battle_opponent_id: str | None = None
    battle_action: str | None = None

    def inventory_dict(self) -> list[dict[str, str]]:
        return [item.dict() for item in self.inventory]

    def public_dict(self) -> dict[str, Any]:
        # This object is only sent to the player themselves (or the admin).
        status_parts: list[str] = []
        if self.fear_turns_remaining > 0:
            status_parts.append(f"FEAR · {self.fear_turns_remaining}T")
        if self.poison_turns_remaining > 0:
            label = {1: "POISONED", 2: "BADLY POISONED", 3: "SEVERELY POISONED"}.get(self.poison_stage, "POISONED")
            status_parts.append(f"{label} · {self.poison_turns_remaining}T")
        if self.golden_apple_turns_remaining > 0:
            status_parts.append(f"GOLDEN APPLE · {self.golden_apple_turns_remaining}T")
        if self.status_effect == "ARMORED":
            status_parts.append("ARMORED")
        status_display = " + ".join(status_parts) if status_parts else self.status_effect
        return {
            "id": self.id,
            "name": self.name,
            "gender": self.gender,
            "district": self.district,
            "health": self.health,
            "maxHealth": self.max_health,
            "attack": effective_stat(self, "attack"),
            "defense": effective_stat(self, "defense"),
            "agility": effective_stat(self, "agility"),
            "zoneId": self.zone_id,
            "zoneName": ZONES[self.zone_id].name,
            "alive": self.alive,
            "connected": self.connected,
            "currentAction": self.current_action,
            "actionTaken": self.action_taken,
            "actionDeadline": self.action_deadline.isoformat() if self.action_deadline else None,
            "statusEffect": status_display,
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
            "baseAttack": self.base_attack,
            "baseDefense": self.base_defense,
            "baseAgility": self.base_agility,
            "joinedAt": self.joined_at,
            "goldenAppleTurnsRemaining": self.golden_apple_turns_remaining,
            "crownBloodStacks": self.crown_blood_stacks,
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
    hazard_counters: dict[str, int] = field(default_factory=lambda: {zone_id: 0 for zone_id in OUTER_ZONE_IDS})
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

    def add_event(
        self,
        message: str,
        event_type: str = "INFO",
        *,
        player_ids: Iterable[str] = (),
        broadcast: bool = False,
    ) -> dict[str, Any]:
        """Append to the event log.

        `player_ids` lists the players an event is about (doing it, or having it
        done to them); only those players see it in their live feed.
        `broadcast=True` is reserved for arena-wide notices every player should see
        (announcements, game start / pause / resume / over).
        """
        event = {
            "id": secrets.token_hex(6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "message": message,
            "playerIds": sorted(set(player_ids)),
            "broadcast": broadcast,
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
                if _all_round_actions_complete_locked():
                    _resolve_round_locked(eliminate_missed=False)
                    should_sync = True
                elif round_expired:
                    _resolve_round_locked(eliminate_missed=True)
                    should_sync = True
                elif battle_expired:
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

StatLevel = Literal["HIGH", "MID", "LOW"]


class StatChoice(BaseModel):
    attack: StatLevel
    defense: StatLevel
    agility: StatLevel


class JoinRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    gender: Literal["M", "F"]
    stats: StatChoice | None = None


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
    attack: float | None = Field(default=None, ge=1, le=100)
    defense: float | None = Field(default=None, ge=1, le=100)
    agility: float | None = Field(default=None, ge=1, le=100)
    speed: float | None = Field(default=None, ge=1, le=100)  # legacy alias for agility


class PlayerActionRequest(BaseModel):
    playerId: str = Field(min_length=1, max_length=16)
    action: str
    targetZoneId: str | None = None
    targetPlayerId: str | None = None
    itemId: str | None = None


# ---------------------------------------------------------------------------
# Game engine helpers
# ---------------------------------------------------------------------------

ACTION_SET = {"MOVE", "REST", "ATTACK", "USE_ITEM", "WAIT", "GRAB_ITEM"}
BATTLE_ACTION_SET = {"BATTLE_ATTACK", "BATTLE_DEFEND", "BATTLE_RUN", "BATTLE_USE_ITEM"}


def event_visible_to(event: dict[str, Any], player_id: str) -> bool:
    return bool(event.get("broadcast")) or player_id in (event.get("playerIds") or ())


def player_feed(player_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """The live feed for one player: only events they did, or that happened to them."""
    feed: list[dict[str, Any]] = []
    for event in reversed(game.event_log):
        if event_visible_to(event, player_id):
            feed.append({
                "id": event["id"],
                "timestamp": event["timestamp"],
                "type": event["type"],
                "message": event["message"],
            })
            if len(feed) >= limit:
                break
    feed.reverse()
    return feed


def has_item(player: Player, item_type: str) -> bool:
    return any(item.type == item_type for item in player.inventory)


def refresh_item_derived_state_locked(player: Player) -> None:
    desired_max_health = 160 if has_item(player, "HEART_OF_IRON") else 100
    if player.max_health != desired_max_health:
        player.max_health = desired_max_health
        player.health = min(player.health, player.max_health)


def effective_stat(player: Player, stat_name: str) -> float:
    value = float(getattr(player, stat_name))
    if player.fear_turns_remaining > 0:
        value *= FEAR_STAT_MULTIPLIER
    if player.golden_apple_turns_remaining > 0:
        value *= 1.50
    if player.crown_blood_stacks > 0 and has_item(player, "CROWN_OF_BLOOD"):
        value *= 1.0 + (0.30 * player.crown_blood_stacks)
    if stat_name == "attack":
        if has_item(player, "SHINY_SWORD"):
            value *= 1.40
        if has_item(player, "SERPENTINE_DAGGER"):
            value *= 0.75
        if has_item(player, "BERSERKER_GAUNTLETS"):
            health_ratio = player.health / max(1, player.max_health)
            if health_ratio < 0.15:
                value *= 1.75
            elif health_ratio < 0.50:
                value *= 1.30
    elif stat_name == "agility":
        if has_item(player, "HUNTERS_FEATHER"):
            value *= 1.50
        if has_item(player, "HEART_OF_IRON"):
            value *= 0.50
    return value


def environment_damage_multiplier(player: Player) -> float:
    return 0.60 if has_item(player, "ADVENTURERS_BOOTS") else 1.0


def mitigation_multiplier(stat: float) -> float:
    """Damage multiplier from a defensive/agility stat: 1.0 at baseline, 0.6 at +40%, 1.4 at -40%."""
    return max(0.3, min(1.7, 2.0 - stat / STAT_BASELINE))


def percent_damage_for(player: Player, percent: float, *, agility_factor: bool = True) -> int:
    multiplier = mitigation_multiplier(effective_stat(player, "agility")) if agility_factor else 1.0
    multiplier *= environment_damage_multiplier(player)
    return max(1, int(round(player.max_health * percent * multiplier)))


def hazard_damage_for(player: Player) -> int:
    """Preview the current zone hazard's typical damage for the player's UI."""
    zone = ZONES[player.zone_id]
    if zone.id in game.hazard_zones:
        if zone.id == "zone_1":
            return percent_damage_for(player, LIGHTNING_DAMAGE_PERCENT, agility_factor=False)
        if zone.id == "zone_2":
            return percent_damage_for(player, TRACKER_JACKER_DAMAGE_PERCENT, agility_factor=False)
        if zone.id == "zone_4":
            stage = max(1, min(3, player.poison_stage or 1))
            return percent_damage_for(player, POISON_DAMAGE_PERCENTS[stage], agility_factor=False)
        if zone.id == "zone_5":
            return percent_damage_for(player, TIDAL_DAMAGE_PERCENT)
        if zone.id == "zone_6":
            return max(1, int(round(player.max_health * 0.125)))
    return 0


def roll_stat(level: str) -> float:
    jitter = rng.uniform(1.0 - STAT_JITTER, 1.0 + STAT_JITTER)
    return round(STAT_BASELINE * STAT_LEVEL_MULTIPLIERS[level] * jitter, 2)


def resolve_stat_choice(choice: "StatChoice | None") -> dict[str, str]:
    if choice is None:
        levels = ["HIGH", "MID", "LOW"]
        rng.shuffle(levels)
        return dict(zip(STAT_NAMES, levels))
    picked = {name: getattr(choice, name) for name in STAT_NAMES}
    if sorted(picked.values()) != ["HIGH", "LOW", "MID"]:
        raise HTTPException(status_code=400, detail="Choose exactly one HIGH, one MID and one LOW stat.")
    return picked


def clean_name(name: str) -> str:
    return " ".join(name.strip().split())


def next_player_id() -> str:
    index = len(game.players) + 1
    while f"P-{index:03d}" in game.players:
        index += 1
    return f"P-{index:03d}"


def next_district_for_gender_locked(gender: str) -> int | None:
    occupied = {player.district for player in game.players.values() if player.gender == gender}
    return next((district for district in range(1, 13) if district not in occupied), None)


def make_item(item_type: str) -> Item:
    definition = ITEM_DEFINITIONS[item_type]
    return Item(
        id=f"I-{secrets.token_hex(5)}",
        type=definition.type,
        name=definition.name,
        description=definition.description,
        uses_remaining=7 if definition.type == "TITAN_SHIELD" else None,
    )


def random_loot_type() -> str:
    return rng.choice(tuple(ITEM_DEFINITIONS.keys()))


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


def available_move_zone_ids(player: Player) -> list[str]:
    allowed = list(ZONES[player.zone_id].connected_zones)
    if has_item(player, "ADVENTURERS_BOOTS") and player.zone_id != "cornucopia":
        ring = list(OUTER_ZONE_IDS)
        if player.zone_id in ring:
            index = ring.index(player.zone_id)
            expanded = [ring[(index + offset) % len(ring)] for offset in (-2, -1, 1, 2)]
            allowed.extend(zone_id for zone_id in expanded if zone_id not in allowed)
    return allowed


def available_actions(player: Player) -> list[str]:
    if not player.alive or game.status != GameStatus.ACTIVE:
        return []

    battle = get_battle_locked(player)
    if battle:
        if player.battle_action:
            return []
        actions = set(BATTLE_ACTION_SET)
        if not (player.inventory and player.inventory[0].type in CONSUMABLE_ITEM_TYPES):
            actions.discard("BATTLE_USE_ITEM")
        return sorted(actions)

    if player.action_taken:
        return []

    actions = {"MOVE", "REST", "WAIT"}
    if nearby_opponents(player):
        actions.add("ATTACK")
    if player.inventory:
        actions.add("USE_ITEM")
    if player.zone_id == "cornucopia" and game.zone_items["cornucopia"]:
        # With a full bag, GRAB_ITEM becomes a swap: the held item goes back on the pile.
        actions.add("GRAB_ITEM")
    return sorted(actions)


def public_zone_dict(zone: ZoneDefinition, *, include_counts: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": zone.id,
        "name": zone.name,
        "description": zone.description,
        "connectedZones": list(zone.connected_zones),
        "hazardName": zone.hazard_name,
        "hazardDescription": zone.hazard_description,
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
        "adjacentZones": [public_zone_dict(ZONES[zone_id]) for zone_id in available_move_zone_ids(player)],
        "visibleOpponents": visible_opponent_dict(player),
        "zoneLootCount": len(game.zone_items[player.zone_id]),
        "offeredItem": (
            game.zone_items["cornucopia"][0].dict()
            if player.zone_id == "cornucopia" and game.zone_items["cornucopia"]
            else None
        ),
        "zoneHazard": player.zone_id in game.hazard_zones,
        "hazardName": ZONES[player.zone_id].hazard_name if player.zone_id in game.hazard_zones else None,
        "hazardDescription": ZONES[player.zone_id].hazard_description if player.zone_id in game.hazard_zones else None,
        "hazardDamage": hazard_damage_for(player),
        "playerCount": len(game.players),
        "aliveCount": game.alive_count,
        "maxPlayers": MAX_PLAYERS,
        "winnerId": game.winner_id,
        "events": player_feed(player.id),
        "battle": battle_dict(battle, viewer_id=player.id),
    }


def admin_state() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    alive_players = [p for p in game.players.values() if p.alive]
    acted_count = sum(1 for player in alive_players if player.round_action_complete)
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
        "hazardCounters": dict(game.hazard_counters),
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
    game.hazard_zones.update(zone_id for zone_id in raw.get("hazardZones", []) if zone_id in OUTER_ZONE_IDS)
    for zone_id in OUTER_ZONE_IDS:
        try:
            game.hazard_counters[zone_id] = int((raw.get("hazardCounters") or {}).get(zone_id, 0)) % 3
        except (TypeError, ValueError):
            game.hazard_counters[zone_id] = 0

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
                        uses_remaining=(int(data.get("usesRemaining", 7)) if definition.type == "TITAN_SHIELD" else None),
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
                gender=str(data.get("gender", "M")).upper() if str(data.get("gender", "M")).upper() in {"M", "F"} else "M",
                district=int(data.get("district", 1) or 1),
                health=int(data.get("health", 100)),
                max_health=int(data.get("maxHealth", 100)),
                attack=float(data.get("attack", STAT_BASELINE)),
                defense=float(data.get("defense", STAT_BASELINE)),
                agility=float(data.get("agility", data.get("speed", STAT_BASELINE))),
                base_attack=float(data.get("baseAttack", data.get("attack", STAT_BASELINE))),
                base_defense=float(data.get("baseDefense", data.get("defense", STAT_BASELINE))),
                base_agility=float(data.get("baseAgility", data.get("agility", data.get("speed", STAT_BASELINE)))),
                zone_id=str(data.get("zoneId", "zone_1")),
                alive=bool(data.get("alive", True)),
                connected=False,
                current_action=data.get("currentAction"),
                action_taken=bool(data.get("actionTaken", False)),
                round_action_complete=bool(data.get("roundActionComplete", bool(data.get("actionTaken", False)) and not data.get("battleId"))),
                action_deadline=None,
                status_effect=str(data.get("statusEffect", "NORMAL")),
                fear_turns_remaining=int(data.get("fearTurnsRemaining", 0)),
                poison_stage=int(data.get("poisonStage", 0)),
                poison_turns_remaining=int(data.get("poisonTurnsRemaining", 0)),
                last_result=str(data.get("lastResult", "Recovered from the previous server session.")),
                joined_at=joined_at,
                session_token=str(data.get("sessionToken") or secrets.token_urlsafe(24)),
                kills=int(data.get("kills", 0)),
                golden_apple_turns_remaining=int(data.get("goldenAppleTurnsRemaining", 0)),
                crown_blood_stacks=int(data.get("crownBloodStacks", 0)),
                battle_id=data.get("battleId"),
                battle_opponent_id=data.get("battleOpponentId"),
                battle_action=data.get("battleAction"),
            )
            if player.zone_id not in ZONES:
                player.zone_id = "zone_1"
            for item_data in data.get("inventory", []):
                if len(player.inventory) >= MAX_INVENTORY:
                    break  # checkpoints from before the one-item rule may hold more
                item_type = item_data.get("type")
                if item_type in ITEM_DEFINITIONS:
                    definition = ITEM_DEFINITIONS[item_type]
                    player.inventory.append(Item(
                        id=str(item_data["id"]),
                        type=definition.type,
                        name=definition.name,
                        description=definition.description,
                        uses_remaining=(int(item_data.get("usesRemaining", 7)) if definition.type == "TITAN_SHIELD" else None),
                    ))
            refresh_item_derived_state_locked(player)
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
        player.max_health = 100
        player.health = player.max_health
        player.attack = player.base_attack
        player.defense = player.base_defense
        player.agility = player.base_agility
        player.alive = True
        player.action_taken = False
        player.round_action_complete = False
        player.current_action = None
        player.action_deadline = None
        player.status_effect = "NORMAL"
        player.fear_turns_remaining = 0
        player.poison_stage = 0
        player.poison_turns_remaining = 0
        player.last_result = f"You begin in {ZONES[player.zone_id].name}."
        player.kills = 0
        player.inventory.clear()
        player.golden_apple_turns_remaining = 0
        player.crown_blood_stacks = 0
        player.battle_id = None
        player.battle_opponent_id = None
        player.battle_action = None

    for zone_id in game.zone_items:
        game.zone_items[zone_id].clear()
    game.hazard_zones.clear()
    game.hazard_counters = {zone_id: 0 for zone_id in OUTER_ZONE_IDS}

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
        f"Arena hazard active in {names}. Players there will take damage at round start.",
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
            refresh_item_derived_state_locked(player)
            if player.golden_apple_turns_remaining > 0:
                player.golden_apple_turns_remaining -= 1
            player.round_action_complete = False
            if get_battle_locked(player):
                player.current_action = "BATTLE"
                player.action_taken = True
                player.action_deadline = game.battles[player.battle_id].deadline if player.battle_id in game.battles else game.round_deadline
                player.last_result = f"Battle with {game.players[player.battle_opponent_id].name} continues. Choose a combat move." if player.battle_opponent_id in game.players else "Your battle continues."
                continue
            player.current_action = None
            player.action_taken = False
            player.action_deadline = game.round_deadline
            if player.status_effect == "ARMORED":
                player.status_effect = "NORMAL"
            player.last_result = f"Round {game.round_number} has begun. Choose your action."
        else:
            player.action_deadline = None

    hazard_events = _apply_hazards_at_round_start_locked()
    if game.alive_count <= 1:
        _finish_game_locked()
        return

    game.add_event(
        f"Round {game.round_number} has begun. Players have {ROUND_DURATION_SECONDS} seconds to act.",
        "ROUND_STARTED",
    )

    # Hazard events are already logged individually; return value retained for
    # readability/testing even though the shared event feed picks them up.
    _ = hazard_events


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
        player.round_action_complete = ended_action_round


def _end_battle_locked(battle_id: str, *, reason: str | None = None, ended_action_round: bool = True) -> None:
    battle = game.battles.pop(battle_id, None)
    if not battle:
        return
    # Any resolved/administratively ended battle consumes the current round's
    # action, even when the battle itself began in an earlier round.
    _clear_battle_players_locked(battle, ended_action_round=ended_action_round)
    if reason:
        game.add_event(reason, "BATTLE_ENDED", player_ids=battle.participant_ids())


def _finish_game_locked() -> None:
    alive_players = [p for p in game.players.values() if p.alive]
    if len(alive_players) == 1:
        game.winner_id = alive_players[0].id
        alive_players[0].last_result = "You are the last player standing."
        game.add_event(f"{alive_players[0].name} is the last player standing.", "GAME_OVER", broadcast=True)
    else:
        game.winner_id = None
        game.add_event("The game has ended with no player left standing.", "GAME_OVER", broadcast=True)
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
        return game.add_event(f"{player.name} was already eliminated.", "INFO", player_ids=(player.id,))

    phoenix_index = next((index for index, item in enumerate(player.inventory) if item.type == "PHOENIX_ASHES"), None)
    if phoenix_index is not None:
        player.inventory.pop(phoenix_index)
        refresh_item_derived_state_locked(player)
        player.health = max(1, int(round(player.max_health * 0.35)))
        player.alive = True
        player.status_effect = "NORMAL"
        player.last_result = f"Phoenix Ashes saved you. You revived with {player.health} HP."
        return game.add_event(
            f"Phoenix Ashes revived {player.name} with {player.health} HP.",
            "PLAYER_REVIVED",
            player_ids=(player.id,) if killer is None else (player.id, killer.id),
        )

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
        if has_item(killer, "CROWN_OF_BLOOD"):
            killer.crown_blood_stacks += 1
    involved = [player.id] + ([killer.id] if killer is not None else [])
    return game.add_event(reason, "PLAYER_ELIMINATED", player_ids=involved)


def _apply_hazards_at_round_start_locked() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    # Tidal Wave advances once per round for the whole zone, not once per player.
    # That keeps the 3-turn warning cycle identical whether zero, one, or many
    # players are standing in Zone 5.
    tidal_counter = None
    if "zone_5" in game.hazard_zones:
        tidal_counter = (game.hazard_counters.get("zone_5", 0) % 3) + 1
        game.hazard_counters["zone_5"] = tidal_counter

    for player in list(game.players.values()):
        if not player.alive:
            continue

        active_hazard = player.zone_id if player.zone_id in game.hazard_zones else None
        zone = ZONES[player.zone_id]

        # Poison is a lingering effect for two turns after each exposure. If the
        # player starts another turn in the fog, the poison escalates first.
        in_poison_fog = active_hazard == "zone_4"
        if in_poison_fog:
            player.poison_stage = min(3, max(1, player.poison_stage + 1))
            player.poison_turns_remaining = POISON_DURATION_TURNS
            messages = {
                1: "You feel a poison spreading throughout your body.",
                2: "A very potent poison is burning through your veins. You need to run!",
                3: "Your body is full of poison.",
            }
            labels = {1: "POISONED", 2: "BADLY POISONED", 3: "SEVERELY POISONED"}
            player.status_effect = labels[player.poison_stage]
            events.append(game.add_event(messages[player.poison_stage], "HAZARD_WARNING", player_ids=(player.id,)))
            player.last_result = messages[player.poison_stage]

        if player.poison_turns_remaining > 0:
            stage = max(1, min(3, player.poison_stage))
            damage = percent_damage_for(player, POISON_DAMAGE_PERCENTS[stage], agility_factor=False)
            old_health = player.health
            player.health = max(0, player.health - damage)
            player.poison_turns_remaining -= 1
            if player.poison_turns_remaining <= 0:
                player.poison_stage = 0
                if player.status_effect in {"POISONED", "BADLY POISONED", "SEVERELY POISONED"}:
                    player.status_effect = "NORMAL"
            player.last_result = f"{player.last_result} You take {old_health - player.health} poison damage." if old_health != player.health else player.last_result
            events.append(game.add_event(
                f"{player.name} took {old_health - player.health} poison damage in {zone.name}.",
                "HAZARD_DAMAGE",
                player_ids=(player.id,),
            ))
            if player.health <= 0:
                events.append(_eliminate_player_locked(
                    player,
                    f"{player.name} ({player.id}) was eliminated by poison.",
                ))
                continue

        # Blood Rain refreshes a five-turn fear effect each turn spent in it.
        if active_hazard == "zone_3":
            player.fear_turns_remaining = FEAR_DURATION_TURNS
            player.last_result = "The Blood Rain fills you with fear. All stats are reduced by 30% for 5 turns."
            events.append(game.add_event(
                "The Blood Rain affected you with FEAR: all stats are reduced by 30% for 5 turns.",
                "HAZARD_FEAR",
                player_ids=(player.id,),
            ))

        # Lightning: a chance to be hit, with agility improving survival.
        if active_hazard == "zone_1":
            chance = max(0.15, min(0.75, 0.45 - (effective_stat(player, "agility") - STAT_BASELINE) * 0.025))
            if rng.random() < chance:
                damage = percent_damage_for(player, LIGHTNING_DAMAGE_PERCENT, agility_factor=False)
                old_health = player.health
                player.health = max(0, player.health - damage)
                player.last_result = f"Lightning struck you for {old_health - player.health} damage!"
                events.append(game.add_event(
                    f"{player.name} was struck by lightning for {old_health - player.health} damage in {zone.name}.",
                    "HAZARD_DAMAGE",
                    player_ids=(player.id,),
                ))
                if player.health <= 0:
                    events.append(_eliminate_player_locked(player, f"{player.name} ({player.id}) was eliminated by a lightning strike in {zone.name}."))
                    continue
            else:
                player.last_result = "You hear thunder overhead, but the lightning misses you."
                events.append(game.add_event("Lightning cracked nearby, but you were spared.", "HAZARD_MISSED", player_ids=(player.id,)))

        # Tracker Jackers: a chance to be stung for 30% damage.
        elif active_hazard == "zone_2":
            chance = max(0.20, min(0.80, 0.50 - (effective_stat(player, "agility") - STAT_BASELINE) * 0.03))
            if rng.random() < chance:
                damage = percent_damage_for(player, TRACKER_JACKER_DAMAGE_PERCENT, agility_factor=False)
                old_health = player.health
                player.health = max(0, player.health - damage)
                player.last_result = f"Tracker Jacker Wasps stung you for {old_health - player.health} damage!"
                events.append(game.add_event(
                    f"Tracker Jacker Wasps stung {player.name} for {old_health - player.health} damage in {zone.name}.",
                    "HAZARD_DAMAGE",
                    player_ids=(player.id,),
                ))
                if player.health <= 0:
                    events.append(_eliminate_player_locked(player, f"{player.name} ({player.id}) was eliminated by Tracker Jacker Wasps in {zone.name}."))
                    continue
            else:
                player.last_result = "You hear the Tracker Jacker swarm, but the wasps miss you."
                events.append(game.add_event("The Tracker Jacker swarm passes without a sting.", "HAZARD_MISSED", player_ids=(player.id,)))

        # Tidal Wave counter: one warning per turn, then the wave hits on 3.
        elif active_hazard == "zone_5":
            counter = tidal_counter or 1
            if counter == 1:
                player.last_result = "You feel a small vibration in the ground."
                events.append(game.add_event("You feel a small vibration in the ground.", "HAZARD_WARNING", player_ids=(player.id,)))
            elif counter == 2:
                player.last_result = "You see a wave of water in the distance."
                events.append(game.add_event("You see a wave of water in the distance.", "HAZARD_WARNING", player_ids=(player.id,)))
            else:
                damage = percent_damage_for(player, TIDAL_DAMAGE_PERCENT)
                old_health = player.health
                player.health = max(0, player.health - damage)
                player.last_result = f"A huge tidal wave slams into you for {old_health - player.health} damage!"
                events.append(game.add_event(
                    f"A huge tidal wave slammed into {player.name} for {old_health - player.health} damage in {zone.name}.",
                    "HAZARD_DAMAGE",
                    player_ids=(player.id,),
                ))
                if player.health <= 0:
                    events.append(_eliminate_player_locked(player, f"{player.name} ({player.id}) was eliminated by the tidal wave in {zone.name}."))
                    continue

        # Monkey Mutts: random 5–20% hit, kept within that range after agility.
        elif active_hazard == "zone_6":
            base_percent = rng.uniform(0.05, 0.20)
            agility_scale = 1.0 - (effective_stat(player, "agility") - STAT_BASELINE) * 0.025
            final_percent = max(0.05, min(0.20, base_percent * agility_scale))
            damage = max(1, int(round(player.max_health * final_percent * environment_damage_multiplier(player))))
            old_health = player.health
            player.health = max(0, player.health - damage)
            player.last_result = f"Monkey Mutts attacked you for {old_health - player.health} damage!"
            events.append(game.add_event(
                f"Monkey Mutts attacked {player.name} for {old_health - player.health} damage in {zone.name}.",
                "HAZARD_DAMAGE",
                player_ids=(player.id,),
            ))
            if player.health <= 0:
                events.append(_eliminate_player_locked(player, f"{player.name} ({player.id}) was eliminated by Monkey Mutts in {zone.name}."))
                continue

        # Count this turn against FEAR. Blood Rain refreshes the effect to five
        # turns at the start, then the current turn consumes one of them.
        if player.fear_turns_remaining > 0:
            player.fear_turns_remaining = max(0, player.fear_turns_remaining - 1)

    if tidal_counter == 3:
        game.hazard_counters["zone_5"] = 0

    return events


def _all_round_actions_complete_locked() -> bool:
    if game.status != GameStatus.ACTIVE or game.alive_count <= 1:
        return False
    return all(player.round_action_complete for player in game.players.values() if player.alive)


def _resolve_round_locked(*, eliminate_missed: bool = True) -> None:
    if game.status != GameStatus.ACTIVE:
        return

    # If the round timer ends while a battle turn is still waiting, resolve missing
    # combat choices as DEFEND so battles also complete their current round action.
    if eliminate_missed:
        for battle in list(game.battles.values()):
            if len(battle.actions) < 2:
                for player_id in battle.participant_ids():
                    player = game.players.get(player_id)
                    if player and player.alive and player_id not in battle.actions:
                        battle.actions[player_id] = "BATTLE_DEFEND"
                        player.battle_action = "BATTLE_DEFEND"
                        player.last_result = "The round ended while you were thinking, so you defended automatically."
                _resolve_battle_turn_locked(battle)

    missed: list[Player] = [
        player
        for player in game.players.values()
        if player.alive and not player.round_action_complete and not player.battle_id
    ]

    if eliminate_missed:
        for player in missed:
            _eliminate_player_locked(
                player,
                f"{player.name} ({player.id}) failed to act before the timer expired and was eliminated.",
            )

    if game.alive_count <= 1:
        _finish_game_locked()
        return

    if not eliminate_missed:
        game.add_event("All living players have acted. The round ends automatically.", "ROUND_AUTO_END")

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
        allowed_battle_actions = set(BATTLE_ACTION_SET)
        if player.inventory and player.inventory[0].type in CONSUMABLE_ITEM_TYPES:
            allowed_battle_actions.add("BATTLE_USE_ITEM")
        if action not in allowed_battle_actions:
            return False, "You are engaged in battle. Only ATTACK, DEFEND, RUN, or a consumable item are available."
        if action == "BATTLE_USE_ITEM" and player.inventory[0].type == "MEDKIT" and player.health >= player.max_health:
            return False, "Your health is already full."
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
        if has_item(player, "CROWN_OF_BLOOD") and player.zone_id == "cornucopia" and target_zone_id != "cornucopia":
            return False, "The Crown Of Blood cannot leave the Cornucopia while equipped."
        allowed_zones = set(ZONES[player.zone_id].connected_zones)
        if has_item(player, "ADVENTURERS_BOOTS") and player.zone_id != "cornucopia":
            ring = list(OUTER_ZONE_IDS)
            if player.zone_id in ring:
                index = ring.index(player.zone_id)
                for offset in (-2, -1, 1, 2):
                    candidate = ring[(index + offset) % len(ring)]
                    allowed_zones.add(candidate)
        if target_zone_id not in allowed_zones:
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

    if action == "USE_ITEM":
        if not item_id:
            return False, "Choose an item to use."
        item = next((candidate for candidate in player.inventory if candidate.id == item_id), None)
        if item is None:
            return False, "That item is no longer in your inventory."
        if item.type == "MEDKIT" and player.health >= player.max_health:
            return False, "Your health is already full."
        if item.type not in CONSUMABLE_ITEM_TYPES:
            return False, "That item is automatically active while you hold it."

    return True, ""


def _attack_damage_locked(attacker: Player, defender: Player, *, defense: bool = False) -> tuple[int, bool, int]:
    # Tuned so equal mid-stat players usually need about four to five landed hits.
    raw = max(1, 11 + int(round(effective_stat(attacker, "attack"))) + rng.randint(-2, 2))
    critical = rng.random() < 0.10
    if critical:
        raw += 5

    raw = max(1, int(round(raw * mitigation_multiplier(effective_stat(defender, "defense")))))

    # Agility provides a small baseline dodge chance; Shadow Cloak makes that chance 35% higher.
    base_dodge = max(0.03, min(0.30, 0.08 + (effective_stat(defender, "agility") - STAT_BASELINE) * 0.012))
    dodge_chance = base_dodge * 1.35 if has_item(defender, "SHADOW_CLOAK") else base_dodge
    if rng.random() < min(0.45, dodge_chance):
        return 0, False, 0

    reduction = 0
    if defense:
        reduction += max(4, int(round(raw * 0.50)))
    if defender.status_effect == "ARMORED":
        reduction += 8
        defender.status_effect = "NORMAL"

    shield = next((item for item in defender.inventory if item.type == "TITAN_SHIELD"), None)
    if shield is not None:
        raw = max(1, int(round(raw * 0.60)))
        shield.uses_remaining = max(0, (shield.uses_remaining if shield.uses_remaining is not None else 7) - 1)
        if shield.uses_remaining == 0:
            defender.inventory.remove(shield)
            defender.last_result = "Your Titan Shield broke after 7 uses."
            refresh_item_derived_state_locked(defender)

    if has_item(defender, "HUNTERS_FEATHER"):
        raw = max(1, int(round(raw * 1.50)))

    return max(1, raw - reduction), critical, reduction


def _apply_serpentine_poison_on_hit_locked(attacker: Player, defender: Player) -> dict[str, Any] | None:
    if not has_item(attacker, "SERPENTINE_DAGGER") or not defender.alive:
        return None
    defender.poison_stage = max(1, defender.poison_stage)
    defender.poison_turns_remaining = max(2, defender.poison_turns_remaining)
    defender.status_effect = "POISONED"
    defender.last_result = f"{attacker.name}'s Serpentine Dagger poisoned you."
    return game.add_event(
        f"{attacker.name} applied POISONED to {defender.name} with the Serpentine Dagger.",
        "STATUS_APPLIED",
        player_ids=(attacker.id, defender.id),
    )


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

    def _log(message: str, event_type: str = "INFO") -> dict[str, Any]:
        return game.add_event(message, event_type, player_ids=(player_a.id, player_b.id))

    events: list[dict[str, Any]] = []

    # Consumables may be used as a battle move. They resolve before attack damage
    # for this turn, and using one consumes the combatant's move.
    if action_a == "BATTLE_USE_ITEM" and player_a.inventory:
        events.append(_use_item_locked(player_a, player_a.inventory[0].id))
    if action_b == "BATTLE_USE_ITEM" and player_b.inventory:
        events.append(_use_item_locked(player_b, player_b.inventory[0].id))

    if action_a == "BATTLE_RUN" or action_b == "BATTLE_RUN":
        runners = [(player_a, player_b, action_a), (player_b, player_a, action_b)]
        for runner, opponent, runner_action in runners:
            if runner_action != "BATTLE_RUN":
                continue
            chance = max(0.20, min(0.85, 0.50 + (effective_stat(runner, "agility") - effective_stat(opponent, "agility")) * 0.04))
            success = rng.random() < chance
            if success:
                runner.last_result = f"You escaped from {opponent.name}."
                opponent.last_result = f"{runner.name} escaped from the battle."
                event = _log(
                    f"{runner.name} escaped the battle with {opponent.name}.",
                    "BATTLE_RUN",
                )
                events.append(event)
                _end_battle_locked(battle.id)
                return events
            runner.last_result = f"You tried to run from {opponent.name}, but failed."
            opponent.last_result = f"{runner.name} tried to run, but failed."
            events.append(_log(
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
                if damage == 0:
                    events.append(_log(f"{player_a.name} dodged {player_b.name}'s attack while trying to run.", "BATTLE_DODGE"))
                else:
                    events.append(_log(
                        f"{player_b.name} struck {player_a.name} for {damage} damage as they tried to run.{(' Critical hit.' if critical else '')}{(' Armor helped.' if reduction else '')}",
                        "BATTLE_HIT",
                    ))
                if damage > 0:
                    poison_event = _apply_serpentine_poison_on_hit_locked(player_b, player_a)
                    if poison_event:
                        events.append(poison_event)
                if player_a.health <= 0:
                    events.append(_eliminate_player_locked(player_a, f"{player_a.name} ({player_a.id}) was eliminated by {player_b.name} in battle.", killer=player_b))
                    if not player_a.alive:
                        if game.alive_count <= 1:
                            _finish_game_locked()
                        return events
        elif action_b == "BATTLE_RUN" and action_a == "BATTLE_ATTACK" and player_b.alive:
            damage, critical, reduction = _attack_damage_locked(player_a, player_b)
            player_b.health = max(0, player_b.health - damage)
            player_a.last_result = f"You caught {player_b.name} while they ran for {damage} damage."
            player_b.last_result = f"You failed to run and took {damage} damage from {player_a.name}."
            if damage == 0:
                events.append(_log(f"{player_b.name} dodged {player_a.name}'s attack while trying to run.", "BATTLE_DODGE"))
            else:
                events.append(_log(
                    f"{player_a.name} struck {player_b.name} for {damage} damage as they tried to run.{(' Critical hit.' if critical else '')}{(' Armor helped.' if reduction else '')}",
                    "BATTLE_HIT",
                ))
            if damage > 0:
                poison_event = _apply_serpentine_poison_on_hit_locked(player_a, player_b)
                if poison_event:
                    events.append(poison_event)
            if player_b.health <= 0:
                events.append(_eliminate_player_locked(player_b, f"{player_b.name} ({player_b.id}) was eliminated by {player_a.name} in battle.", killer=player_a))
                if not player_b.alive:
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
            if damage == 0:
                events.append(_log(f"{player_b.name} dodged {player_a.name}'s attack.", "BATTLE_DODGE"))
            else:
                events.append(_log(
                    f"{player_a.name} attacked {player_b.name} for {damage} damage.{(' Critical hit.' if critical else '')}{(' Defense/armor reduced the hit.' if reduction else '')}",
                    "BATTLE_HIT",
                ))
            if damage > 0:
                poison_event = _apply_serpentine_poison_on_hit_locked(player_a, player_b)
                if poison_event:
                    events.append(poison_event)
            if player_b.health <= 0:
                events.append(_eliminate_player_locked(player_b, f"{player_b.name} ({player_b.id}) was eliminated by {player_a.name} in battle.", killer=player_a))
                if not player_b.alive:
                    if game.alive_count <= 1:
                        _finish_game_locked()
                    return events
        if action_b == "BATTLE_ATTACK" and player_a.alive and player_b.alive:
            damage, critical, reduction = _attack_damage_locked(player_b, player_a, defense=action_a == "BATTLE_DEFEND")
            player_a.health = max(0, player_a.health - damage)
            player_b.last_result = f"You attacked {player_a.name} for {damage} damage."
            player_a.last_result = f"{player_b.name} attacked you for {damage} damage."
            if damage == 0:
                events.append(_log(f"{player_a.name} dodged {player_b.name}'s attack.", "BATTLE_DODGE"))
            else:
                events.append(_log(
                    f"{player_b.name} attacked {player_a.name} for {damage} damage.{(' Critical hit.' if critical else '')}{(' Defense/armor reduced the hit.' if reduction else '')}",
                    "BATTLE_HIT",
                ))
            if damage > 0:
                poison_event = _apply_serpentine_poison_on_hit_locked(player_b, player_a)
                if poison_event:
                    events.append(poison_event)
            if player_a.health <= 0:
                events.append(_eliminate_player_locked(player_a, f"{player_a.name} ({player_a.id}) was eliminated by {player_b.name} in battle.", killer=player_b))
                if not player_a.alive:
                    if game.alive_count <= 1:
                        _finish_game_locked()
                    return events

        if action_a == action_b == "BATTLE_DEFEND" and player_a.alive and player_b.alive:
            player_a.last_result = f"You defended against {player_b.name}."
            player_b.last_result = f"You defended against {player_a.name}."
            events.append(_log(
                f"{player_a.name} and {player_b.name} both held their ground.",
                "BATTLE_DEFEND",
            ))

    for player in (player_a, player_b):
        if player.alive:
            player.battle_action = None
            player.round_action_complete = True

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
        battle_options = "ATTACK, DEFEND, RUN"
        if player.inventory and player.inventory[0].type in CONSUMABLE_ITEM_TYPES:
            battle_options += ", or USE ITEM"
        player.last_result = f"Battle turn {battle.turn_number}: choose {battle_options}."
    events.append(_log(
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
        heal = int(round(player.max_health * 0.50))
        player.health = min(player.max_health, player.health + heal)
        player.last_result = f"You used a Medkit and recovered {player.health - old_health} health."
    elif item.type == "GOLDEN_APPLE":
        player.golden_apple_turns_remaining = 5
        player.last_result = "You ate the Golden Apple. All combat stats are boosted by 50% for 5 turns."
    else:
        raise HTTPException(status_code=409, detail="That item is automatically active while you hold it.")

    player.inventory.pop(item_index)
    refresh_item_derived_state_locked(player)
    return game.add_event(f"{player.name} used a {item.name} in {ZONES[player.zone_id].name}.", "ITEM_USED", player_ids=(player.id,))


def _grab_item_locked(player: Player) -> dict[str, Any]:
    """Take the offered Cornucopia item, or swap it for the item you are holding."""
    if player.zone_id != "cornucopia":
        raise HTTPException(status_code=409, detail="Items can only be collected at the Cornucopia.")
    if not game.zone_items["cornucopia"]:
        player.last_result = "The Cornucopia is empty."
        raise HTTPException(status_code=409, detail="The Cornucopia is empty.")

    item = game.zone_items["cornucopia"].pop(0)
    if len(player.inventory) >= MAX_INVENTORY:
        dropped = player.inventory.pop(0)
        game.zone_items["cornucopia"].append(dropped)  # goes to the back of the pile
        player.inventory.append(item)
        refresh_item_derived_state_locked(player)
        player.last_result = f"You swapped your {dropped.name} for a {item.name}. {item.description}"
        message = f"{player.name} swapped a {dropped.name} for a {item.name} at the Cornucopia."
        return game.add_event(message, "ITEM_SWAPPED", player_ids=(player.id,))

    player.inventory.append(item)
    refresh_item_derived_state_locked(player)
    player.last_result = f"You grabbed a {item.name}. {item.description}"
    return game.add_event(f"{player.name} grabbed a {item.name} from the Cornucopia.", "ITEM_GRABBED", player_ids=(player.id,))


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
        # Only the chooser sees this: moves are simultaneous, so the opponent must not learn it.
        event = game.add_event(
            f"{player.name} chose {action.replace('BATTLE_', '')} in battle.",
            "BATTLE_ACTION",
            player_ids=(player.id,),
        )
        if len(battle.actions) == 2:
            battle_events = _resolve_battle_turn_locked(battle)
            event = battle_events[-1] if battle_events else event
        return event

    player.current_action = action
    player.action_taken = True
    player.round_action_complete = True
    player.action_deadline = None

    if action == "MOVE":
        previous = player.zone_id
        player.zone_id = target_zone_id or player.zone_id
        player.last_result = f"You moved from {ZONES[previous].name} to {ZONES[player.zone_id].name}."
        event = game.add_event(f"{player.name} moved to {ZONES[player.zone_id].name}.", "PLAYER_MOVED", player_ids=(player.id,))
    elif action == "REST":
        old_health = player.health
        player.health = min(player.max_health, player.health + 5)
        player.last_result = f"You rested and recovered {player.health - old_health} health."
        event = game.add_event(f"{player.name} rested in {ZONES[player.zone_id].name}.", "PLAYER_RESTED", player_ids=(player.id,))
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
        player.round_action_complete = False
        player.action_deadline = battle.deadline
        target.battle_id = battle.id
        target.battle_opponent_id = player.id
        target.battle_action = None
        target.current_action = "BATTLE"
        target.action_taken = True
        target.round_action_complete = False
        target.action_deadline = battle.deadline
        player.last_result = f"You engaged {target.name}. Choose ATTACK, DEFEND, or RUN."
        target.last_result = f"{player.name} attacked you and you are now engaged. Choose ATTACK, DEFEND, or RUN."
        event = game.add_event(
            f"{player.name} engaged {target.name} in battle in {ZONES[player.zone_id].name}.",
            "BATTLE_STARTED",
            player_ids=(player.id, target.id),
        )
    elif action == "USE_ITEM":
        event = _use_item_locked(player, item_id or "")
    elif action == "GRAB_ITEM":
        event = _grab_item_locked(player)
    else:  # WAIT
        player.last_result = "You waited and watched your surroundings."
        event = game.add_event(f"{player.name} waited in {ZONES[player.zone_id].name}.", "PLAYER_WAITED", player_ids=(player.id,))

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


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    return {key: event[key] for key in ("id", "timestamp", "type", "message")}


async def sync_action_result(player_id: str, event: dict[str, Any]) -> None:
    async with state_lock:
        snapshot = player_state(game.players[player_id]) if player_id in game.players else None
        admin_snapshot = admin_state()
        spectator_snapshot = spectate_state()

    sends = [
        manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot}),
        manager.broadcast_spectators({"type": "SPECTATE_STATE", "data": spectator_snapshot}),
    ]
    if snapshot is not None:
        sends.append(manager.send_player(player_id, {"type": "GAME_STATE", "data": snapshot}))
    if event.get("broadcast"):
        sends.append(manager.broadcast_players({"type": "PUBLIC_EVENT", "data": _public_event(event)}))
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

        district = next_district_for_gender_locked(request.gender)
        if district is None:
            raise HTTPException(status_code=409, detail=f"All 12 {request.gender} district slots are already filled.")

        levels = resolve_stat_choice(request.stats)
        rolled = {stat: roll_stat(level) for stat, level in levels.items()}
        player = Player(
            id=next_player_id(),
            name=name,
            gender=request.gender,
            district=district,
            attack=rolled["attack"],
            defense=rolled["defense"],
            agility=rolled["agility"],
            base_attack=rolled["attack"],
            base_defense=rolled["defense"],
            base_agility=rolled["agility"],
        )
        game.players[player.id] = player
        _write_checkpoint_locked()
        game.add_event(f"{player.name} joined the arena ({player.id}).", "PLAYER_JOINED", player_ids=(player.id,))
        player_snapshot = player_state(player)
        current_game = {
            "gameId": game.game_id,
            "status": game.status.value,
            "phase": game.phase.value,
            "round": game.round_number,
            "playerCount": len(game.players),
            "maxPlayers": MAX_PLAYERS,
        }
        admin_snapshot = admin_state()
        spectator_snapshot = spectate_state()

    await asyncio.gather(
        manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot}),
        manager.broadcast_spectators({"type": "SPECTATE_STATE", "data": spectator_snapshot}),
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
        round_before = game.round_number
        if _all_round_actions_complete_locked():
            _resolve_round_locked(eliminate_missed=False)
        round_changed = game.round_number != round_before
        snapshot = player_state(player)
        _write_checkpoint_locked()

    # When the last player acts, the server may advance the round immediately.
    # A normal action sync only updates the actor (plus directly related players),
    # which can leave other clients — especially mobile clients — displaying the
    # old round/deadline until they refresh. Push the complete new state to every
    # connected player whenever an early round transition occurs.
    if round_changed:
        await sync_everyone_full()
    else:
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
            game.add_event("The arena has begun.", "GAME_STARTED", broadcast=True)
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
            game.add_event("The arena has been paused by the admin.", "GAME_PAUSED", broadcast=True)
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
            game.add_event("The arena has resumed.", "GAME_RESUMED", broadcast=True)
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
        elif action in {"TRIGGER_HAZARD", "TOGGLE_HAZARD"}:
            if game.status not in {GameStatus.ACTIVE, GameStatus.PAUSED}:
                raise HTTPException(status_code=409, detail="Hazards can only be changed during the game")
            target = request.targetZoneId
            if not target or target not in OUTER_ZONE_IDS:
                raise HTTPException(status_code=400, detail="Choose one of the six outer zones")
            if action == "TRIGGER_HAZARD" or target not in game.hazard_zones:
                game.hazard_zones.add(target)
                if target == "zone_5":
                    game.hazard_counters["zone_5"] = 0
                selected_event = game.add_event(
                    f"Admin activated {ZONES[target].hazard_name} in {ZONES[target].name}.",
                    "ARENA_HAZARD",
                    broadcast=True,
                )
            else:
                game.hazard_zones.remove(target)
                if target == "zone_5":
                    game.hazard_counters["zone_5"] = 0
                selected_event = game.add_event(
                    f"Admin deactivated {ZONES[target].hazard_name} in {ZONES[target].name}.",
                    "ARENA_HAZARD_CLEARED",
                    broadcast=True,
                )
        elif action == "CLEAR_HAZARDS":
            game.hazard_zones.clear()
            game.hazard_counters = {zone_id: 0 for zone_id in OUTER_ZONE_IDS}
            selected_event = game.add_event("The admin cleared all active arena hazards.", "ARENA_HAZARD_CLEARED", broadcast=True)
        elif action == "BROADCAST_ANNOUNCEMENT":
            message = " ".join((request.message or "").strip().split())
            if not message:
                raise HTTPException(status_code=400, detail="Enter an announcement message")
            selected_event = game.add_event(f"ARENA ANNOUNCEMENT: {message}", "ANNOUNCEMENT", broadcast=True)
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
                selected_event = game.add_event(f"{target.name} ({target.id}) was restored to the arena by the admin.", "PLAYER_RESTORED", player_ids=(target.id,))
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
                selected_event = game.add_event(f"Admin moved {target.name} ({target.id}) to {ZONES[destination].name}.", "PLAYER_MOVED_ADMIN", player_ids=(target.id,))
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
                selected_event = game.add_event(f"Admin set {target.name} ({target.id}) health to {target.health}.", "PLAYER_HEALTH_SET", player_ids=(target.id,))
            elif action == "SET_STATS":
                new_agility = request.agility if request.agility is not None else request.speed
                if (
                    request.attack is None
                    and request.defense is None
                    and new_agility is None
                    and request.value is None
                ):
                    raise HTTPException(status_code=400, detail="Provide at least one stat value")
                if target.battle_id:
                    _end_battle_locked(target.battle_id, reason=f"The battle involving {target.name} ended after an admin stat change.")
                if request.value is not None:
                    target.health = min(target.max_health, request.value)
                if request.attack is not None:
                    target.attack = request.attack
                if request.defense is not None:
                    target.defense = request.defense
                if new_agility is not None:
                    target.agility = new_agility
                if target.health > 0 and not target.alive and game.status != GameStatus.GAME_OVER:
                    target.alive = True
                    target.status_effect = "NORMAL"
                target.last_result = "An admin updated your arena stats."
                selected_event = game.add_event(f"Admin updated {target.name} ({target.id}) stats.", "PLAYER_STATS_SET", player_ids=(target.id,))
            elif action == "GIVE_ITEM":
                item_type = (request.itemType or "").upper()
                if item_type not in ITEM_DEFINITIONS:
                    raise HTTPException(status_code=400, detail="Choose a valid item type")
                if len(target.inventory) >= MAX_INVENTORY:
                    raise HTTPException(status_code=409, detail="That player's inventory is full")
                if item_type == "CROWN_OF_BLOOD" and target.zone_id != "cornucopia":
                    raise HTTPException(status_code=409, detail="Crown Of Blood can only be equipped at the Cornucopia.")
                item = make_item(item_type)
                target.inventory.append(item)
                refresh_item_derived_state_locked(target)
                target.last_result = f"An admin gave you a {item.name}."
                selected_event = game.add_event(f"Admin gave {target.name} ({target.id}) a {item.name}.", "ITEM_GRANTED", player_ids=(target.id,))
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
            game.hazard_counters = {zone_id: 0 for zone_id in OUTER_ZONE_IDS}
            game.add_event("The arena has been reset. Waiting for players.", "GAME_RESET")
        else:
            raise HTTPException(status_code=400, detail="Unsupported admin action")

        _write_checkpoint_locked()
        snapshot = admin_state()

    if reset_requested:
        await manager.close_all_players({"type": "RESET"})
    if selected_event and action == "BROADCAST_ANNOUNCEMENT":
        await manager.broadcast_players({"type": "PUBLIC_EVENT", "data": _public_event(selected_event)})
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
            admin_snapshot = None
            spectator_snapshot = None
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
