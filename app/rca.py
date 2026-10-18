# RCA v2: graph-causal ranking.
# The "frontier" signal: a service that is unhealthy while none of its own dependencies are
# is where the failure starts; everything above it is a symptom.
from . import graph_ops as G
from . import queries as Q
from .db import run

WEIGHTS = {"coverage": 0.35, "frontier": 0.20, "health": 0.15, "deploy": 0.20, "history": 0.10}
MODEL = "v2-frontier"


def _blank(r, hops):
    return {"service": r["candidate"], "health": r["health"], "kind": r["kind"], "tier": r["tier"],
            "explains": [], "hops": hops, "deploys": [], "past": 0}


def gather_candidates(alerting, tenant=None):
    found = {}
    for r in run(Q.ROOT_CAUSE_CANDIDATES, {"alerting": alerting}, tenant=tenant, readonly=True):
        c = _blank(r, r["hops"])
        c["explains"] = list(r["explains"] or [])
        found[r["candidate"]] = c
    for r in run(Q.ALERTING_SELF, {"alerting": alerting}, tenant=tenant, readonly=True):
        c = found.setdefault(r["candidate"], _blank(r, 0))
        if r["candidate"] not in c["explains"]:
            c["explains"].append(r["candidate"])
        c["hops"] = 0
    names = list(found)
    if names:
        for d in G.get_recent_deploys(names, tenant=tenant)["deploys"]:
            if d["service"] in found:
                found[d["service"]]["deploys"].append(
                    {k: d[k] for k in ("id", "version", "summary", "author", "minutes_ago")})
        for p in run(Q.PAST_ROOT_CAUSES, {"services": names}, tenant=tenant, readonly=True):
            found[p["service"]]["past"] = p["past"]
    return found


def frontier(tenant=None):
    return {r["service"] for r in run(Q.FRONTIER, tenant=tenant, readonly=True)}


def score(alerting, candidates, frontier_set):
    # Pure function (unit tested).
    alerting = set(alerting)
    total = max(len(alerting), 1)
    ranked = []
    for c in candidates:
        hit = sorted(set(c.get("explains") or []) & alerting)
        coverage = len(hit) / total
        health = G.HEALTH_WEIGHT.get(c.get("health"), 0.0)
        is_frontier = 1.0 if (c["service"] in frontier_set and health > 0) else 0.0
        deploy = 1.0 if c.get("deploys") else 0.0
        history = min(int(c.get("past") or 0), 3) / 3
        s = (WEIGHTS["coverage"] * coverage + WEIGHTS["frontier"] * is_frontier + WEIGHTS["health"] * health
             + WEIGHTS["deploy"] * deploy + WEIGHTS["history"] * history)
        evidence = [f"{len(hit)}/{total} alerting services depend on it"]
        if is_frontier:
            evidence.append("unhealthy while all of its own dependencies are healthy (failure frontier)")
        elif health:
            evidence.append(f"health is {c.get('health')}")
        if deploy:
            evidence.append(f"{len(c['deploys'])} recent deploy(s)")
        if history:
            evidence.append(f"root cause of {c.get('past')} past incident(s)")
        ranked.append({**c, "explains": hit, "coverage": round(coverage, 2), "frontier": bool(is_frontier),
                       "score": round(s, 3), "evidence": evidence})
    ranked.sort(key=lambda x: (-x["score"], x.get("hops", 99)))
    return ranked


def rank_root_causes(alerting=None, tenant=None, limit=5):
    alerting = G._as_list(alerting) or G.get_active_alerts(tenant)["services"]
    if not alerting:
        return {"alerting": [], "candidates": [], "note": "no firing alerts", "model": MODEL}
    ranked = score(alerting, gather_candidates(alerting, tenant).values(), frontier(tenant))
    if len(ranked) > 1:
        margin = round(ranked[0]["score"] - ranked[1]["score"], 3)
    else:
        margin = ranked[0]["score"] if ranked else 0.0
    return {"alerting": alerting, "candidates": ranked[:limit], "margin": margin, "model": MODEL}


def counterfactual(service, alerting, tenant=None):
    # "If we fix X, which alerts clear?" = alerting services whose dependency paths reach X.
    alerting = G._as_list(alerting)
    if not alerting or not service:
        return {"service": service, "would_clear": [], "remaining": alerting, "fraction": 0.0}
    rows = run(Q.ALERTS_EXPLAINED_BY, {"alerting": alerting, "service": service}, tenant=tenant, readonly=True)
    clears = sorted({r["service"] for r in rows})
    return {"service": service, "would_clear": clears, "remaining": sorted(set(alerting) - set(clears)),
            "fraction": round(len(clears) / len(alerting), 2)}
