from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_liveness() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_readiness_queries_postgresql(database_reachable: bool) -> None:
    response = client.get("/health/ready")

    if database_reachable:
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ready"}
    else:
        assert response.status_code == 503
        assert response.json()["detail"] == "database_unavailable"
