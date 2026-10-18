# Every Cypher query Sev0 depends on, named and parameterised.
# The LLM never writes Cypher. It calls tools, and tools run these.

# ======================= core graph =======================
COUNT_SERVICES = "MATCH (s:Service) RETURN count(s) AS n"

SERVICE_NAMES = "MATCH (s:Service) RETURN s.name AS name"

SERVICE_NODES = """
MATCH (s:Service)
OPTIONAL MATCH (t:Team)-[:OWNS]->(s)
RETURN s.name AS name, s.kind AS kind, s.tier AS tier, s.health AS health,
       s.error_rate AS error_rate, s.p99_ms AS p99_ms, t.name AS team
"""

SERVICE_EDGES = """
MATCH (a:Service)-[d:DEPENDS_ON]->(b:Service)
RETURN a.name AS source, b.name AS target, d.critical AS critical
"""

SERVICE_EDGES_V2 = """
MATCH (a:Service)-[d:DEPENDS_ON]->(b:Service)
RETURN a.name AS source, b.name AS target, d.critical AS critical,
       d.calls AS calls, d.errors AS errors, d.source AS origin
"""

ACTIVE_ALERTS = """
MATCH (a:Alert {status: 'firing'})-[:ON]->(s:Service)
RETURN a.id AS id, s.name AS service, a.metric AS metric, a.value AS value, a.at AS at
ORDER BY at
"""

# ======================= root-cause analysis =======================
# Multi-hop: every service reachable downstream from any alerting service,
# with WHICH alerting services it explains and how far away it is.
ROOT_CAUSE_CANDIDATES = """
UNWIND $alerting AS svc
MATCH p = (s:Service {name: svc})-[:DEPENDS_ON*1..6]->(c:Service)
WITH c, collect(DISTINCT s.name) AS explains, min(length(p)) AS hops
RETURN c.name AS candidate, c.health AS health, c.kind AS kind, c.tier AS tier, explains, hops
"""

ALERTING_SELF = """
UNWIND $alerting AS svc
MATCH (c:Service {name: svc})
RETURN c.name AS candidate, c.health AS health, c.kind AS kind, c.tier AS tier
"""

# Failure frontier: unhealthy services whose own dependencies are all healthy.
FRONTIER = """
MATCH (c:Service) WHERE c.health <> 'ok'
OPTIONAL MATCH (c)-[:DEPENDS_ON]->(d:Service) WHERE d.health <> 'ok'
WITH c, count(d) AS unhealthy_deps
WHERE unhealthy_deps = 0
RETURN c.name AS service
"""

# Counterfactual: which alerting services would recover if $service were fixed.
ALERTS_EXPLAINED_BY = """
UNWIND $alerting AS svc
MATCH (s:Service {name: svc})
OPTIONAL MATCH p = (s)-[:DEPENDS_ON*1..6]->(:Service {name: $service})
WITH s, count(p) AS paths
WHERE paths > 0 OR s.name = $service
RETURN s.name AS service
"""

RECENT_DEPLOYS = """
MATCH (p:Person)-[:AUTHORED]->(d:Deploy)-[:DEPLOYED_TO]->(s:Service)
WHERE s.name IN $services AND d.at >= $since
RETURN d.id AS id, s.name AS service, d.version AS version, d.summary AS summary,
       d.at AS at, d.risky AS risky, p.name AS author
ORDER BY at DESC
"""

PAST_ROOT_CAUSES = """
UNWIND $services AS svc
MATCH (s:Service {name: svc})
OPTIONAL MATCH (i:Incident {status: 'resolved'})-[:ROOT_CAUSE]->(s)
RETURN s.name AS service, count(i) AS past
"""

DEPENDENCY_PATH = """
MATCH p = (a:Service {name: $src})-[:DEPENDS_ON*1..6]->(b:Service {name: $dst})
RETURN nodes(p) AS path, length(p) AS hops
ORDER BY hops ASC
LIMIT 1
"""

# Reverse traversal: who breaks if $service breaks.
BLAST_RADIUS = """
MATCH p = (up:Service)-[:DEPENDS_ON*1..8]->(x:Service {name: $service})
WITH up, min(length(p)) AS hops
OPTIONAL MATCH (t:Team)-[:OWNS]->(up)
RETURN up.name AS service, up.tier AS tier, hops, t.name AS team
ORDER BY hops ASC, tier ASC
"""

OWNERS = """
MATCH (t:Team)-[:OWNS]->(s:Service {name: $service})
OPTIONAL MATCH (p:Person)-[:ON_CALL]->(t)
RETURN t.name AS team, t.channel AS channel, collect(p.name) AS on_call, collect(p.handle) AS handles
"""

EXPERTS = """
MATCH (p:Person)-[:RESPONDED_TO]->(i:Incident)-[:ROOT_CAUSE|AFFECTED]->(s:Service {name: $service})
RETURN p.name AS person, p.handle AS handle, count(DISTINCT i) AS incidents
ORDER BY incidents DESC
LIMIT 3
"""

SIMILAR_INCIDENTS = """
UNWIND $services AS svc
MATCH (i:Incident {status: 'resolved'})-[:ROOT_CAUSE|AFFECTED]->(s:Service {name: svc})
WITH i, collect(DISTINCT s.name) AS overlap
OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(rc:Service)
OPTIONAL MATCH (i)-[:RESOLVED_WITH]->(r:Runbook)
RETURN i.id AS id, i.title AS title, rc.name AS root_cause, i.fix AS fix,
       overlap, size(overlap) AS score, r.title AS runbook, r.steps AS steps
ORDER BY score DESC
LIMIT 3
"""

PAGERANK = """
CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
RETURN node.name AS service, score
ORDER BY score DESC
"""

CRITICALITY_FALLBACK = """
MATCH (s:Service)
OPTIONAL MATCH (u:Service)-[:DEPENDS_ON*1..8]->(s)
RETURN s.name AS service, count(DISTINCT u) AS score
ORDER BY score DESC
"""

# ======================= live telemetry =======================
# Topology discovery from spans: one batched upsert per detector tick.
UPSERT_OBSERVED_EDGES = """
UNWIND range(0, size($src) - 1) AS i
WITH $src[i] AS s, $dst[i] AS t, $calls[i] AS c, $errs[i] AS e
MERGE (a:Service {name: s})
ON CREATE SET a.kind = 'api', a.tier = 3, a.health = 'ok', a.error_rate = 0.0, a.p99_ms = 0, a.base_p99 = 0, a.discovered = 'otel'
MERGE (b:Service {name: t})
ON CREATE SET b.kind = 'api', b.tier = 3, b.health = 'ok', b.error_rate = 0.0, b.p99_ms = 0, b.base_p99 = 0, b.discovered = 'otel'
MERGE (a)-[d:DEPENDS_ON]->(b)
ON CREATE SET d.critical = 1, d.first_seen = $now, d.source = 'otel'
SET d.calls = coalesce(d.calls, 0) + c, d.errors = coalesce(d.errors, 0) + e, d.last_seen = $now
"""

# Temporal topology: stale observed edges are removed and remembered as TopologyChange nodes.
EXPIRE_STALE_EDGES = """
MATCH (a:Service)-[d:DEPENDS_ON]->(b:Service)
WHERE d.source = 'otel' AND d.last_seen < $cutoff
CREATE (:TopologyChange {src: a.name, dst: b.name, change: 'removed', first_seen: d.first_seen, at: $now})
DELETE d
"""

TOPOLOGY_AT = """
MATCH (a:Service)-[d:DEPENDS_ON]->(b:Service)
WHERE coalesce(d.first_seen, 0) <= $at
RETURN a.name AS source, b.name AS target
UNION
MATCH (c:TopologyChange {change: 'removed'})
WHERE coalesce(c.first_seen, 0) <= $at AND c.at > $at
RETURN c.src AS source, c.dst AS target
"""

UPDATE_HEALTH = """
UNWIND range(0, size($names) - 1) AS i
WITH $names[i] AS n, $health[i] AS h, $err[i] AS e, $p99[i] AS p
MATCH (s:Service {name: n})
SET s.health = h, s.error_rate = e, s.p99_ms = p
"""

RESET_HEALTH = "MATCH (s:Service) SET s.health = 'ok', s.error_rate = 0.1"

FIRE_DETECTED_ALERT = """
MATCH (s:Service {name: $service})
CREATE (:Alert {id: $id, metric: $metric, value: $value, status: 'firing', at: $at, source: 'detector'})-[:ON]->(s)
"""

CLEAR_SERVICE_ALERTS = """
MATCH (a:Alert {status: 'firing'})-[:ON]->(s:Service {name: $service})
SET a.status = 'cleared', a.cleared_at = $at
"""

MITIGATE_INCIDENTS = """
MATCH (i:Incident {status: 'open'})-[:TRIGGERED_BY]->(a:Alert)
WITH i, sum(CASE WHEN a.status = 'firing' THEN 1 ELSE 0 END) AS firing
WHERE firing = 0
SET i.status = 'mitigated', i.mitigated_at = $now
RETURN i.id AS id
"""

CREATE_DEPLOY_WEBHOOK = """
MATCH (s:Service {name: $service})
MERGE (p:Person {handle: $author})
ON CREATE SET p.name = $author
CREATE (p)-[:AUTHORED]->(:Deploy {id: $id, version: $version, summary: $summary, at: $at, risky: $risky, source: 'webhook'})-[:DEPLOYED_TO]->(s)
RETURN $id AS id
"""

# ======================= agent memory =======================
OPEN_INCIDENT = """
CREATE (i:Incident {id: $id, title: $title, status: 'investigating', started_at: $at})
RETURN i.id AS id
"""

LINK_FIRING_ALERTS = """
MATCH (i:Incident {id: $id})
MATCH (a:Alert {status: 'firing'})
MERGE (i)-[:TRIGGERED_BY]->(a)
"""

LOG_STEP = """
MATCH (i:Incident {id: $id})
CREATE (st:AgentStep {seq: $seq, agent: $agent, tool: $tool, args: $args, result: $result, at: $at})
CREATE (st)-[:PART_OF]->(i)
"""

LOG_HANDOFF = """
MATCH (i:Incident {id: $id})
CREATE (h:Handoff {from_agent: $from_agent, to_agent: $to_agent, note: $note, at: $at})
CREATE (h)-[:FOR]->(i)
"""

CONCLUDE_INCIDENT = """
MATCH (i:Incident {id: $id}), (rc:Service {name: $root})
SET i.status = 'open', i.root_cause = $root, i.summary = $summary, i.fix = $fix,
    i.confidence = $conf, i.severity = $sev
MERGE (i)-[:ROOT_CAUSE]->(rc)
"""

LINK_AFFECTED = """
MATCH (i:Incident {id: $id})
UNWIND $services AS svc
MATCH (s:Service {name: svc})
MERGE (i)-[:AFFECTED]->(s)
"""

LINK_DEPLOY = """
MATCH (i:Incident {id: $id}), (d:Deploy {id: $deploy})
MERGE (i)-[:CAUSED_BY]->(d)
"""

RESOLVE_INCIDENT = """
MATCH (i:Incident {id: $id})
SET i.status = 'resolved', i.resolved_at = $at
RETURN i.id AS id
"""

LINK_RESPONDERS = """
MATCH (i:Incident {id: $id})-[:ROOT_CAUSE]->(s:Service)<-[:OWNS]-(t:Team)<-[:ON_CALL]-(p:Person)
MERGE (p)-[:RESPONDED_TO]->(i)
"""

LINK_RUNBOOK = """
MATCH (i:Incident {id: $id})-[:ROOT_CAUSE]->(s:Service)<-[:FOR]-(r:Runbook)
MERGE (i)-[:RESOLVED_WITH]->(r)
"""

LIST_INCIDENTS = """
MATCH (i:Incident)
OPTIONAL MATCH (i)-[:ROOT_CAUSE]->(rc:Service)
RETURN i.id AS id, i.title AS title, i.status AS status, i.severity AS severity,
       rc.name AS root_cause, i.started_at AS started_at, i.confidence AS confidence
ORDER BY started_at DESC
LIMIT 25
"""

INCIDENT_TRACE = """
MATCH (st:AgentStep)-[:PART_OF]->(i:Incident {id: $id})
RETURN st.seq AS seq, st.agent AS agent, st.tool AS tool, st.args AS args, st.result AS result
ORDER BY seq
"""

# ======================= remediation (policy as graph) =======================
PROPOSE_ACTION = """
MATCH (i:Incident {id: $inc}), (s:Service {name: $target})
CREATE (a:Action {id: $id, type: $type, status: 'proposed', risk: $risk, required: $required, reason: $reason, created_at: $at})
CREATE (a)-[:FOR]->(i)
CREATE (a)-[:TARGETS]->(s)
RETURN a.id AS id
"""

# Approver must be on the owning team, or on platform.
CAN_APPROVE = """
MATCH (a:Action {id: $action})-[:TARGETS]->(s:Service)
MATCH (p:Person {handle: $handle})-[:MEMBER_OF]->(t:Team)
OPTIONAL MATCH (t)-[o:OWNS]->(s)
WITH p, t, count(o) AS owns
WHERE owns > 0 OR t.name = 'platform'
RETURN p.name AS approver, t.name AS team
"""

RECORD_APPROVAL = """
MATCH (a:Action {id: $action}), (p:Person {handle: $handle})
MERGE (p)-[r:APPROVED]->(a)
ON CREATE SET r.at = $at
WITH a
MATCH (x:Person)-[:APPROVED]->(a)
WITH a, count(DISTINCT x) AS n
SET a.status = CASE WHEN n >= a.required AND a.status = 'proposed' THEN 'approved' ELSE a.status END
RETURN a.status AS status, n AS approvals, a.required AS required
"""

EXECUTE_ACTION = """
MATCH (a:Action {id: $action})-[:TARGETS]->(s:Service)
WHERE a.status = 'approved'
SET a.status = 'executed', a.executed_at = $at
RETURN s.name AS target, a.type AS type
"""

LIST_ACTIONS = """
MATCH (a:Action)-[:FOR]->(i:Incident {id: $inc})
MATCH (a)-[:TARGETS]->(s:Service)
OPTIONAL MATCH (p:Person)-[:APPROVED]->(a)
RETURN a.id AS id, a.type AS type, a.status AS status, a.risk AS risk, a.required AS required,
       a.reason AS reason, s.name AS target, collect(p.handle) AS approvers
ORDER BY id
"""

# ======================= evals and benchmark =======================
AGE_EVAL_DEPLOYS = "MATCH (d:Deploy) WHERE d.id STARTS WITH 'EVAL-' SET d.at = 0"

BULK_CREATE_SERVICES = """
UNWIND range(0, size($names) - 1) AS i
WITH $names[i] AS n, $tiers[i] AS t
CREATE (:Service {name: n, kind: 'api', tier: t, health: 'ok', error_rate: 0.1, p99_ms: 50, base_p99: 50})
"""

BULK_CREATE_EDGES = """
UNWIND range(0, size($src) - 1) AS i
WITH $src[i] AS a, $dst[i] AS b
MATCH (x:Service {name: a}), (y:Service {name: b})
CREATE (x)-[:DEPENDS_ON {critical: 1}]->(y)
"""
