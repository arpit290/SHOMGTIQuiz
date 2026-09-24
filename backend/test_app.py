import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import app


client = TestClient(app.app)


def reset_state() -> None:
    app.game.status = app.GameStatus.LOBBY
    app.game.phase = app.GamePhase.OPENING
    app.game.round_number = 0
    app.game.round_deadline = None
    app.game.paused_remaining_seconds = None
    app.game.winner_id = None
    app.game.players.clear()
    app.game.event_log.clear()


def admin_header():
    return {"X-Admin-Token": app.ADMIN_TOKEN}


def test_health() -> None:
    reset_state()
    assert client.get("/api/health").json() == {"status": "ok"}


def test_zones_are_complete() -> None:
    reset_state()
    response = client.get("/api/zones")
    assert response.status_code == 200
    zones = response.json()
    assert len(zones) == 13
    assert zones[-1]["id"] == "cornucopia"
    assert all(len(zone["connectedZones"]) > 0 for zone in zones)


def test_join_player_gets_phase_2_stats() -> None:
    reset_state()
    response = client.post("/api/join", json={"name": "Arjun"})
    assert response.status_code == 200
    player = response.json()["player"]
    assert player["id"] == "P-001"
    assert player["health"] == 100
    assert player["attack"] == 10
    assert player["speed"] == 10
    assert player["zoneId"] == "zone_1"


def test_duplicate_name_rejected() -> None:
    reset_state()
    client.post("/api/join", json={"name": "Arjun"})
    response = client.post("/api/join", json={"name": " arjun "})
    assert response.status_code == 409


def test_admin_requires_token() -> None:
    reset_state()
    response = client.get("/api/admin/state")
    assert response.status_code == 401


def test_start_game_assigns_zones_and_round_timer() -> None:
    reset_state()
    client.post("/api/join", json={"name": "Arjun"})
    client.post("/api/join", json={"name": "Rohan"})
    response = client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    assert response.status_code == 200
    state = response.json()
    assert state["status"] == "ACTIVE"
    assert state["phase"] == "OPENING"
    assert state["round"] == 1
    assert state["roundDeadline"] is not None
    assert {player["zoneId"] for player in state["players"]} == {"zone_1", "zone_2"}
    reset_state()


def test_player_can_move_to_adjacent_zone() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun"}).json()
    client.post("/api/join", json={"name": "Rohan"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    response = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "MOVE", "targetZoneId": "zone_2"},
    )
    assert response.status_code == 200
    assert response.json()["player"]["zoneId"] == "zone_2"
    assert response.json()["player"]["actionTaken"] is True
    reset_state()


def test_player_cannot_move_to_non_adjacent_zone() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun"}).json()
    client.post("/api/join", json={"name": "Rohan"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    response = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "MOVE", "targetZoneId": "zone_6"},
    )
    assert response.status_code == 409
    reset_state()


def test_player_can_rest_once_per_round() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun"}).json()
    client.post("/api/join", json={"name": "Rohan"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    player = app.game.players[join["player"]["id"]]
    player.health = 80

    response = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "REST"},
    )
    assert response.status_code == 200
    assert response.json()["player"]["health"] == 85

    second = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "REST"},
    )
    assert second.status_code == 409
    reset_state()


def test_end_round_eliminates_players_who_missed_action() -> None:
    reset_state()
    first = client.post("/api/join", json={"name": "Arjun"}).json()
    second = client.post("/api/join", json={"name": "Rohan"}).json()
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})

    response = client.post("/api/admin/action", headers=admin_header(), json={"action": "END_ROUND"})
    assert response.status_code == 200
    state = response.json()
    # Both skipped their action; the arena therefore reaches GAME_OVER with no winner.
    assert state["status"] == "GAME_OVER"
    assert state["aliveCount"] == 0
    assert app.game.players[first["player"]["id"]].alive is False
    assert app.game.players[second["player"]["id"]].alive is False
    reset_state()


def test_pause_and_resume_preserve_round() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun"}).json()
    client.post("/api/join", json={"name": "Rohan"})
    started = client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"}).json()
    assert started["round"] == 1

    paused = client.post("/api/admin/action", headers=admin_header(), json={"action": "PAUSE_GAME"}).json()
    assert paused["status"] == "PAUSED"
    remaining = app.game.paused_remaining_seconds
    assert remaining is not None

    resumed = client.post("/api/admin/action", headers=admin_header(), json={"action": "RESUME_GAME"}).json()
    assert resumed["status"] == "ACTIVE"
    assert resumed["round"] == 1
    assert resumed["roundDeadline"] is not None
    assert app.game.players[join["player"]["id"]].action_deadline is not None
    reset_state()


def test_two_hundred_players_can_register() -> None:
    reset_state()
    for index in range(200):
        response = client.post("/api/join", json={"name": f"Player {index + 1}"})
        assert response.status_code == 200
    assert len(app.game.players) == 200
    response = client.post("/api/join", json={"name": "Overflow"})
    assert response.status_code == 409
    reset_state()


async def _round_resolution_smoke_test() -> None:
    reset_state()
    client.post("/api/join", json={"name": "Arjun"})
    client.post("/api/join", json={"name": "Rohan"})
    client.post("/api/join", json={"name": "Priya"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    # Two players act; one misses the timer, so the next round starts.
    admin_players = client.get("/api/admin/state", headers=admin_header()).json()["players"]
    join = admin_players[0]
    join_two = admin_players[1]
    player = app.game.players[join["id"]]
    player_two = app.game.players[join_two["id"]]
    player.action_taken = True
    player_two.action_taken = True
    app.game.round_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    # Directly exercise the async resolver path used by the background timer.
    async with app.state_lock:
        app._resolve_round_locked()
    assert app.game.round_number == 2
    assert app.game.status == app.GameStatus.ACTIVE
    reset_state()


def test_background_round_resolver_logic() -> None:
    asyncio.run(_round_resolution_smoke_test())
