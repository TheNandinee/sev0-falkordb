# Graph operations: agent tools, the v1 heuristic (kept as an eval baseline) and memory writes.
import json
import time

from . import queries as Q
from .config import settings
from .db import run

HEALTH_WEIGHT = {"down": 1.0, "degraded": 0.5, "ok": 0.0}
WEIGHTS = {"coverage": 0.45, "health": 0.25, "deploy": 0.20, "history": 0.10}


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def has_data(tenant=None):
    rows = run(Q.COUNT_SERVICES, tenant=tenant, readonly=True)
    return bool(rows) and rows[0]["n"] > 0


def get_active_alerts(tenant=None):
    rows = run(Q.ACTIVE_ALERTS, tenant=tenant, readonly=True)
    return {"alerts": rows, "services": sorted({r["service"] for r in rows})}


def score_candidates(alerting, candidates):
    alerting = set(alerting)
    total = max(len(alerting), 1)
    ranked = []
    for c in candidates:
        hit = sorted(set(c.get("explains") or []) & alerting)
        coverage = len(hit) / total
        health = HEALTH_WEIGHT.get(c.get("health"), 0.0)
        deploy = 1.0 if c.get("deploys") else 0.0
        history = min(int(c.get("past") or 0), 3) / 3
        score = (WEIGHTS["coverage"] * coverage + WEIGHTS["health"] * health
                 + WEIGHTS["deploy"] * deploy + WEIGHTS["history"] * history)
        evidence = [f"{len(hit)}/{total} alerting services depend on it"]
        if health:
            evidence.append(f"health is {c.get('health')}")
        if deploy:
            evidence.append(f"{len(c['deploys'])} recent deploy(s)")
        if history:
            evidence.append(f"root cause of {c.get('past')} past incident(s)")
        ranked.append({**c, "explains": hit, "coverage": round(coverage, 2),
                       "score": round(score, 3), "evidence": evidence})
    ranked.sort(key=lambda x: (-x["score"], x.get("hops", 99)))
    return ranked


def get_recent_deploys(services=None, minutes=None, tenant=None):
    minutes = minutes or settings.deploy_lookback_min
    now = int(time.time())
    services = _as_list(services) or [r["name"] for r in run(Q.SERVICE_NAMES, tenant=tenant, readonly=True)]
    rows = run(Q.RECENT_DEPLOYS, {"services": services, "since": now - minutes * 60}, tenant=tenant, readonly=True)
    for r in rows:
        r["minutes_ago"] = max(0, (now - int(r["at"])) // 60)
    return {"window_min": minutes, "deploys": rows}


def rank_root_causes(alerting=None, tenant=None):
    # v1 heuristic (baseline for evals). v2 lives in rca.py.
    alerting = _as_list(alerting) or get_active_alerts(tenant)["services"]
    if not alerting:
        return {"alerting": [], "candidates": [], "note": "no firing alerts"}
    found = {}
    for r in run(Q.ROOT_CAUSE_CANDIDATES, {"alerting": alerting}, tenant=tenant, readonly=True):
        found[r["candidate"]] = {
            "service": r["candidate"], "health": r["health"], "kind": r["kind"], "tier": r["tier"],
            "explains": list(r["explains"] or []), "hops": r["hops"], "deploys": [], "past": 0,
        }
    for r in run(Q.ALERTING_SELF, {"alerting": alerting}, tenant=tenant, readonly=True):
        c = found.setdefault(r["candidate"], {
            "service": r["candidate"], "health": r["health"], "kind": r["kind"], "tier": r["tier"],
            "explains": [], "hops": 0, "deploys": [], "past": 0,
        })
        if r["candidate"] not in c["explains"]:
            c["explains"].append(r["candidate"])
        c["hops"] = 0
    names = list(found)
    for d in get_recent_deploys(names, tenant=tenant)["deploys"]:
        found[d["service"]]["deploys"].append(
            {k: d[k] for k in ("id", "version", "summary", "author", "minutes_ago")})
    for p in run(Q.PAST_ROOT_CAUSES, {"services": names}, tenant=tenant, readonly=True):
        found[p["service"]]["past"] = p["past"]
    ranked = score_candidates(alerting, found.values())
    return {"alerting": alerting, "candidates": ranked[:5]}


def explain_dependency_path(src, dst, tenant=None):
    if src == dst:
        return {"path": [src], "hops": 0}
    rows = run(Q.DEPENDENCY_PATH, {"src": src, "dst": dst}, tenant=tenant, readonly=True)
    if not rows:
        return {"path": [], "hops": None, "note": f"{src} does not depend on {dst}"}
    path = [n.get("name") if isinstance(n, dict) else str(n) for n in rows[0]["path"]]
    return {"path": path, "hops": rows[0]["hops"]}


def get_blast_radius(service, tenant=None):
    rows = run(Q.BLAST_RADIUS, {"service": service}, tenant=tenant, readonly=True)
    return {
        "service": service,
        "impacted": rows,
        "tier1": sum(1 for r in rows if r.get("tier") == 1),
        "teams": sorted({r["team"] for r in rows if r.get("team")}),
    }


def find_similar_incidents(services, tenant=None):
    rows = run(Q.SIMILAR_INCIDENTS, {"services": _as_list(services)}, tenant=tenant, readonly=True)
    return {"incidents": rows}


def get_owners_and_experts(service, tenant=None):
    owners = run(Q.OWNERS, {"service": service}, tenant=tenant, readonly=True)
    experts = run(Q.EXPERTS, {"service": service}, tenant=tenant, readonly=True)
    o = owners[0] if owners else {}
    return {
        "service": service, "team": o.get("team"), "channel": o.get("channel"),
        "on_call": o.get("on_call") or [], "handles": o.get("handles") or [], "experts": experts,
    }


def criticality(limit=6, tenant=None):
    try:
        rows = run(Q.PAGERANK, tenant=tenant)
        algorithm = "algo.pageRank"
    except Exception:
        rows = run(Q.CRITICALITY_FALLBACK, tenant=tenant, readonly=True)
        algorithm = "dependents count (fallback)"
    return {"algorithm": algorithm,
            "services": [{"service": r["service"], "score": round(float(r["score"]), 4)} for r in rows[:limit]]}


# ---------------- agent memory ----------------
def open_incident(inc_id, title, tenant=None):
    run(Q.OPEN_INCIDENT, {"id": inc_id, "title": title, "at": int(time.time())}, tenant=tenant)
    run(Q.LINK_FIRING_ALERTS, {"id": inc_id}, tenant=tenant)


def log_step(inc_id, seq, agent, tool, args, result, tenant=None):
    run(Q.LOG_STEP, {
        "id": inc_id, "seq": seq, "agent": agent, "tool": tool,
        "args": json.dumps(args, default=str)[:500],
        "result": json.dumps(result, default=str)[:1500],
        "at": int(time.time()),
    }, tenant=tenant)


def log_handoff(inc_id, from_agent, to_agent, note, tenant=None):
    run(Q.LOG_HANDOFF, {"id": inc_id, "from_agent": from_agent, "to_agent": to_agent,
                        "note": note, "at": int(time.time())}, tenant=tenant)


def conclude_incident(inc_id, result, affected, tenant=None):
    d = result["diagnosis"]
    run(Q.CONCLUDE_INCIDENT, {
        "id": inc_id, "root": d["root_cause_service"], "summary": d["summary"],
        "fix": d["suggested_fix"], "conf": d["confidence"], "sev": result["severity"],
    }, tenant=tenant)
    run(Q.LINK_AFFECTED, {"id": inc_id, "services": affected}, tenant=tenant)
    if d.get("suspect_deploy"):
        run(Q.LINK_DEPLOY, {"id": inc_id, "deploy": d["suspect_deploy"]["id"]}, tenant=tenant)


def resolve_incident(inc_id, tenant=None):
    rows = run(Q.RESOLVE_INCIDENT, {"id": inc_id, "at": int(time.time())}, tenant=tenant)
    run(Q.LINK_RESPONDERS, {"id": inc_id}, tenant=tenant)
    run(Q.LINK_RUNBOOK, {"id": inc_id}, tenant=tenant)
    return {"resolved": bool(rows), "id": inc_id}


def list_incidents(tenant=None):
    return run(Q.LIST_INCIDENTS, tenant=tenant, readonly=True)


def incident_trace(inc_id, tenant=None):
    return run(Q.INCIDENT_TRACE, {"id": inc_id}, tenant=tenant, readonly=True)
