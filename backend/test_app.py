from fastapi.testclient import TestClient

import app


client = TestClient(app.app)


def reset_state() -> None:
    app.game.status = app.GameStatus.LOBBY
    app.game.players.clear()
    app.game.event_log.clear()


def test_health() -> None:
    assert client.get("/api/health").json() == {"status": "ok"}


def test_join_player() -> None:
    reset_state()
    response = client.post("/api/join", json={"name": "Arjun"})
    assert response.status_code == 200
    data = response.json()
    assert data["player"]["id"] == "P-001"
    assert data["player"]["name"] == "Arjun"
    assert data["sessionToken"]


def test_duplicate_name_rejected() -> None:
    reset_state()
    client.post("/api/join", json={"name": "Arjun"})
    response = client.post("/api/join", json={"name": " arjun "})
    assert response.status_code == 409


def test_admin_requires_token() -> None:
    reset_state()
    response = client.get("/api/admin/state")
    assert response.status_code == 401


def test_admin_can_start_game() -> None:
    reset_state()
    client.post("/api/join", json={"name": "Arjun"})
    response = client.post(
        "/api/admin/action",
        headers={"X-Admin-Token": app.ADMIN_TOKEN},
        json={"action": "START_GAME"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ACTIVE"

    reset_state()
