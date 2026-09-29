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
    app.game.battles.clear()
    app.game.event_log.clear()
    app.game.hazard_zones.clear()
    app.game.hazard_counters = {zone_id: 0 for zone_id in app.OUTER_ZONE_IDS}
    for items in app.game.zone_items.values():
        items.clear()


def admin_header():
    return {"X-Admin-Token": app.ADMIN_TOKEN}


def start_small_game(names: list[str] | None = None):
    names = names or ["Arjun", "Rohan"]
    joins = [client.post("/api/join", json={"name": name, "gender": "M" if index % 2 == 0 else "F"}).json() for index, name in enumerate(names)]
    started = client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    assert started.status_code == 200
    normalize_stats()
    return joins, started.json()


def normalize_stats() -> None:
    """Make combat maths deterministic: everyone gets baseline 10/10/10 stats."""
    for player in app.game.players.values():
        player.attack = player.defense = player.agility = 10.0
        player.base_attack = player.base_defense = player.base_agility = 10.0


def test_health() -> None:
    reset_state()
    assert client.get("/api/health").json() == {"status": "ok"}


def test_zones_and_phase3_zone_metadata_are_complete() -> None:
    reset_state()
    response = client.get("/api/zones")
    assert response.status_code == 200
    zones = response.json()
    assert len(zones) == 7
    assert zones[-1]["id"] == "cornucopia"
    assert [zone["hazardName"] for zone in zones[:6]] == [
        "Lightning Strikes",
        "Tracker Jacker Wasps",
        "Blood Rain",
        "Poison Fog",
        "Tidal Wave",
        "Monkey Mutations",
    ]
    assert all("lootCount" in zone for zone in zones)
    assert all("hazard" in zone for zone in zones)


def test_join_player_gets_stats_and_empty_inventory() -> None:
    reset_state()
    response = client.post("/api/join", json={"name": "Arjun", "gender": "M"})
    assert response.status_code == 200
    player = response.json()["player"]
    assert player["id"] == "P-001"
    assert player["health"] == 100
    assert player["inventory"] == []
    # Without an explicit choice the server hands out one HIGH / one MID / one LOW.
    stats = sorted([player["attack"], player["defense"], player["agility"]])
    assert stats[0] < 7 and 9 < stats[1] < 11 and stats[2] > 13
    reset_state()


def test_join_with_chosen_stats_applies_high_mid_low_with_small_jitter() -> None:
    reset_state()
    response = client.post(
        "/api/join",
        json={"name": "Arjun", "gender": "M", "stats": {"attack": "HIGH", "defense": "MID", "agility": "LOW"}},
    )
    assert response.status_code == 200
    player = response.json()["player"]
    assert 14 * 0.95 <= player["attack"] <= 14 * 1.05
    assert 10 * 0.95 <= player["defense"] <= 10 * 1.05
    assert 6 * 0.95 <= player["agility"] <= 6 * 1.05
    assert "speed" not in player
    reset_state()


def test_join_rejects_stat_choice_that_is_not_one_high_mid_low() -> None:
    reset_state()
    response = client.post(
        "/api/join",
        json={"name": "Arjun", "gender": "M", "stats": {"attack": "HIGH", "defense": "HIGH", "agility": "LOW"}},
    )
    assert response.status_code == 400
    assert app.game.players == {}
    reset_state()


def test_player_feed_only_contains_events_involving_that_player() -> None:
    reset_state()
    joins, _ = start_small_game(["Alice", "Bob", "Cara"])
    alice, bob, cara = (app.game.players[j["player"]["id"]] for j in joins)
    bob.zone_id = alice.zone_id = "zone_1"
    cara.zone_id = "zone_6"

    assert client.post("/api/action", headers={"X-Player-Token": cara.session_token},
                       json={"playerId": cara.id, "action": "REST"}).status_code == 200
    assert client.post("/api/action", headers={"X-Player-Token": alice.session_token},
                       json={"playerId": alice.id, "action": "ATTACK", "targetPlayerId": bob.id}).status_code == 200
    assert client.post("/api/action", headers={"X-Player-Token": alice.session_token},
                       json={"playerId": alice.id, "action": "BATTLE_ATTACK"}).status_code == 200

    def feed(player):
        return [e["message"] for e in app.player_state(player)["events"]]

    assert any("engaged" in m for m in feed(bob))
    # Bob must not see Alice's simultaneous move before he has chosen his own.
    assert not any("chose" in m for m in feed(bob))
    assert any("chose ATTACK" in m for m in feed(alice))
    assert not any("Cara" in m for m in feed(alice) + feed(bob))
    assert not any("Alice" in m or "Bob" in m for m in feed(cara))
    assert any("begun" in m for m in feed(cara))  # game-start is arena-wide
    reset_state()


def test_hazard_damage_preview_is_reduced_by_agility() -> None:
    reset_state()
    joins, _ = start_small_game(["Quick", "Slow"])
    quick = app.game.players[joins[0]["player"]["id"]]
    slow = app.game.players[joins[1]["player"]["id"]]
    quick.zone_id = slow.zone_id = "zone_5"
    app.game.hazard_zones.add("zone_5")
    quick.agility, slow.agility = 14.0, 6.0
    assert app.hazard_damage_for(quick) < app.hazard_damage_for(slow)
    reset_state()


def test_duplicate_name_rejected() -> None:
    reset_state()
    client.post("/api/join", json={"name": "Arjun", "gender": "M"})
    response = client.post("/api/join", json={"name": " arjun ", "gender": "M"})
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
    join = client.post("/api/join", json={"name": "Arjun", "gender": "M"}).json()
    client.post("/api/join", json={"name": "Rohan", "gender": "M"})
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
    join = client.post("/api/join", json={"name": "Arjun", "gender": "M"}).json()
    client.post("/api/join", json={"name": "Rohan", "gender": "M"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    response = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "MOVE", "targetZoneId": "zone_4"},
    )
    assert response.status_code == 409
    reset_state()


def test_rest_and_cornucopia_grab_replace_search_and_hide() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun", "gender": "M"}).json()
    client.post("/api/join", json={"name": "Rohan", "gender": "M"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})

    player = app.game.players[join["player"]["id"]]
    player.health = 80

    rest = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "REST"},
    )
    assert rest.status_code == 200
    assert rest.json()["player"]["health"] == 85

    player.action_taken = False
    player.current_action = None
    player.action_deadline = app.game.round_deadline
    search = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "SEARCH"},
    )
    assert search.status_code == 409

    player.action_taken = False
    player.current_action = None
    player.action_deadline = app.game.round_deadline
    hide = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": join["player"]["id"], "action": "HIDE"},
    )
    assert hide.status_code == 409

    player.zone_id = "cornucopia"
    app.game.zone_items["cornucopia"].clear()
    app.game.zone_items["cornucopia"].append(app.make_item("MEDKIT"))
    grab = client.post(
        "/api/action",
        headers={"X-Player-Token": join["sessionToken"]},
        json={"playerId": player.id, "action": "GRAB_ITEM"},
    )
    assert grab.status_code == 200
    assert grab.json()["player"]["inventory"][0]["type"] == "MEDKIT"
    reset_state()


def test_inventory_holds_one_item_and_grab_swaps_it() -> None:
    reset_state()
    joins, _ = start_small_game(["Arjun", "Rohan"])
    player = app.game.players[joins[0]["player"]["id"]]
    player.zone_id = "cornucopia"
    pile = app.game.zone_items["cornucopia"]
    pile.clear()
    pile.extend([app.make_item("SHINY_SWORD"), app.make_item("SHADOW_CLOAK")])
    headers = {"X-Player-Token": player.session_token}

    # The offered item is visible before committing.
    assert app.player_state(player)["offeredItem"]["type"] == "SHINY_SWORD"
    first = client.post("/api/action", headers=headers, json={"playerId": player.id, "action": "GRAB_ITEM"})
    assert first.status_code == 200
    assert [i.type for i in player.inventory] == ["SHINY_SWORD"]

    # Bag is full: GRAB_ITEM is now a swap, and the old item returns to the pile.
    player.action_taken = False
    player.current_action = None
    assert app.player_state(player)["offeredItem"]["type"] == "SHADOW_CLOAK"
    swap = client.post("/api/action", headers=headers, json={"playerId": player.id, "action": "GRAB_ITEM"})
    assert swap.status_code == 200
    assert [i.type for i in player.inventory] == ["SHADOW_CLOAK"]
    assert [i.type for i in pile] == ["SHINY_SWORD"]
    reset_state()


def test_attack_engages_both_players_and_battle_actions_can_eliminate() -> None:
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
    assert attacker.battle_id is not None
    assert target.battle_id == attacker.battle_id
    assert attacker.health == 100
    assert target.health == 100
    assert "BATTLE_ATTACK" in response.json()["availableActions"]

    defender_move = client.post(
        "/api/action",
        headers={"X-Player-Token": target.session_token},
        json={"playerId": target.id, "action": "BATTLE_ATTACK"},
    )
    assert defender_move.status_code == 200
    attacker_move = client.post(
        "/api/action",
        headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "BATTLE_ATTACK"},
    )
    assert attacker_move.status_code == 200
    assert target.alive is False
    assert target.health == 0
    assert attacker.kills == 1
    assert "eliminated" in target.last_result.lower()
    assert app.game.status == app.GameStatus.GAME_OVER
    assert app.game.winner_id == attacker.id
    reset_state()


def test_battle_blocks_normal_actions_and_defend_reduces_damage() -> None:
    reset_state()
    joins, _ = start_small_game(["Attacker", "Target"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    target = app.game.players[joins[1]["player"]["id"]]
    target.zone_id = attacker.zone_id
    attacker.attack = 20
    app.rng = __import__("random").Random(999)

    engaged = client.post(
        "/api/action",
        headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "ATTACK", "targetPlayerId": target.id},
    )
    assert engaged.status_code == 200

    blocked = client.post(
        "/api/action",
        headers={"X-Player-Token": target.session_token},
        json={"playerId": target.id, "action": "REST"},
    )
    assert blocked.status_code == 409

    target.inventory.append(app.make_item("TITAN_SHIELD"))
    target_choice = client.post(
        "/api/action",
        headers={"X-Player-Token": target.session_token},
        json={"playerId": target.id, "action": "BATTLE_DEFEND"},
    )
    assert target_choice.status_code == 200
    attacker_choice = client.post(
        "/api/action",
        headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "BATTLE_ATTACK"},
    )
    assert attacker_choice.status_code == 200
    assert target.health < 100
    assert target.health >= 80
    assert target.battle_id is not None
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


def test_use_medkit_and_golden_apple_consumes_items_and_changes_stats() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun", "gender": "M"}).json()
    client.post("/api/join", json={"name": "Rohan", "gender": "F"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    player = app.game.players[join["player"]["id"]]
    player.health = 50
    player.inventory.append(app.make_item("MEDKIT"))

    medkit_id = player.inventory[0].id
    response = client.post("/api/action", headers={"X-Player-Token": player.session_token}, json={"playerId": player.id, "action": "USE_ITEM", "itemId": medkit_id})
    assert response.status_code == 200
    assert response.json()["player"]["health"] == 100
    assert response.json()["player"]["inventory"] == []

    player.action_taken = False
    player.round_action_complete = False
    player.current_action = None
    player.action_deadline = app.game.round_deadline
    player.inventory.append(app.make_item("GOLDEN_APPLE"))
    apple_id = player.inventory[0].id
    before = player.base_attack
    boost = client.post("/api/action", headers={"X-Player-Token": player.session_token}, json={"playerId": player.id, "action": "USE_ITEM", "itemId": apple_id})
    assert boost.status_code == 200
    assert player.golden_apple_turns_remaining == 5
    assert player.inventory == []
    assert app.effective_stat(player, "attack") == before * 1.5
    reset_state()

def test_consumable_can_be_used_as_a_battle_move() -> None:
    reset_state()
    joins, _ = start_small_game(["Attacker", "Target"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    target = app.game.players[joins[1]["player"]["id"]]
    target.zone_id = attacker.zone_id
    target.health = 50
    target.inventory = [app.make_item("MEDKIT")]

    engaged = client.post(
        "/api/action", headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "ATTACK", "targetPlayerId": target.id},
    )
    assert engaged.status_code == 200
    target_state = client.get(f"/api/player/{target.id}/state", headers={"X-Player-Token": target.session_token})
    assert target_state.status_code == 200
    assert "BATTLE_USE_ITEM" in target_state.json()["availableActions"]
    target_move = client.post(
        "/api/action", headers={"X-Player-Token": target.session_token},
        json={"playerId": target.id, "action": "BATTLE_USE_ITEM"},
    )
    assert target_move.status_code == 200
    attacker_move = client.post(
        "/api/action", headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "BATTLE_DEFEND"},
    )
    assert attacker_move.status_code == 200
    assert target.health == 100
    assert target.inventory == []
    reset_state()

def test_titan_shield_reduces_damage_and_breaks_after_seven_hits() -> None:
    reset_state()
    joins, _ = start_small_game(["Attacker", "Target", "Extra"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    target = app.game.players[joins[1]["player"]["id"]]
    attacker.attack = attacker.base_attack = 10
    target.defense = target.base_defense = 10
    shield = app.make_item("TITAN_SHIELD")
    target.inventory.append(shield)
    app.rng = __import__("random").Random(123)

    damages = []
    for _ in range(7):
        damage, _, _ = app._attack_damage_locked(attacker, target)
        assert damage > 0
        damages.append(damage)
        if target.inventory:
            assert target.inventory[0].type == "TITAN_SHIELD"
    assert len(damages) == 7
    assert target.inventory == []
    reset_state()

def test_manual_supply_drop_and_hazard_controls() -> None:
    reset_state()
    start_small_game(["Arjun", "Rohan", "Priya"])
    supply = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "SPAWN_SUPPLY_DROP", "targetZoneId": "cornucopia"},
    )
    assert supply.status_code == 200
    assert len(app.game.zone_items["cornucopia"]) >= 1

    invalid_supply = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "SPAWN_SUPPLY_DROP", "targetZoneId": "zone_5"},
    )
    assert invalid_supply.status_code == 400

    hazard = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "TRIGGER_HAZARD", "targetZoneId": "zone_5"},
    )
    assert hazard.status_code == 200
    assert app.game.hazard_zones == {"zone_5"}
    reset_state()


def test_gender_is_required_at_registration() -> None:
    reset_state()
    response = client.post("/api/join", json={"name": "NoGender"})
    assert response.status_code == 422
    reset_state()


def test_each_gender_has_twelve_district_slots() -> None:
    reset_state()
    for index in range(12):
        assert client.post("/api/join", json={"name": f"M{index}", "gender": "M"}).status_code == 200
    blocked = client.post("/api/join", json={"name": "M13", "gender": "M"})
    assert blocked.status_code == 409
    assert client.post("/api/join", json={"name": "F1", "gender": "F"}).status_code == 200
    assert app.game.players["P-013"].district == 1
    reset_state()



def test_item_pool_contains_only_the_new_twelve_items() -> None:
    expected = {
        "MEDKIT", "SHINY_SWORD", "GOLDEN_APPLE", "SHADOW_CLOAK",
        "TITAN_SHIELD", "HUNTERS_FEATHER", "PHOENIX_ASHES", "HEART_OF_IRON",
        "SERPENTINE_DAGGER", "BERSERKER_GAUNTLETS", "ADVENTURERS_BOOTS", "CROWN_OF_BLOOD",
    }
    assert set(app.ITEM_DEFINITIONS) == expected
    assert not ({"FOOD", "WEAPON", "ARMOR", "SCOUT", "SPEED_BOOST"} & set(app.ITEM_DEFINITIONS))


def test_registration_caps_at_24_and_pairs_gender_by_district() -> None:
    reset_state()
    for index in range(12):
        assert client.post("/api/join", json={"name": f"Male {index}", "gender": "M"}).status_code == 200
        assert client.post("/api/join", json={"name": f"Female {index}", "gender": "F"}).status_code == 200
    assert len(app.game.players) == 24
    males = sorted(p.district for p in app.game.players.values() if p.gender == "M")
    females = sorted(p.district for p in app.game.players.values() if p.gender == "F")
    assert males == list(range(1, 13))
    assert females == list(range(1, 13))
    full = client.post("/api/join", json={"name": "Extra", "gender": "M"})
    assert full.status_code == 409
    reset_state()


def test_new_item_stat_effects_and_derived_hp() -> None:
    reset_state()
    joins, _ = start_small_game(["Tribute", "Other"])
    player = app.game.players[joins[0]["player"]["id"]]
    player.attack = player.base_attack = 10
    player.defense = player.base_defense = 10
    player.agility = player.base_agility = 10

    player.inventory = [app.make_item("SHINY_SWORD")]
    assert app.effective_stat(player, "attack") == 14
    player.inventory = [app.make_item("SERPENTINE_DAGGER")]
    assert app.effective_stat(player, "attack") == 7.5
    player.inventory = [app.make_item("BERSERKER_GAUNTLETS")]
    player.health = 40
    assert app.effective_stat(player, "attack") == 13
    player.health = 10
    assert app.effective_stat(player, "attack") == 17.5
    player.health = 100
    player.inventory = [app.make_item("HUNTERS_FEATHER")]
    assert app.effective_stat(player, "agility") == 15
    player.inventory = [app.make_item("HEART_OF_IRON")]
    app.refresh_item_derived_state_locked(player)
    assert player.max_health == 160
    assert player.health == 100
    assert app.effective_stat(player, "agility") == 5
    reset_state()


def test_serpentine_dagger_applies_the_poisoned_status() -> None:
    reset_state()
    joins, _ = start_small_game(["Hunter", "Target"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    defender = app.game.players[joins[1]["player"]["id"]]
    attacker.inventory = [app.make_item("SERPENTINE_DAGGER")]
    event = app._apply_serpentine_poison_on_hit_locked(attacker, defender)
    assert event is not None
    assert defender.status_effect == "POISONED"
    assert defender.poison_stage == 1
    assert defender.poison_turns_remaining == 2
    reset_state()


def test_crown_of_blood_kill_stack_and_cornucopia_only_movement() -> None:
    reset_state()
    joins, _ = start_small_game(["Killer", "Victim"])
    killer = app.game.players[joins[0]["player"]["id"]]
    victim = app.game.players[joins[1]["player"]["id"]]
    killer.zone_id = "cornucopia"
    victim.zone_id = "cornucopia"
    killer.inventory = [app.make_item("CROWN_OF_BLOOD")]

    event = app._eliminate_player_locked(victim, "Victim was eliminated.", killer=killer)
    assert event["type"] == "PLAYER_ELIMINATED"
    assert killer.kills == 1
    assert killer.crown_blood_stacks == 1
    assert app.effective_stat(killer, "attack") == killer.base_attack * 1.30

    # The Crown holder cannot move out of the Cornucopia.
    valid, message = app._validate_player_action_locked(killer, "MOVE", "zone_1", None, None)
    assert not valid
    assert "cannot leave" in message.lower()
    reset_state()


def test_adventurers_boots_extend_travel_and_reduce_environment_damage() -> None:
    reset_state()
    joins, _ = start_small_game(["Boots", "Other"])
    player = app.game.players[joins[0]["player"]["id"]]
    player.zone_id = "zone_1"
    player.inventory = [app.make_item("ADVENTURERS_BOOTS")]
    assert "zone_5" in app.available_move_zone_ids(player)

    player.max_health = 100
    player.agility = player.base_agility = 10
    player.inventory = []
    without = app.percent_damage_for(player, 0.50, agility_factor=False)
    player.inventory = [app.make_item("ADVENTURERS_BOOTS")]
    with_boots = app.percent_damage_for(player, 0.50, agility_factor=False)
    assert with_boots == int(round(without * 0.60))
    reset_state()


def test_phoenix_ashes_revives_and_is_consumed() -> None:
    reset_state()
    joins, _ = start_small_game(["Phoenix", "Other"])
    player = app.game.players[joins[0]["player"]["id"]]
    player.health = 1
    player.inventory = [app.make_item("PHOENIX_ASHES")]
    event = app._eliminate_player_locked(player, "Phoenix fell.")
    assert event["type"] == "PLAYER_REVIVED"
    assert player.alive is True
    assert player.health == 35
    assert player.inventory == []
    reset_state()

def test_scout_is_removed_from_player_actions_and_rejected() -> None:
    reset_state()
    join = client.post("/api/join", json={"name": "Arjun", "gender": "M"}).json()
    client.post("/api/join", json={"name": "Rohan", "gender": "M"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    player = app.game.players[join["player"]["id"]]
    assert "SCOUT" not in app.available_actions(player)
    response = client.post(
        "/api/action",
        headers={"X-Player-Token": player.session_token},
        json={"playerId": player.id, "action": "SCOUT"},
    )
    assert response.status_code == 409
    reset_state()


def test_mid_stats_take_about_four_to_five_hits_to_eliminate() -> None:
    reset_state()
    joins, _ = start_small_game(["Attacker", "Target"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    defender = app.game.players[joins[1]["player"]["id"]]
    normalize_stats()
    app.rng = __import__("random").Random(12345)

    hits = 0
    while defender.health > 0 and hits < 8:
        damage, _, _ = app._attack_damage_locked(attacker, defender)
        defender.health = max(0, defender.health - damage)
        hits += 1
    assert hits in {4, 5}
    reset_state()


def test_all_players_acting_ends_round_automatically() -> None:
    reset_state()
    joins, _ = start_small_game(["Arjun", "Rohan"])
    first = app.game.players[joins[0]["player"]["id"]]
    second = app.game.players[joins[1]["player"]["id"]]

    first_move = client.post("/api/action", headers={"X-Player-Token": first.session_token}, json={"playerId": first.id, "action": "WAIT"})
    assert first_move.status_code == 200
    assert app.game.round_number == 1

    second_move = client.post("/api/action", headers={"X-Player-Token": second.session_token}, json={"playerId": second.id, "action": "WAIT"})
    assert second_move.status_code == 200
    assert app.game.round_number == 2
    assert app.game.status == app.GameStatus.ACTIVE
    assert not first.round_action_complete
    assert not second.round_action_complete
    reset_state()


def test_battle_actions_end_round_automatically_but_battle_carries_over() -> None:
    reset_state()
    joins, _ = start_small_game(["Arjun", "Rohan"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    target = app.game.players[joins[1]["player"]["id"]]
    target.zone_id = attacker.zone_id
    normalize_stats()
    app.rng = __import__("random").Random(999)

    engaged = client.post(
        "/api/action", headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "ATTACK", "targetPlayerId": target.id},
    )
    assert engaged.status_code == 200

    first = client.post(
        "/api/action", headers={"X-Player-Token": target.session_token},
        json={"playerId": target.id, "action": "BATTLE_ATTACK"},
    )
    assert first.status_code == 200
    second = client.post(
        "/api/action", headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "BATTLE_ATTACK"},
    )
    assert second.status_code == 200
    assert app.game.round_number == 2
    assert attacker.alive and target.alive
    assert attacker.battle_id == target.battle_id
    assert attacker.battle_id is not None
    assert not attacker.round_action_complete
    assert not target.round_action_complete
    reset_state()


def test_carried_battle_elimination_counts_as_the_current_round_action() -> None:
    reset_state()
    joins, _ = start_small_game(["Arjun", "Rohan", "Priya"])
    attacker = app.game.players[joins[0]["player"]["id"]]
    target = app.game.players[joins[1]["player"]["id"]]
    third = app.game.players[joins[2]["player"]["id"]]
    target.zone_id = attacker.zone_id
    third.zone_id = "zone_3"
    normalize_stats()
    app.rng = __import__("random").Random(4)

    engaged = client.post(
        "/api/action", headers={"X-Player-Token": attacker.session_token},
        json={"playerId": attacker.id, "action": "ATTACK", "targetPlayerId": target.id},
    )
    assert engaged.status_code == 200
    for player in (target, attacker):
        response = client.post(
            "/api/action", headers={"X-Player-Token": player.session_token},
            json={"playerId": player.id, "action": "BATTLE_ATTACK"},
        )
        assert response.status_code == 200
    wait = client.post(
        "/api/action", headers={"X-Player-Token": third.session_token},
        json={"playerId": third.id, "action": "WAIT"},
    )
    assert wait.status_code == 200
    assert app.game.round_number == 2

    # Make the carried battle resolve with a kill during round 2.
    attacker.attack = 100
    wait = client.post(
        "/api/action", headers={"X-Player-Token": third.session_token},
        json={"playerId": third.id, "action": "WAIT"},
    )
    assert wait.status_code == 200
    for player in (target, attacker):
        response = client.post(
            "/api/action", headers={"X-Player-Token": player.session_token},
            json={"playerId": player.id, "action": "BATTLE_ATTACK"},
        )
        assert response.status_code == 200
    assert target.alive is False
    assert app.game.status == app.GameStatus.ACTIVE
    assert app.game.round_number == 3
    assert attacker.round_action_complete is False
    assert third.round_action_complete is False
    reset_state()


def test_admin_can_toggle_each_hazard_on_and_off() -> None:
    reset_state()
    start_small_game(["Arjun", "Rohan"])
    for zone_id in app.OUTER_ZONE_IDS:
        response = client.post(
            "/api/admin/action", headers=admin_header(),
            json={"action": "TOGGLE_HAZARD", "targetZoneId": zone_id},
        )
        assert response.status_code == 200
        assert zone_id in app.game.hazard_zones
        response = client.post(
            "/api/admin/action", headers=admin_header(),
            json={"action": "TOGGLE_HAZARD", "targetZoneId": zone_id},
        )
        assert response.status_code == 200
        assert zone_id not in app.game.hazard_zones
    reset_state()


def test_tidal_wave_counter_advances_once_per_round_for_multiple_players() -> None:
    reset_state()
    joins, _ = start_small_game(["Arjun", "Rohan", "Priya"])
    for join in joins:
        app.game.players[join["player"]["id"]].zone_id = "zone_5"
    app.game.hazard_zones.add("zone_5")
    app.rng = __import__("random").Random(1)

    app._apply_hazards_at_round_start_locked()
    assert app.game.hazard_counters["zone_5"] == 1
    assert all(p.health == 100 for p in app.game.players.values())
    app._apply_hazards_at_round_start_locked()
    assert app.game.hazard_counters["zone_5"] == 2
    assert all(p.health == 100 for p in app.game.players.values())
    app._apply_hazards_at_round_start_locked()
    assert app.game.hazard_counters["zone_5"] == 0
    assert all(45 <= p.health <= 55 for p in app.game.players.values())
    reset_state()


def test_hazard_effects_progress_and_apply() -> None:
    reset_state()
    joins, _ = start_small_game(["Lightning", "Jackers", "Rain", "Fog", "Mutts", "Tide"])
    players = [app.game.players[j["player"]["id"]] for j in joins]
    app.rng = __import__("random").Random(2)

    # Lightning / Jackers / Monkey Mutts are damage-producing hazards with agility involved.
    players[0].zone_id = "zone_1"
    players[1].zone_id = "zone_2"
    players[4].zone_id = "zone_6"
    app.game.hazard_zones.update({"zone_1", "zone_2", "zone_6"})
    before = {p.id: p.health for p in (players[0], players[1], players[4])}
    app._apply_hazards_at_round_start_locked()
    assert players[0].health < before[players[0].id] or any(e["type"] == "HAZARD_MISSED" for e in app.player_feed(players[0].id))
    assert players[1].health < before[players[1].id] or any(e["type"] == "HAZARD_MISSED" for e in app.player_feed(players[1].id))
    assert 5 <= (before[players[4].id] - players[4].health) <= 20

    # Blood Rain lowers all exposed stats by 30% through the effective-stat layer.
    players[2].zone_id = "zone_3"
    app.game.hazard_zones = {"zone_3"}
    players[2].attack = players[2].defense = players[2].agility = 10
    app._apply_hazards_at_round_start_locked()
    assert players[2].fear_turns_remaining == 4
    assert players[2].attack == 10
    assert app.effective_stat(players[2], "attack") == 7

    # Poison Fog progresses 7% -> 15% -> 25%.
    players[3].zone_id = "zone_4"
    app.game.hazard_zones = {"zone_4"}
    players[3].health = 100
    app._apply_hazards_at_round_start_locked()
    assert players[3].health == 93
    assert players[3].poison_stage == 1
    assert any("You feel a poison spreading throughout your body" in e["message"] for e in app.player_feed(players[3].id))
    app._apply_hazards_at_round_start_locked()
    assert players[3].health == 78
    assert players[3].poison_stage == 2
    assert any("A very potent poison is burning through your veins" in e["message"] for e in app.player_feed(players[3].id))
    app._apply_hazards_at_round_start_locked()
    assert players[3].health == 53
    assert players[3].poison_stage == 3
    assert any("Your body is full of poison" in e["message"] for e in app.player_feed(players[3].id))
    reset_state()


def test_end_round_eliminates_players_who_missed_action() -> None:
    reset_state()
    first = client.post("/api/join", json={"name": "Arjun", "gender": "M"}).json()
    second = client.post("/api/join", json={"name": "Rohan", "gender": "M"}).json()
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
    join = client.post("/api/join", json={"name": "Arjun", "gender": "M"}).json()
    client.post("/api/join", json={"name": "Rohan", "gender": "M"})
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


def test_twenty_four_players_can_register_and_gender_districts_pair() -> None:
    reset_state()
    players = []
    for index in range(24):
        gender = "M" if index % 2 == 0 else "F"
        response = client.post("/api/join", json={"name": f"Player {index + 1}", "gender": gender})
        assert response.status_code == 200
        players.append(response.json()["player"])
    assert len(app.game.players) == 24
    assert sorted(player["district"] for player in players if player["gender"] == "M") == list(range(1, 13))
    assert sorted(player["district"] for player in players if player["gender"] == "F") == list(range(1, 13))
    overflow = client.post("/api/join", json={"name": "Overflow", "gender": "M"})
    assert overflow.status_code == 409
    reset_state()

async def _round_resolution_smoke_test() -> None:
    reset_state()
    client.post("/api/join", json={"name": "Arjun", "gender": "M"})
    client.post("/api/join", json={"name": "Rohan", "gender": "M"})
    client.post("/api/join", json={"name": "Priya", "gender": "M"})
    client.post("/api/admin/action", headers=admin_header(), json={"action": "START_GAME"})
    admin_players = client.get("/api/admin/state", headers=admin_header()).json()["players"]
    player = app.game.players[admin_players[0]["id"]]
    player_two = app.game.players[admin_players[1]["id"]]
    player.action_taken = True
    player.round_action_complete = True
    player_two.action_taken = True
    player_two.round_action_complete = True
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
        json={"action": "GIVE_ITEM", "targetPlayerId": target_id, "itemType": "SHINY_SWORD"},
    )
    assert give.status_code == 200
    assert app.game.players[target_id].inventory[-1].type == "SHINY_SWORD"

    stats = client.post(
        "/api/admin/action",
        headers=admin_header(),
        json={"action": "SET_STATS", "targetPlayerId": target_id, "value": 77, "attack": 25, "defense": 12, "agility": 19},
    )
    assert stats.status_code == 200
    assert app.game.players[target_id].health == 77
    assert app.game.players[target_id].attack == 25
    assert app.game.players[target_id].defense == 12
    assert app.game.players[target_id].agility == 19

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
    first.round_action_complete = True
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
