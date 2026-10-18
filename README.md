# Sev0

**The graph-native on-call agent.**

Sev0 turns production into a live FalkorDB graph of services, dependencies, teams, people, deploys, alerts, incidents and runbooks. When something breaks, a team of agents walks the graph to find the root cause, proves it with dependency paths and a counterfactual, proposes an approval-gated fix, and writes the status update and postmortem. Every step is saved back into the graph as memory.

**Tracks:** Agents That Act on Connected Data (primary) · Agent Memory and Coordination

> Demo video: `ADD LINK` · Blog: `ADD LINK` · Eval results: `PASTE YOUR NUMBERS FROM scripts/run_evals.py`

## What happens end to end

1. **Telemetry flows in.** The built-in traffic generator (or your own `/api/ingest/spans`) sends distributed-trace spans. Sev0 discovers the dependency graph from them and records when each edge was first and last seen.
2. **Alerts emerge.** An anomaly detector learns per-service baselines for error rate and p99 latency, then fires and clears alerts in the graph. Nothing is scripted.
3. **Agents investigate**, streamed live to the UI:
   - The investigator (Groq tool calling, or a deterministic planner) runs graph-causal RCA, traces dependency paths, checks the "what if we fixed it" counterfactual, deploys, blast radius, similar incidents and owners.
   - The verifier re-ranks with RCA v2 and overrides the LLM if its pick isn't supported by the graph.
   - The responder proposes a remediation (rollback, failover, scale or restart) scored by blast-radius risk.
   - The scribe drafts comms from verified facts only.
4. **Humans approve.** The policy lives in the graph: approvers must be on the owning team or on platform, and high-risk actions need two distinct approvers. Executing the action clears the fault, the detector sees recovery, and the incident flips to `mitigated` on its own.
5. **Memory compounds.** Resolving an incident links responders and runbooks, and future investigations use them.

## Graph data model

~~~
(:Person)-[:MEMBER_OF]->(:Team)-[:OWNS]->(:Service)-[:DEPENDS_ON {critical, calls, errors, first_seen, last_seen, source}]->(:Service)
(:Person)-[:ON_CALL]->(:Team)
(:Person)-[:AUTHORED]->(:Deploy {source: seed|webhook})-[:DEPLOYED_TO]->(:Service)
(:Alert {status, source: detector})-[:ON]->(:Service)
(:Runbook)-[:FOR]->(:Service)
(:TopologyChange {src, dst, change, first_seen, at})                       temporal topology

(:Incident {status: investigating|open|mitigated|resolved})
   -[:ROOT_CAUSE|AFFECTED]->(:Service)  -[:CAUSED_BY]->(:Deploy)
   -[:TRIGGERED_BY]->(:Alert)           -[:RESOLVED_WITH]->(:Runbook)
(:Person)-[:RESPONDED_TO]->(:Incident)

(:AgentStep {seq, agent, tool, args, result})-[:PART_OF]->(:Incident)      agent memory
(:Handoff {from_agent, to_agent, note})-[:FOR]->(:Incident)                 agent coordination
(:Action {type, status, risk, required})-[:FOR]->(:Incident), -[:TARGETS]->(:Service)
(:Person)-[:APPROVED {at}]->(:Action)                                       audit trail
~~~

One isolated graph per tenant (`sev0_<tenant>`), using FalkorDB multigraph.

## RCA v2: how the root cause is ranked

`score = 0.35·coverage + 0.20·frontier + 0.15·health + 0.20·recent_deploy + 0.10·history`

- **Coverage:** what fraction of alerting services depend on the candidate (multi-hop traversal).
- **Frontier:** the candidate is unhealthy while all of its own dependencies are healthy. This is where a failure starts; everything above it is a symptom.
- **Counterfactual check:** fixing the candidate would clear how many of the alerts.
- **Eval harness:** `scripts/run_evals.py` injects random faults into an isolated graph, with silent roots, noise alerts and decoy deploys, and reports top-1 and top-3 accuracy for naive vs v1 vs v2.

## Key Cypher

All queries are in `app/queries.py`, and the LLM never writes Cypher.

~~~cypher
// candidates: every shared downstream dependency of the alerting services
UNWIND $alerting AS svc
MATCH p = (s:Service {name: svc})-[:DEPENDS_ON*1..6]->(c:Service)
WITH c, collect(DISTINCT s.name) AS explains, min(length(p)) AS hops
RETURN c.name AS candidate, c.health AS health, explains, hops
~~~

~~~cypher
// failure frontier
MATCH (c:Service) WHERE c.health <> 'ok'
OPTIONAL MATCH (c)-[:DEPENDS_ON]->(d:Service) WHERE d.health <> 'ok'
WITH c, count(d) AS unhealthy_deps WHERE unhealthy_deps = 0
RETURN c.name AS service
~~~

~~~cypher
// counterfactual: which alerts clear if $service is fixed
UNWIND $alerting AS svc
MATCH (s:Service {name: svc})
OPTIONAL MATCH p = (s)-[:DEPENDS_ON*1..6]->(:Service {name: $service})
WITH s, count(p) AS paths WHERE paths > 0 OR s.name = $service
RETURN s.name AS service
~~~

~~~cypher
// topology discovery from spans (batched, one round trip per tick)
UNWIND range(0, size($src) - 1) AS i
WITH $src[i] AS s, $dst[i] AS t, $calls[i] AS c, $errs[i] AS e
MERGE (a:Service {name: s}) MERGE (b:Service {name: t})
MERGE (a)-[d:DEPENDS_ON]->(b)
ON CREATE SET d.first_seen = $now, d.source = 'otel'
SET d.calls = coalesce(d.calls, 0) + c, d.last_seen = $now
~~~

~~~cypher
// approval policy as a graph query
MATCH (a:Action {id: $action})-[:TARGETS]->(s:Service)
MATCH (p:Person {handle: $handle})-[:MEMBER_OF]->(t:Team)
OPTIONAL MATCH (t)-[o:OWNS]->(s)
WITH p, t, count(o) AS owns WHERE owns > 0 OR t.name = 'platform'
RETURN p.name AS approver
~~~

Also used: blast radius by reverse traversal, similar-incident recall, "who knows this system", the temporal topology (`TOPOLOGY_AT`, a UNION query), and `algo.pageRank('Service','DEPENDS_ON')`.

## Architecture

~~~
 spans ──> /api/ingest/spans ─┐        ┌─ detector thread (5 s): baselines → alerts, health, mitigation
 built-in simulator ──────────┴─> TelemetryStore ──> FalkorDB (topology upserts, temporal edges)
 deploy webhook (HMAC) ─────────────────────────────> FalkorDB
 UI ── SSE ──> investigator/planner → verifier → responder → scribe ──> FalkorDB (memory, actions, audit)
 /metrics (Prometheus) · /healthz · /readyz · JSON logs · tenant API keys · rate limits
~~~

## Run

~~~bash
cp .env.example .env              # add GROQ_API_KEY (optional)
docker compose up --build
~~~
Open http://localhost:8000 and wait about 40 seconds for "learning baseline…" to disappear. Then click **Bad pricing deploy**. Alerts appear within about 30 seconds; then click **Investigate**.

Without Docker for the app: `docker run -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest`, then `pip install -r requirements.txt` and `uvicorn app.main:app`.

| Command | What it does |
|---|---|
| `pytest -q` | Unit tests, plus integration tests when FalkorDB is reachable |
| `python scripts/run_evals.py --n 200` | RCA accuracy, naive vs v1 vs v2 |
| `python scripts/bench.py --services 5000` | Latency of RCA and blast radius on a synthetic large graph |

### Key API

| Method | Path | Purpose |
|---|---|---|
| POST | /api/ingest/spans | `{"spans":[{"service","span_id","parent_span_id" or "parent_service","duration_ms","error"}]}` |
| POST | /api/webhooks/deploy | `{"service","version","summary","author","risky"}`, header `X-Sev0-Signature: sha256=<hmac>` |
| GET | /api/investigate/stream | SSE stream of agent steps and the result |
| GET | /api/topology?at=<epoch> | Topology as it was at a point in time |
| POST | /api/actions/{id}/approve, /execute | Approval-gated remediation |
| POST | /api/evals/run?n=100 | Eval harness |
| GET | /metrics, /healthz, /readyz | Operations endpoints |

Auth: set `SEV0_API_KEYS=tenant:key,...` and send `X-API-Key`. The key decides the tenant, so customers can't cross into each other's graphs.

## Honest limits

The detector, simulator and API run in one process, so run a single uvicorn worker. Splitting ingest, detection and agents into separate services behind a queue is the next step. The span format is a simplified OTel shape. See `LEVELS.md`.

## AI disclosure

Built with help from an AI coding assistant (Claude by Anthropic), which drafted code and docs. I reviewed, ran, tested and changed it, and I can explain the graph model, queries and design decisions. *(Edit this to describe exactly what you changed.)*

## License

MIT
