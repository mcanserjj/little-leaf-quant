from fastapi.testclient import TestClient

import app.data_updates as data_updates
from app.main import app


def test_core_api_contracts():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["independent"] is True
        overview = client.get("/api/overview").json()
        assert overview["groups"] == 8
        assert "configured" in overview["ai"]
        assert "configured" in overview["hithink"]
        assert overview["execution"]["quoteRefreshSeconds"] == 5
        groups = client.get("/api/groups").json()
        assert len(groups) == 8
        updates = client.get("/api/data/updates").json()
        assert updates["sources"]["financials"]["updateSupported"] is True
        assert updates["sources"]["user_data_research_news"]["updateSupported"] is True
        assert updates["sources"]["user_data_research_news"]["credential"] is None
        assert updates["newsAutoSync"]["intervalMinutes"] == 10
        assert "龙虎榜" in updates["sources"]["market_intelligence"]["method"]
        readiness = client.get("/api/backtest/readiness").json()
        assert readiness["totalGroups"] == 8
        assert "不填充" in readiness["policy"]
        assert client.get("/api/backtest/status").status_code == 200
        assert client.get("/api/reviews/schedule/status").status_code == 200
        strategies = client.get("/api/strategies").json()
        assert len(strategies) == 8
        assert all(row["activeVersion"] >= 1 for row in strategies)
        assert client.get("/api/execution/status").json()["strategyEvaluationSeconds"] == 600


def test_news_auto_sync_can_be_enabled_through_api(monkeypatch, tmp_path):
    monkeypatch.setattr(data_updates, "NEWS_SYNC_SETTINGS_PATH", tmp_path / "news-settings.json")

    with TestClient(app) as client:
        response = client.put("/api/data/news/auto-sync", json={"enabled": True})

        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert client.get("/api/data/updates").json()["newsAutoSync"]["enabled"] is True
