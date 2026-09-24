from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "200"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
ROUND_DURATION_SECONDS = max(5, int(os.getenv("ROUND_DURATION_SECONDS", "15")))
FINAL_PLAYER_THRESHOLD = max(2, int(os.getenv("FINAL_PLAYER_THRESHOLD", "20")))


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
    connected: bool = True
    current_action: str | None = None
    action_taken: bool = False
    action_deadline: datetime | None = None
    status_effect: str = "NORMAL"
    last_result: str = "Waiting for the game to begin."
    joined_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    session_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    kills: int = 0

    def public_dict(self) -> dict[str, Any]:
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
                # Look up the owning player when cleaning the socket.
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
            if (
                game.status == GameStatus.ACTIVE
                and game.round_deadline is not None
                and datetime.now(timezone.utc) >= game.round_deadline
            ):
                _resolve_round_locked()
                should_sync = True

        if should_sync:
            await sync_everyone_full()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global round_task
    round_task = asyncio.create_task(round_loop())
    yield
    if round_task:
        round_task.cancel()
        try:
            await round_task
        except asyncio.CancelledError:
            pass
        round_task = None


app = FastAPI(title="Arena RPG API", version="0.2.0", lifespan=lifespan)
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


class PlayerActionRequest(BaseModel):
    playerId: str = Field(min_length=1, max_length=16)
    action: str
    targetZoneId: str | None = None


# ---------------------------------------------------------------------------
# Game engine helpers
# ---------------------------------------------------------------------------

ACTION_SET = {"MOVE", "SEARCH", "REST", "HIDE", "SCOUT", "WAIT"}


def clean_name(name: str) -> str:
    return " ".join(name.strip().split())


def next_player_id() -> str:
    index = len(game.players) + 1
    while f"P-{index:03d}" in game.players:
        index += 1
    return f"P-{index:03d}"


def available_actions(player: Player) -> list[str]:
    if not player.alive or game.status != GameStatus.ACTIVE or player.action_taken:
        return []
    return sorted(ACTION_SET)


def public_zone_dict(zone: ZoneDefinition) -> dict[str, Any]:
    return {
        "id": zone.id,
        "name": zone.name,
        "description": zone.description,
        "connectedZones": list(zone.connected_zones),
    }


def player_state(player: Player) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "gameId": game.game_id,
        "status": game.status.value,
        "phase": game.phase.value,
        "round": game.round_number,
        "roundDurationSeconds": ROUND_DURATION_SECONDS,
        "serverNow": now.isoformat(),
        "roundDeadline": game.round_deadline.isoformat() if game.round_deadline else None,
        "player": player.public_dict(),
        "availableActions": available_actions(player),
        "currentZone": public_zone_dict(ZONES[player.zone_id]),
        "adjacentZones": [public_zone_dict(ZONES[zone_id]) for zone_id in ZONES[player.zone_id].connected_zones],
        "playerCount": len(game.players),
        "aliveCount": game.alive_count,
        "maxPlayers": MAX_PLAYERS,
        "winnerId": game.winner_id,
        "events": game.event_log[-30:],
    }


def admin_state() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    zone_counts = game.zone_counts
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
        "maxPlayers": MAX_PLAYERS,
        "winnerId": game.winner_id,
        "zones": [
            {
                **public_zone_dict(zone),
                "playerCount": zone_counts[zone.id],
            }
            for zone in ZONES.values()
        ],
        "players": [p.admin_dict() for p in game.players.values()],
        "events": game.event_log[-100:],
    }


def assign_starting_zones_locked() -> None:
    for index, player in enumerate(game.players.values()):
        player.zone_id = OUTER_ZONE_IDS[index % len(OUTER_ZONE_IDS)]
        player.health = player.max_health
        player.attack = 10
        player.speed = 10
        player.alive = True
        player.action_taken = False
        player.current_action = None
        player.status_effect = "NORMAL"
        player.last_result = f"You begin in {ZONES[player.zone_id].name}."
        player.kills = 0


def _set_phase_locked() -> None:
    if game.alive_count <= 1:
        game.phase = GamePhase.GAME_OVER
    elif game.round_number <= 1:
        # The first round is always the opening, even if a tiny test game
        # happens to be below the final-player threshold.
        game.phase = GamePhase.OPENING
    elif game.alive_count <= FINAL_PLAYER_THRESHOLD:
        game.phase = GamePhase.FINAL
    else:
        game.phase = GamePhase.MAIN


def _begin_round_locked() -> None:
    if game.alive_count <= 1:
        _finish_game_locked()
        return

    game.round_deadline = datetime.now(timezone.utc) + timedelta(seconds=ROUND_DURATION_SECONDS)
    game.paused_remaining_seconds = None
    _set_phase_locked()

    for player in game.players.values():
        if player.alive:
            player.current_action = None
            player.action_taken = False
            player.action_deadline = game.round_deadline
            player.status_effect = "NORMAL"
            player.last_result = f"Round {game.round_number} has begun. Choose your action."
        else:
            player.action_deadline = None

    game.add_event(
        f"Round {game.round_number} has begun. Players have {ROUND_DURATION_SECONDS} seconds to act.",
        "ROUND_STARTED",
    )


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

    for player in game.players.values():
        player.action_deadline = None
        player.action_taken = True


def _eliminate_player_locked(player: Player, reason: str) -> dict[str, Any]:
    if not player.alive:
        return game.add_event(f"{player.name} was already eliminated.", "INFO")
    player.alive = False
    player.health = 0
    player.action_deadline = None
    player.action_taken = True
    player.current_action = None
    player.status_effect = "ELIMINATED"
    player.last_result = reason
    return game.add_event(reason, "PLAYER_ELIMINATED")


def _resolve_round_locked() -> None:
    if game.status != GameStatus.ACTIVE:
        return

    missed: list[Player] = [
        player
        for player in game.players.values()
        if player.alive and not player.action_taken
    ]

    for player in missed:
        _eliminate_player_locked(
            player,
            f"{player.name} ({player.id}) failed to act before the timer expired and was eliminated.",
        )

    if game.alive_count <= 1:
        _finish_game_locked()
        return

    game.round_number += 1
    _begin_round_locked()


def _validate_player_action_locked(
    player: Player,
    action: str,
    target_zone_id: str | None,
) -> tuple[bool, str]:
    action = action.upper()

    if game.status != GameStatus.ACTIVE:
        return False, "The game is not currently accepting player actions."
    if not player.alive:
        return False, "You have been eliminated."
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

    return True, ""


def _apply_player_action_locked(
    player: Player,
    action: str,
    target_zone_id: str | None,
) -> dict[str, Any]:
    action = action.upper()
    valid, error = _validate_player_action_locked(player, action, target_zone_id)
    if not valid:
        raise HTTPException(status_code=409, detail=error)

    player.current_action = action
    player.action_taken = True
    player.action_deadline = None

    if action == "MOVE":
        previous = player.zone_id
        player.zone_id = target_zone_id or player.zone_id
        player.last_result = f"You moved from {ZONES[previous].name} to {ZONES[player.zone_id].name}."
        event = game.add_event(
            f"{player.name} moved to {ZONES[player.zone_id].name}.",
            "PLAYER_MOVED",
        )
    elif action == "REST":
        old_health = player.health
        player.health = min(player.max_health, player.health + 5)
        player.last_result = f"You rested and recovered {player.health - old_health} health."
        event = game.add_event(f"{player.name} rested in {ZONES[player.zone_id].name}.", "PLAYER_RESTED")
    elif action == "HIDE":
        player.status_effect = "HIDDEN"
        player.last_result = "You found cover and are hidden for this round."
        event = game.add_event(f"{player.name} disappeared into cover in {ZONES[player.zone_id].name}.", "PLAYER_HID")
    elif action == "SCOUT":
        adjacent = ZONES[player.zone_id].connected_zones
        counts = [f"{ZONES[zone_id].name}: {game.zone_counts[zone_id]}" for zone_id in adjacent]
        player.last_result = "Nearby population: " + ", ".join(counts) + "."
        event = game.add_event(f"{player.name} scouted the area.", "PLAYER_SCOUTED")
    elif action == "SEARCH":
        player.last_result = "You searched the area but found nothing useful yet. Items arrive in Phase 3."
        event = game.add_event(f"{player.name} searched {ZONES[player.zone_id].name}.", "PLAYER_SEARCHED")
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

    await asyncio.gather(
        *(manager.send_player(pid, {"type": "GAME_STATE", "data": snapshot}) for pid, snapshot in player_snapshots.items()),
        manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot}),
    )


async def sync_action_result(player_id: str, event: dict[str, Any]) -> None:
    async with state_lock:
        snapshot = player_state(game.players[player_id]) if player_id in game.players else None
        admin_snapshot = admin_state()

    sends = [manager.broadcast_players({"type": "PUBLIC_EVENT", "data": event}), manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot})]
    if snapshot is not None:
        sends.append(manager.send_player(player_id, {"type": "GAME_STATE", "data": snapshot}))
    await asyncio.gather(*sends)


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
        return [public_zone_dict(zone) for zone in ZONES.values()]


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
    )

    return {
        "player": player.public_dict(),
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
        event = _apply_player_action_locked(player, request.action, request.targetZoneId)
        snapshot = player_state(player)

    await sync_action_result(player.id, event)
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
                game.paused_remaining_seconds = max(
                    0.0,
                    (game.round_deadline - datetime.now(timezone.utc)).total_seconds(),
                )
            game.round_deadline = None
            game.status = GameStatus.PAUSED
            game.add_event("The arena has been paused by the admin.", "GAME_PAUSED")
        elif action == "RESUME_GAME":
            if game.status != GameStatus.PAUSED:
                raise HTTPException(status_code=409, detail="Game is not paused")
            remaining = game.paused_remaining_seconds or ROUND_DURATION_SECONDS
            game.round_deadline = datetime.now(timezone.utc) + timedelta(seconds=max(1, remaining))
            for player in game.players.values():
                if player.alive and not player.action_taken:
                    player.action_deadline = game.round_deadline
            game.status = GameStatus.ACTIVE
            game.add_event("The arena has resumed.", "GAME_RESUMED")
        elif action == "END_ROUND":
            if game.status != GameStatus.ACTIVE:
                raise HTTPException(status_code=409, detail="Game is not active")
            _resolve_round_locked()
        elif action == "RESET_GAME":
            reset_requested = True
            game.status = GameStatus.LOBBY
            game.phase = GamePhase.OPENING
            game.round_number = 0
            game.round_deadline = None
            game.paused_remaining_seconds = None
            game.winner_id = None
            game.players.clear()
            game.event_log.clear()
            game.add_event("The arena has been reset. Waiting for players.", "GAME_RESET")
        else:
            raise HTTPException(status_code=400, detail="Unsupported admin action")

        snapshot = admin_state()

    if reset_requested:
        await manager.close_all_players({"type": "RESET"})
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
                    admin_snapshot = admin_state()
            await manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_snapshot})


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

