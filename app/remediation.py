# Approval-gated remediation. The policy lives in the graph:
# an approver must be on the team that owns the target (or on platform),
# and high-risk actions need two distinct approvers. Every approval is an edge (audit trail).
import time

from . import queries as Q
from .db import run
from .observability import event, metrics


class ActionStateError(Exception):
    pass


KIND_ACTION = {"external": "failover", "cache": "scale", "database": "scale", "queue": "scale", "search": "scale"}


def propose(inc_id, diagnosis, blast, tenant=None):
    target = diagnosis["root_cause_service"]
    deploy = diagnosis.get("suspect_deploy")
    action_type = "rollback" if deploy else KIND_ACTION.get(diagnosis.get("kind"), "restart")
    tier1 = int(blast.get("tier1") or 0)
    risk = "high" if tier1 >= 4 else ("medium" if tier1 >= 1 else "low")
    required = 2 if risk == "high" else 1
    if deploy:
        reason = f"Roll back {deploy['version']} on {target}; it was deployed {deploy['minutes_ago']} min before the alerts."
    else:
        reason = f"{action_type.capitalize()} {target}; it is the deepest unhealthy dependency shared by the alerts."
    reason += f" Blast radius: {len(blast.get('impacted') or [])} services, {tier1} tier-1."
    action_id = "ACT-" + inc_id.split("-", 1)[1]
    run(Q.PROPOSE_ACTION, {"inc": inc_id, "target": target, "id": action_id, "type": action_type, "risk": risk,
                           "required": required, "reason": reason, "at": int(time.time())}, tenant=tenant)
    metrics.inc("sev0_actions_total", status="proposed")
    event("action.proposed", incident=inc_id, action=action_id, type=action_type, risk=risk)
    return {"id": action_id, "type": action_type, "target": target, "risk": risk, "required": required,
            "status": "proposed", "reason": reason, "approvers": []}


def approve(action_id, handle, tenant=None):
    allowed = run(Q.CAN_APPROVE, {"action": action_id, "handle": handle}, tenant=tenant, readonly=True)
    if not allowed:
        raise PermissionError(f"@{handle} cannot approve {action_id}: approvers must be on the team that owns "
                              f"the target service, or on the platform team.")
    rows = run(Q.RECORD_APPROVAL, {"action": action_id, "handle": handle, "at": int(time.time())}, tenant=tenant)
    if not rows:
        raise ActionStateError(f"{action_id} not found")
    metrics.inc("sev0_actions_total", status="approval")
    event("action.approved", action=action_id, by=handle, status=rows[0]["status"])
    return rows[0]


def execute(action_id, tenant=None, sim=None):
    rows = run(Q.EXECUTE_ACTION, {"action": action_id, "at": int(time.time())}, tenant=tenant)
    if not rows:
        raise ActionStateError(f"{action_id} is not approved yet, or does not exist.")
    target = rows[0]["target"]
    if sim is not None:
        sim.clear(target)
        effect = f"Fault on {target} cleared in live traffic. Alerts will drain as the detector sees recovery."
    else:
        from . import seed
        seed.reset_outage(tenant)
        effect = "Scripted outage reset."
    metrics.inc("sev0_actions_total", status="executed")
    event("action.executed", action=action_id, target=target)
    return {"id": action_id, "status": "executed", "target": target, "effect": effect}


def list_actions(inc_id, tenant=None):
    return run(Q.LIST_ACTIONS, {"inc": inc_id}, tenant=tenant, readonly=True)
