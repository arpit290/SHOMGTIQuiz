from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "200"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")


class GameStatus(str, Enum):
    LOBBY = "LOBBY"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    GAME_OVER = "GAME_OVER"


@dataclass
class Player:
    id: str
    name: str
    alive: bool = True
    connected: bool = True
    joined_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    session_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "alive": self.alive,
            "connected": self.connected,
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
    players: dict[str, Player] = field(default_factory=dict)
    event_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def alive_count(self) -> int:
        return sum(player.alive for player in self.players.values())

    def add_event(self, message: str, event_type: str = "INFO") -> dict[str, Any]:
        event = {
            "id": secrets.token_hex(6),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "message": message,
        }
        self.event_log.append(event)
        # Keep Phase 1 memory use bounded during a long event/test session.
        if len(self.event_log) > 500:
            del self.event_log[:-500]
        return event


game = GameState()


class ConnectionManager:
    def __init__(self) -> None:
        self.player_connections: dict[str, set[WebSocket]] = {}
        self.admin_connections: set[WebSocket] = set()

    async def connect_player(self, player_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.player_connections.setdefault(player_id, set()).add(websocket)

    def disconnect_player(self, player_id: str, websocket: WebSocket) -> None:
        connections = self.player_connections.get(player_id)
        if not connections:
            return
        connections.discard(websocket)
        if not connections:
            self.player_connections.pop(player_id, None)

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


app = FastAPI(title="Arena RPG API", version="0.1.0")
cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class JoinRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class AdminLoginRequest(BaseModel):
    token: str


class AdminActionRequest(BaseModel):
    action: str


def clean_name(name: str) -> str:
    return " ".join(name.strip().split())


def next_player_id() -> str:
    index = len(game.players) + 1
    while f"P-{index:03d}" in game.players:
        index += 1
    return f"P-{index:03d}"


def player_state(player: Player) -> dict[str, Any]:
    return {
        "gameId": game.game_id,
        "status": game.status.value,
        "player": player.public_dict(),
        "playerCount": len(game.players),
        "aliveCount": game.alive_count,
        "maxPlayers": MAX_PLAYERS,
        "events": game.event_log[-20:],
    }


def admin_state() -> dict[str, Any]:
    return {
        "gameId": game.game_id,
        "status": game.status.value,
        "playerCount": len(game.players),
        "aliveCount": game.alive_count,
        "maxPlayers": MAX_PLAYERS,
        "players": [p.admin_dict() for p in game.players.values()],
        "events": game.event_log[-50:],
    }


def require_admin(x_admin_token: str | None) -> None:
    if not x_admin_token or not secrets.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")


async def sync_everyone() -> None:
    for player_id, player in list(game.players.items()):
        await manager.send_player(player_id, {"type": "GAME_STATE", "data": player_state(player)})
    await manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_state()})


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/game")
async def get_game() -> dict[str, Any]:
    return {
        "gameId": game.game_id,
        "status": game.status.value,
        "playerCount": len(game.players),
        "aliveCount": game.alive_count,
        "maxPlayers": MAX_PLAYERS,
    }


@app.post("/api/join")
async def join_game(request: JoinRequest) -> dict[str, Any]:
    name = clean_name(request.name)
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be blank")
    if len(game.players) >= MAX_PLAYERS:
        raise HTTPException(status_code=409, detail="The game is full")
    if game.status != GameStatus.LOBBY:
        raise HTTPException(status_code=409, detail="Registration is closed")

    duplicate = next((p for p in game.players.values() if p.name.casefold() == name.casefold()), None)
    if duplicate:
        raise HTTPException(status_code=409, detail="That name is already registered")

    player = Player(id=next_player_id(), name=name)
    game.players[player.id] = player
    game.add_event(f"{player.name} joined the arena ({player.id}).", "PLAYER_JOINED")
    await sync_everyone()

    return {
        "player": player.public_dict(),
        "sessionToken": player.session_token,
        "game": {
            "gameId": game.game_id,
            "status": game.status.value,
            "playerCount": len(game.players),
            "maxPlayers": MAX_PLAYERS,
        },
    }


@app.post("/api/admin/login")
async def admin_login(request: AdminLoginRequest) -> dict[str, bool]:
    if not secrets.compare_digest(request.token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    return {"authenticated": True}


@app.get("/api/admin/state")
async def get_admin_state(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(x_admin_token)
    return admin_state()


@app.post("/api/admin/action")
async def admin_action(
    request: AdminActionRequest,
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_admin(x_admin_token)
    action = request.action.upper()
    if action == "START_GAME":
        if game.status != GameStatus.LOBBY:
            raise HTTPException(status_code=409, detail="Game can only be started from the lobby")
        if not game.players:
            raise HTTPException(status_code=409, detail="At least one player is required")
        game.status = GameStatus.ACTIVE
        game.add_event("The arena has begun.", "GAME_STARTED")
    elif action == "PAUSE_GAME":
        if game.status != GameStatus.ACTIVE:
            raise HTTPException(status_code=409, detail="Game is not active")
        game.status = GameStatus.PAUSED
        game.add_event("The arena has been paused.", "GAME_PAUSED")
    elif action == "RESUME_GAME":
        if game.status != GameStatus.PAUSED:
            raise HTTPException(status_code=409, detail="Game is not paused")
        game.status = GameStatus.ACTIVE
        game.add_event("The arena has resumed.", "GAME_RESUMED")
    elif action == "RESET_GAME":
        game.status = GameStatus.LOBBY
        game.players.clear()
        game.event_log.clear()
        game.add_event("The arena has been reset.", "GAME_RESET")
    else:
        raise HTTPException(status_code=400, detail="Unsupported admin action")

    await sync_everyone()
    return admin_state()


@app.websocket("/ws/player/{player_id}")
async def player_ws(websocket: WebSocket, player_id: str, token: str | None = None) -> None:
    player = game.players.get(player_id)
    if not player or not token or not secrets.compare_digest(token, player.session_token):
        await websocket.close(code=1008)
        return

    await manager.connect_player(player_id, websocket)
    player.connected = True
    await manager.send_player(player_id, {"type": "GAME_STATE", "data": player_state(player)})
    await manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_state()})

    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "PING":
                await websocket.send_json({"type": "PONG"})
    except WebSocketDisconnect:
        manager.disconnect_player(player_id, websocket)
        if player_id in game.players:
            player.connected = False
            await manager.broadcast_admin({"type": "ADMIN_STATE", "data": admin_state()})


@app.websocket("/ws/admin")
async def admin_ws(websocket: WebSocket, token: str | None = None) -> None:
    if not token or not secrets.compare_digest(token, ADMIN_TOKEN):
        await websocket.close(code=1008)
        return

    await manager.connect_admin(websocket)
    await websocket.send_json({"type": "ADMIN_STATE", "data": admin_state()})

    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "PING":
                await websocket.send_json({"type": "PONG"})
    except WebSocketDisconnect:
        manager.disconnect_admin(websocket)
