# Synthetic company "Kirana Cloud", a fictional quick-commerce startup.
# All data is invented (hackathon rule 6).
import time

from . import queries as Q
from .db import graph, run

SERVICES = [  # name, kind, tier (1 = most critical), baseline p99 ms
    ("web-frontend", "frontend", 1, 180), ("mobile-bff", "frontend", 1, 160),
    ("api-gateway", "gateway", 1, 40), ("auth-service", "api", 1, 60),
    ("user-service", "api", 2, 50), ("catalog-service", "api", 2, 90),
    ("search-service", "api", 2, 120), ("pricing-service", "api", 1, 45),
    ("cart-service", "api", 1, 70), ("checkout-service", "api", 1, 150),
    ("payment-service", "api", 1, 220), ("inventory-service", "api", 2, 60),
    ("order-service", "api", 1, 80), ("notification-service", "worker", 3, 300),
    ("delivery-service", "api", 2, 110), ("redis-cache", "cache", 1, 3),
    ("postgres-users", "database", 1, 8), ("postgres-catalog", "database", 2, 10),
    ("postgres-orders", "database", 1, 12), ("kafka-events", "queue", 1, 15),
    ("elasticsearch", "search", 2, 35), ("psp-gateway", "external", 1, 400),
    ("sms-provider", "external", 3, 600),
]

DEPENDENCIES = [  # caller, callee, critical
    ("web-frontend", "api-gateway", 1), ("mobile-bff", "api-gateway", 1),
    ("api-gateway", "auth-service", 1), ("api-gateway", "catalog-service", 1),
    ("api-gateway", "search-service", 1), ("api-gateway", "cart-service", 1),
    ("api-gateway", "checkout-service", 1), ("api-gateway", "order-service", 1),
    ("api-gateway", "delivery-service", 0),
    ("auth-service", "user-service", 1), ("auth-service", "redis-cache", 1),
    ("user-service", "postgres-users", 1),
    ("catalog-service", "postgres-catalog", 1), ("catalog-service", "redis-cache", 0),
    ("catalog-service", "pricing-service", 1),
    ("search-service", "elasticsearch", 1), ("search-service", "pricing-service", 1),
    ("pricing-service", "redis-cache", 1), ("pricing-service", "postgres-catalog", 0),
    ("cart-service", "redis-cache", 1), ("cart-service", "pricing-service", 1),
    ("cart-service", "inventory-service", 1),
    ("checkout-service", "cart-service", 1), ("checkout-service", "payment-service", 1),
    ("checkout-service", "inventory-service", 1), ("checkout-service", "order-service", 1),
    ("payment-service", "psp-gateway", 1), ("payment-service", "postgres-orders", 1),
    ("payment-service", "kafka-events", 0),
    ("inventory-service", "postgres-catalog", 1), ("inventory-service", "kafka-events", 0),
    ("order-service", "postgres-orders", 1), ("order-service", "kafka-events", 1),
    ("notification-service", "kafka-events", 1), ("notification-service", "sms-provider", 1),
    ("delivery-service", "order-service", 1), ("delivery-service", "kafka-events", 1),
]

TEAMS = {
    "platform": "#team-platform", "identity": "#team-identity", "discovery": "#team-discovery",
    "commerce": "#team-commerce", "payments": "#team-payments", "fulfilment": "#team-fulfilment",
    "frontend": "#team-frontend",
}

OWNERSHIP = {
    "platform": ["api-gateway", "redis-cache", "kafka-events", "elasticsearch",
                 "postgres-users", "postgres-catalog", "postgres-orders"],
    "identity": ["auth-service", "user-service"],
    "discovery": ["catalog-service", "search-service", "pricing-service"],
    "commerce": ["cart-service", "checkout-service", "order-service", "inventory-service"],
    "payments": ["payment-service", "psp-gateway"],
    "fulfilment": ["delivery-service", "notification-service", "sms-provider"],
    "frontend": ["web-frontend", "mobile-bff"],
}

PEOPLE = [  # name, handle, team, on_call
    ("Aarav Mehta", "aarav", "platform", 1), ("Isha Banerjee", "isha", "platform", 0),
    ("Priya Nair", "priya", "identity", 1), ("Rohan Gupta", "rohan", "identity", 0),
    ("Neel Chatterjee", "neel", "discovery", 1), ("Tara Joshi", "tara", "discovery", 0),
    ("Vikram Rao", "vikram", "commerce", 1), ("Ananya Iyer", "ananya", "commerce", 0),
    ("Kabir Singh", "kabir", "payments", 1), ("Meera Pillai", "meera", "payments", 0),
    ("Zoya Khan", "zoya", "fulfilment", 1), ("Dev Malhotra", "dev", "fulfilment", 0),
    ("Sneha Kulkarni", "sneha", "frontend", 1), ("Arjun Desai", "arjun", "frontend", 0),
]

RUNBOOKS = [  # id, title, steps, service
    ("RB-1", "Redis memory pressure",
     "1) Check evicted_keys and used_memory. 2) Raise maxmemory or add a shard. 3) Restart hot replicas one at a time.",
     "redis-cache"),
    ("RB-2", "Roll back a bad pricing deploy",
     "1) Freeze deploys in #deploys. 2) Roll back to the last green version. 3) Warm the pricing cache. 4) Watch 5xx and p99 for 10 minutes.",
     "pricing-service"),
    ("RB-3", "PSP failover",
     "1) Confirm the PSP status page. 2) Switch payment routing to the secondary PSP. 3) Queue failed payments for retry.",
     "psp-gateway"),
    ("RB-4", "Kafka consumer lag",
     "1) Check consumer group lag. 2) Scale consumers. 3) Look for poison messages.",
     "kafka-events"),
    ("RB-5", "Postgres connection exhaustion",
     "1) Compare active connections with max_connections. 2) Kill idle-in-transaction sessions. 3) Lower the pool size on the noisy service.",
     "postgres-orders"),
]

HISTORY_DEPLOYS = [  # id, service, version, summary, author, minutes_ago, risky
    ("DEP-H1", "pricing-service", "v2.3.0", "Changed cache key format", "neel", 21 * 1440, 1),
    ("DEP-H2", "search-service", "v4.1.2", "Ranking tweak", "tara", 180, 0),
    ("DEP-H3", "payment-service", "v7.0.1", "Retry budget change", "meera", 3 * 1440, 0),
]

INCIDENTS = [  # id, title, sev, root, affected, fix, runbook, responders, days_ago, cause_deploy
    ("INC-101", "Cart and login slow during sale traffic", "SEV2", "redis-cache",
     ["cart-service", "auth-service", "pricing-service"],
     "Raised Redis maxmemory and added a shard", "RB-1", ["aarav", "priya"], 40, None),
    ("INC-102", "Checkout errors after pricing release v2.3.0", "SEV1", "pricing-service",
     ["checkout-service", "cart-service", "search-service"],
     "Rolled back pricing-service to v2.2.4; the new cache key format caused misses", "RB-2",
     ["neel", "tara"], 21, "DEP-H1"),
    ("INC-103", "UPI payments failing at checkout", "SEV1", "psp-gateway",
     ["payment-service", "checkout-service"],
     "Failed over to the secondary PSP route", "RB-3", ["kabir", "meera"], 14, None),
    ("INC-104", "Order notifications delayed by 30 minutes", "SEV3", "kafka-events",
     ["notification-service", "delivery-service"],
     "Scaled consumers and removed a poison message", "RB-4", ["aarav", "zoya"], 9, None),
    ("INC-105", "Order history timing out", "SEV2", "postgres-orders",
     ["order-service", "payment-service"],
     "Killed idle sessions and reduced the order-service pool", "RB-5", ["vikram", "aarav"], 5, None),
]

SCENARIOS = {
    "pricing-deploy": {
        "label": "Bad pricing deploy",
        "deploys": [
            ("pricing-service", "v2.5.0", "New rules engine; cache key format changed", "neel", 22, 1),
            ("notification-service", "v1.9.3", "Template copy update", "dev", 30, 0),  # red herring
        ],
        "health": {
            "pricing-service": ("down", 38.0, 4200), "cart-service": ("degraded", 12.0, 2100),
            "checkout-service": ("degraded", 18.5, 3900), "search-service": ("degraded", 9.0, 1800),
            "catalog-service": ("degraded", 6.0, 900), "api-gateway": ("degraded", 6.2, 600),
        },
        "alerts": [
            ("checkout-service", "5xx_rate_pct", 18.5), ("search-service", "p99_latency_ms", 1800),
            ("cart-service", "5xx_rate_pct", 12.0), ("api-gateway", "5xx_rate_pct", 6.2),
        ],
    },
    "psp-outage": {
        "label": "Payment provider outage",
        "deploys": [],
        "health": {
            "psp-gateway": ("down", 64.0, 30000), "payment-service": ("degraded", 41.0, 9000),
            "checkout-service": ("degraded", 22.0, 6000),
        },
        "alerts": [
            ("payment-service", "payment_failure_pct", 41.0), ("checkout-service", "5xx_rate_pct", 22.0),
        ],
    },
    "redis-meltdown": {
        "label": "Redis meltdown",
        "deploys": [],
        "health": {
            "redis-cache": ("down", 90.0, 5000), "auth-service": ("degraded", 27.0, 2600),
            "cart-service": ("degraded", 15.0, 2400), "pricing-service": ("degraded", 11.0, 1900),
            "catalog-service": ("degraded", 8.0, 1500),
        },
        "alerts": [
            ("auth-service", "login_error_pct", 27.0), ("cart-service", "p99_latency_ms", 2400),
            ("pricing-service", "p99_latency_ms", 1900), ("catalog-service", "p99_latency_ms", 1500),
        ],
    },
}

INDEXES = [
    "CREATE INDEX FOR (s:Service) ON (s.name)",
    "CREATE INDEX FOR (p:Person) ON (p.handle)",
    "CREATE INDEX FOR (t:Team) ON (t.name)",
    "CREATE INDEX FOR (i:Incident) ON (i.id)",
    "CREATE INDEX FOR (d:Deploy) ON (d.id)",
    "CREATE INDEX FOR (a:Alert) ON (a.status)",
    "CREATE INDEX FOR (x:Action) ON (x.id)",
]


def create_deploy(dep_id, service, version, summary, author, minutes_ago, risky, now, tenant=None):
    run("MATCH (s:Service {name: $s}), (p:Person {handle: $a}) "
        "CREATE (p)-[:AUTHORED]->(:Deploy {id: $id, version: $v, summary: $sum, at: $at, risky: $r})"
        "-[:DEPLOYED_TO]->(s)",
        {"s": service, "a": author, "id": dep_id, "v": version, "sum": summary,
         "at": now - minutes_ago * 60, "r": risky}, tenant=tenant)


def seed(tenant=None):
    try:
        graph(tenant).delete()
    except Exception:
        pass
    now = int(time.time())
    for name, kind, tier, p99 in SERVICES:
        run("CREATE (:Service {name: $name, kind: $kind, tier: $tier, health: 'ok', "
            "error_rate: 0.1, p99_ms: $p99, base_p99: $p99})",
            {"name": name, "kind": kind, "tier": tier, "p99": p99}, tenant=tenant)
    for stmt in INDEXES:
        try:
            run(stmt, tenant=tenant)
        except Exception:
            pass
    for a, b, critical in DEPENDENCIES:
        run("MATCH (a:Service {name: $a}), (b:Service {name: $b}) "
            "CREATE (a)-[:DEPENDS_ON {critical: $c}]->(b)", {"a": a, "b": b, "c": critical}, tenant=tenant)
    for team, channel in TEAMS.items():
        run("CREATE (:Team {name: $t, channel: $c})", {"t": team, "c": channel}, tenant=tenant)
        for svc in OWNERSHIP[team]:
            run("MATCH (t:Team {name: $t}), (s:Service {name: $s}) CREATE (t)-[:OWNS]->(s)",
                {"t": team, "s": svc}, tenant=tenant)
    for name, handle, team, on_call in PEOPLE:
        run("MATCH (t:Team {name: $t}) CREATE (:Person {name: $n, handle: $h})-[:MEMBER_OF]->(t)",
            {"t": team, "n": name, "h": handle}, tenant=tenant)
        if on_call:
            run("MATCH (p:Person {handle: $h}), (t:Team {name: $t}) CREATE (p)-[:ON_CALL]->(t)",
                {"h": handle, "t": team}, tenant=tenant)
    for rid, title, steps, svc in RUNBOOKS:
        run("MATCH (s:Service {name: $s}) CREATE (:Runbook {id: $id, title: $title, steps: $steps})-[:FOR]->(s)",
            {"s": svc, "id": rid, "title": title, "steps": steps}, tenant=tenant)
    for d in HISTORY_DEPLOYS:
        create_deploy(*d, now=now, tenant=tenant)
    for iid, title, sev, root, affected, fix, rb, responders, days, cause in INCIDENTS:
        at = now - days * 86400
        run("MATCH (rc:Service {name: $root}) "
            "CREATE (i:Incident {id: $id, title: $title, status: 'resolved', severity: $sev, started_at: $at, "
            "resolved_at: $at2, root_cause: $root, fix: $fix, summary: $title, confidence: 1.0})-[:ROOT_CAUSE]->(rc)",
            {"root": root, "id": iid, "title": title, "sev": sev, "at": at, "at2": at + 5400, "fix": fix},
            tenant=tenant)
        run(Q.LINK_AFFECTED, {"id": iid, "services": affected}, tenant=tenant)
        run("MATCH (i:Incident {id: $id}), (r:Runbook {id: $rb}) CREATE (i)-[:RESOLVED_WITH]->(r)",
            {"id": iid, "rb": rb}, tenant=tenant)
        for handle in responders:
            run("MATCH (p:Person {handle: $h}), (i:Incident {id: $id}) CREATE (p)-[:RESPONDED_TO]->(i)",
                {"h": handle, "id": iid}, tenant=tenant)
        if cause:
            run(Q.LINK_DEPLOY, {"id": iid, "deploy": cause}, tenant=tenant)
    return {"services": len(SERVICES), "dependencies": len(DEPENDENCIES), "people": len(PEOPLE),
            "incidents": len(INCIDENTS), "runbooks": len(RUNBOOKS)}


def reset_outage(tenant=None):
    run("MATCH (s:Service) SET s.health = 'ok', s.error_rate = 0.1, s.p99_ms = s.base_p99", tenant=tenant)
    run("MATCH (a:Alert {status: 'firing'}) SET a.status = 'cleared'", tenant=tenant)
    return {"reset": True}


def simulate(name, tenant=None):
    # Scripted mode (used when live telemetry is off, and by tests).
    sc = SCENARIOS[name]
    reset_outage(tenant)
    now = int(time.time())
    for service, version, summary, author, minutes_ago, risky in sc["deploys"]:
        create_deploy(f"DEP-{service}-{now}", service, version, summary, author, minutes_ago, risky,
                      now=now, tenant=tenant)
    for svc, (health, err, p99) in sc["health"].items():
        run("MATCH (s:Service {name: $s}) SET s.health = $h, s.error_rate = $e, s.p99_ms = $p",
            {"s": svc, "h": health, "e": err, "p": p99}, tenant=tenant)
    for i, (svc, metric, value) in enumerate(sc["alerts"]):
        run("MATCH (s:Service {name: $s}) "
            "CREATE (:Alert {id: $id, metric: $m, value: $v, status: 'firing', at: $at})-[:ON]->(s)",
            {"s": svc, "id": f"AL-{now}-{i}", "m": metric, "v": value, "at": now - (len(sc["alerts"]) - i) * 30},
            tenant=tenant)
    return {"scenario": name, "label": sc["label"], "alerts": len(sc["alerts"])}
