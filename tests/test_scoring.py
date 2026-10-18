from app.graph_ops import score_candidates

ALERTING = ["checkout-service", "search-service", "cart-service", "api-gateway"]


def test_broken_service_with_recent_deploy_beats_shared_cache():
    candidates = [
        {"service": "pricing-service", "health": "down", "explains": ALERTING, "hops": 1,
         "deploys": [{"id": "d1"}], "past": 1},
        {"service": "redis-cache", "health": "ok", "explains": ALERTING, "hops": 2, "deploys": [], "past": 1},
        {"service": "checkout-service", "health": "degraded",
         "explains": ["checkout-service", "api-gateway"], "hops": 0, "deploys": [], "past": 0},
    ]
    ranked = score_candidates(ALERTING, candidates)
    assert ranked[0]["service"] == "pricing-service"
    assert ranked[0]["coverage"] == 1.0


def test_coverage_only_counts_alerting_services():
    ranked = score_candidates(["a"], [{"service": "x", "health": "ok", "explains": ["a", "b"]}])
    assert ranked[0]["explains"] == ["a"]
    assert ranked[0]["coverage"] == 1.0
