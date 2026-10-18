import random

from app import rca
from app.evals import impacted_by


def test_frontier_breaks_tie_between_unhealthy_services():
    alerting = ["a", "b", "c"]
    candidates = [
        {"service": "cart", "health": "down", "explains": ["a", "b"], "deploys": [], "past": 0},
        {"service": "pricing", "health": "down", "explains": ["a", "b", "c"], "deploys": [], "past": 0},
        {"service": "redis", "health": "ok", "explains": ["a", "b", "c"], "deploys": [], "past": 0},
    ]
    ranked = rca.score(alerting, candidates, frontier_set={"pricing"})
    assert ranked[0]["service"] == "pricing"
    assert ranked[0]["frontier"] is True


def test_healthy_service_never_gets_frontier_bonus():
    ranked = rca.score(["a"], [{"service": "x", "health": "ok", "explains": ["a"]}], frontier_set={"x"})
    assert ranked[0]["frontier"] is False


def test_impacted_by_walks_reverse_critical_edges():
    impacted = impacted_by("redis-cache", random.Random(1))
    assert {"auth-service", "cart-service", "pricing-service"} <= impacted
    assert "redis-cache" not in impacted
