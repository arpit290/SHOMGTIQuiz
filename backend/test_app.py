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
    app.game.hazard_zones.clear()
    for items in app.game.zone_items.values():
        items.clear()


def admin_header():
    return {"X-Admin-Token": app.ADMIN_TOKEN}


def start_small_game(names: list[str] | None = None):
    names = names or ["Arjun", "Rohan"]
    joins = [client.post("/api/join", json={"name": name}).json() for name in names]
    started = client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    assert started.status_code == 200
    return joins, started.json()


def test_health() -> None:
    reset_state()
    assert client.get("/api/health").json() == {"status": "ok"}


def test_zones_and_phase3_zone_metadata_are_complete() -> None:
    reset_state()
    response = client.get("/api/zones")
    assert response.status_code == 200
    zones = response.json()
    assert len(zones) == 13
    assert zones[-1]["id"] == "cornucopia"
    assert all("lootCount" in zone for zone in zones)
    assert all("hazard" in zone for zone in zones)


def test_join_player_gets_stats_and_empty_inventory() -> None:
    reset_state()
    response = client.post("/api/join", json={"name": "Arjun"})
    assert response.status_code == 200
    player = response.json()["player"]
    assert player["id"] == "P-001"
    assert player["health"] == 100
    assert player["attack"] == 10
    assert player["speed"] == 10
    assert player["inventory"] == []


def test_duplicate_name_rejected() -> None:
    reset_state()
    client.post("/api/join", json={"name": "Arjun"})
    response = client.post("/api/join", json={"name": " arjun "})
    assert response.status_code == 409


def test_admin_requires_token() -> None:
    reset_state()
    response = client.get("/api/admin/state")
    assert response.status_code == 401


def test_start_game_assigns_zones_round_timer_and_cornucopia_cache() -> None:
    reset_state()
    joins, state = start_small_game(["Arjun", "Rohan", "Priya"])
    assert state["status"] == "ACTIVE"
    assert state["phase"] == "OPENING"
    assert state["round"] == 1
    assert state["roundDeadline"] is not None
    assert [player["zoneId"] for player in state["players"]] == ["zone_1", "zone_2", "zone_3"]
    assert sum(len(items) for items in app.game.zone_items.values()) == 8
    assert app.game.zone_items["cornucopia"]
    reset_state()


def test_player_can_move_and_only_moves_once_per_round() -> None:
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

    second = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "MOVE", "targetZoneId": "zone_3"},
    )
    assert second.status_code == 409
    reset_state()


def test_non_adjacent_move_rejected() -> None:
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


def test_rest_heals_and_search_can_find_existing_supply() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun"}).json()
    client.post("/api/join", json={"name": "Rohan"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})

    player = app.game.players[join["player"]["id"]]
    player.health = 80
    app.game.zone_items[player.zone_id].append(app.make_item("MEDKIT"))

    rest = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "REST"},
    )
    assert rest.status_code == 200
    assert rest.json()["player"]["health"] == 85

    # Move the player into a fresh round without relying on elapsed wall-clock time.
    player.action_taken = False
    player.current_action = None
    player.action_deadline = app.game.round_deadline
    search = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "SEARCH"},
    )
    assert search.status_code == 200
    assert len(search.json()["player"]["inventory"]) == 1
    assert search.json()["player"]["inventory"][0]["type"] == "MEDKIT"
    reset_state()


def test_attack_requires_same_zone_target_and_can_eliminate() -> None:
    reset_state()
    attacker_join, _ = start_small_game(["Attacker", "Target"])
    attacker = app.game.players[attacker_join[0]["player"]["id"]]
    target = app.game.players[attacker_join[1]["player"]["id"]]

    target.zone_id = attacker.zone_id
    attacker.attack = 200
    app.rng = __import__("random").Random(7)

    response = client.post(
        "/api/action",
        headers={"X-Player-Token": attacker.session_token},
        json={
            "playerId": attacker.id,
            "action": "ATTACK",
            "targetPlayerId": target.id,
        },
    )
    assert response.status_code == 200
    assert target.alive is False
    assert target.health == 0
    assert attacker.kills == 1
    assert "eliminated" in target.last_result.lower()
    assert app.game.status == app.GameStatus.GAME_OVER
    assert app.game.winner_id == attacker.id
    reset_state()


def test_attack_rejected_when_target_is_not_in_same_zone() -> None:
    reset_state()
    joins, _ = start_small_game(["Attacker", "Target"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    target = app.game.players[joins[1]["player"]["id"]]
    response = client.post(
        "/api/action",
        headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "ATTACK", "targetPlayerId": target.id},
    )
    assert response.status_code == 409
    reset_state()


def test_use_medkit_and_speed_boost_consumes_items_and_changes_stats() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun"}).json()
    client.post("/api/join", json={"name": "Rohan"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    player = app.game.players[join["player"]["id"]]
    player.health = 60
    player.inventory.append(app.make_item("MEDKIT"))

    medkit_id = player.inventory[0].id
    response = client.post(
        "/api/action",
        headers={"X-Player-Token": player.session_token},
        json={"playerId": player.id, "action": "USE_ITEM", "itemId": medkit_id},
    )
    assert response.status_code == 200
    assert response.json()["player"]["health"] == 90
    assert response.json()["player"]["inventory"] == []

    player.action_taken = False
    player.current_action = None
    player.action_deadline = app.game.round_deadline
    player.inventory.append(app.make_item("SPEED_BOOST"))
    boost_id = player.inventory[0].id
    boost = client.post(
        "/api/action",
        headers={"X-Player-Token": player.session_token},
        json={"playerId": player.id, "action": "USE_ITEM", "itemId": boost_id},
    )
    assert boost.status_code == 200
    assert boost.json()["player"]["speed"] == 13
    assert boost.json()["player"]["inventory"] == []
    reset_state()


def test_armor_reduces_next_attack() -> None:
    reset_state()
    joins, _ = start_small_game(["Attacker", "Target"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    target = app.game.players[joins[1]["player"]["id"]]
    target.zone_id = attacker.zone_id
    target.health = 100
    target.inventory.append(app.make_item("ARMOR"))
    app.rng = __import__("random").Random(999)

    armor_id = target.inventory[0].id
    use = client.post(
        "/api/action",
        headers={"X-Player-Token": target.session_token},
        json={"playerId": target.id, "action": "USE_ITEM", "itemId": armor_id},
    )
    assert use.status_code == 200
    assert target.status_effect == "ARMORED"

    # Reset only action state so the attacker can act in the same test round.
    attacker.action_taken = False
    attacker.current_action = None
    attacker.action_deadline = app.game.round_deadline
    attacker.attack = 20

    hit = client.post(
        "/api/action",
        headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "ATTACK", "targetPlayerId": target.id},
    )
    assert hit.status_code == 200
    assert 73 <= target.health < 100  # armor should reduce the normal hit and then be consumed
    assert target.status_effect != "ARMORED"
    reset_state()


def test_manual_supply_drop_and_hazard_controls() -> None:
    reset_state()
    start_small_game(["Arjun", "Rohan", "Priya"])
    supply = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "SPAWN_SUPPLY_DROP", "targetZoneId": "zone_5"},
    )
    assert supply.status_code == 200
    assert len(app.game.zone_items["zone_5"]) >= 1

    hazard = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "TRIGGER_HAZARD", "targetZoneId": "zone_5"},
    )
    assert hazard.status_code == 200
    assert app.game.hazard_zones == {"zone_5"}
    reset_state()


def test_end_round_eliminates_players_who_missed_action() -> None:
    reset_state()
    first = client.post("/api/join", json={"name": "Arjun"}).json()
    second = client.post("/api/join", json={"name": "Rohan"}).json()
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})

    response = client.post("/api/admin/action", headers=admin_header(), json={"action": "END_ROUND"})
    assert response.status_code == 200
    state = response.json()
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
    assert app.game.paused_remaining_seconds is not None

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
    admin_players = client.get("/api/admin/state", headers=admin_header()).json()["players"]
    player = app.game.players[admin_players[0]["id"]]
    player_two = app.game.players[admin_players[1]["id"]]
    player.action_taken = True
    player_two.action_taken = True
    app.game.round_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with app.state_lock:
        app._resolve_round_locked()
    assert app.game.round_number == 2
    assert app.game.status == app.GameStatus.ACTIVE
    reset_state()


def test_background_round_resolver_logic() -> None:
    asyncio.run(_round_resolution_smoke_test())


def test_admin_operator_tools_and_announcement() -> None:
    reset_state()
    joins, _ = start_small_game(["Arjun", "Rohan", "Priya"])
    target_id = joins[0]["player"]["id"]

    move = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "MOVE_PLAYER", "targetPlayerId": target_id, "targetZoneId": "cornucopia"},
    )
    assert move.status_code == 200
    assert app.game.players[target_id].zone_id == "cornucopia"

    give = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "GIVE_ITEM", "targetPlayerId": target_id, "itemType": "WEAPON"},
    )
    assert give.status_code == 200
    assert app.game.players[target_id].inventory[-1].type == "WEAPON"

    stats = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "SET_STATS", "targetPlayerId": target_id, "value": 77, "attack": 25, "speed": 19},
    )
    assert stats.status_code == 200
    assert app.game.players[target_id].health == 77
    assert app.game.players[target_id].attack == 25
    assert app.game.players[target_id].speed == 19

    announcement = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "BROADCAST_ANNOUNCEMENT", "message": "The arena is watching."},
    )
    assert announcement.status_code == 200
    assert any("The arena is watching." in event["message"] for event in app.game.event_log)
    reset_state()


def test_admin_can_eliminate_and_restore_player() -> None:
    reset_state()
    joins, _ = start_small_game(["Arjun", "Rohan", "Priya"])
    target_id = joins[0]["player"]["id"]

    eliminated = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "ELIMINATE_PLAYER", "targetPlayerId": target_id, "reason": "Event correction"},
    )
    assert eliminated.status_code == 200
    assert app.game.players[target_id].alive is False

    restored = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "RESTORE_PLAYER", "targetPlayerId": target_id, "value": 55},
    )
    assert restored.status_code == 200
    assert app.game.players[target_id].alive is True
    assert app.game.players[target_id].health == 55
    reset_state()


def test_admin_state_reports_online_acted_and_waiting_counts() -> None:
    reset_state()
    joins, _ = start_small_game(["Arjun", "Rohan", "Priya"])
    first = app.game.players[joins[0]["player"]["id"]]
    first.action_taken = True
    state = client.get("/api/admin/state", headers=admin_header()).json()
    assert state["aliveCount"] == 3
    assert state["actedCount"] == 1
    assert state["waitingCount"] == 2
    assert state["onlineCount"] == 0
    reset_state()


def test_checkpoint_recovers_active_game_as_paused(tmp_path) -> None:
    reset_state()
    start_small_game(["Arjun", "Rohan"])
    for player in app.game.players.values():
        player.connected = False
    app.game.round_deadline = datetime.now(timezone.utc) + timedelta(seconds=8)

    original_state_file = app.STATE_FILE
    app.STATE_FILE = tmp_path / "arena_state.json"
    try:
        app._write_checkpoint_locked()
        app.game.players.clear()
        app.game.round_deadline = None
        app.game.status = app.GameStatus.LOBBY
        app.game.phase = app.GamePhase.OPENING
        app._restore_from_checkpoint()
        assert app.game.status == app.GameStatus.PAUSED
        assert len(app.game.players) == 2
        assert app.game.paused_remaining_seconds is not None
        assert all(player.action_deadline is None for player in app.game.players.values())
        assert any(event["type"] == "GAME_RECOVERED" for event in app.game.event_log)
    finally:
        app.STATE_FILE = original_state_file
        reset_state()
