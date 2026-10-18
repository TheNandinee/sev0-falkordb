# Runs against a real FalkorDB. Skipped automatically if none is reachable.
import pytest

from app import evals, rca, remediation
from app import graph_ops as G
from app import seed as S
from app.db import ping

try:
    ping()
    DB = True
except Exception:
    DB = False

pytestmark = pytest.mark.skipif(not DB, reason="FalkorDB not reachable")
TENANT = "pytest"


@pytest.fixture(scope="module", autouse=True)
def fresh_graph():
    S.seed(TENANT)
    yield


@pytest.mark.parametrize("scenario,expected", [
    ("pricing-deploy", "pricing-service"),
    ("psp-outage", "psp-gateway"),
    ("redis-meltdown", "redis-cache"),
])
def test_scenarios_rank_true_root_first(scenario, expected):
    S.simulate(scenario, TENANT)
    alerting = G.get_active_alerts(TENANT)["services"]
    ranked = rca.rank_root_causes(alerting, TENANT)["candidates"]
    assert ranked[0]["service"] == expected


def test_counterfactual_explains_all_pricing_alerts():
    S.simulate("pricing-deploy", TENANT)
    alerting = G.get_active_alerts(TENANT)["services"]
    cf = rca.counterfactual("pricing-service", alerting, TENANT)
    assert cf["fraction"] == 1.0


def test_approval_policy_blocks_wrong_team():
    S.simulate("pricing-deploy", TENANT)
    diag = {"root_cause_service": "pricing-service", "kind": "api", "suspect_deploy": None}
    G.open_incident("INC-test-1", "test", TENANT)
    action = remediation.propose("INC-test-1", diag, {"tier1": 0, "impacted": []}, TENANT)
    with pytest.raises(PermissionError):
        remediation.approve(action["id"], "kabir", TENANT)      # payments team, not owner
    result = remediation.approve(action["id"], "neel", TENANT)  # discovery owns pricing
    assert result["status"] == "approved"


def test_eval_accuracy_floor():
    summary = evals.run_evals(n=40, seed_value=3, tenant="pytest_eval")
    assert summary["v2"]["top3"] >= 0.6
