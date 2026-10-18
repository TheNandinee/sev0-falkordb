# CONTEXT: every file, and why it exists

## Infra
| File | Why |
|---|---|
| `docker-compose.yml` | FalkorDB plus the app with restart policies. One command to run. |
| `Dockerfile` | Slim image running as a non-root user, with a HEALTHCHECK on /healthz. |
| `.env.example` | Every setting documented. Secrets stay out of git. |
| `.github/workflows/ci.yml` | Runs unit and integration tests against a real FalkorDB service container on every push. |

## app/
| File | What | Key decisions |
|---|---|---|
| `config.py` | Settings from the environment | One frozen object; no magic constants spread around. |
| `db.py` | FalkorDB client, per-tenant graphs, `run()`, Cypher trace, query metrics | Every query is parameterised and timed; the trace powers the UI's "show me the Cypher". |
| `queries.py` | Every Cypher query | The LLM never writes Cypher; the whole graph surface can be audited in one file. |
| `graph_ops.py` | Tools, memory writes, v1 heuristic | v1 is kept as a baseline for the eval ablation. |
| `rca.py` | RCA v2: candidates, frontier, scoring, counterfactual | `score()` is pure and unit tested. The frontier is the causal signal: a failure starts at the deepest unhealthy node whose dependencies are healthy. |
| `telemetry.py` | Span ingestion, topology discovery, anomaly detector | Metrics stay in memory (a TSDB's job); topology, health and alerts go to the graph, with one batched upsert per tick. EWMA baselines, a warm-up period, and automatic mitigation of incidents. |
| `simulator.py` | Traffic generator with fault injection | Faults propagate up critical edges with probability, which is realistic. It stands in for an OTel collector so the demo is live. |
| `remediation.py` | Actions, approval policy, execution | Policy is a graph query; two-person rule for high risk; every approval is an edge, which gives an audit trail. |
| `evals.py` | Fault-injection evaluation | Isolated tenant graph; silent roots, noise and decoy deploys; naive vs v1 vs v2. This is how you prove the RCA works rather than claim it. |
| `agents.py` | Investigator, planner, verifier, responder, scribe | The graph decides and the LLM explains. Steps stream to the UI and persist as AgentStep nodes; handoffs are nodes. |
| `security.py` | Tenant API keys, rate limiter, HMAC verification | Constant-time key comparison. The key determines the tenant, so there is no cross-tenant access. |
| `observability.py` | JSON logs and a Prometheus exporter | No extra dependencies. Histograms for HTTP, Cypher, LLM and investigation latency. |
| `main.py` | FastAPI app: routes, SSE, ASGI access-log middleware, lifespan | Pure ASGI middleware so streaming works; background threads start and stop with the app. |
| `seed.py` | Synthetic company and scripted scenarios | Used for scripted mode, evals and tests. All data is fictional. |

## Other
| File | Why |
|---|---|
| `static/index.html` | Live UI: telemetry status, emerging alerts, streamed trace, diagnosis with counterfactual, approve and execute, eval table. |
| `scripts/run_evals.py` | CLI for evals. Put its output in your README and video. |
| `scripts/bench.py` | Scale benchmark on synthetic graphs of thousands of services. |
| `tests/` | Pure scoring tests, plus integration tests (scenarios, counterfactual, approval policy, eval floor). |

## Judge questions, with answers
1. **Why a graph?** Root cause analysis means intersecting multi-hop dependency closures from many alerting services, finding the unhealthy frontier, and reverse-traversing the blast radius. Each of these is one Cypher query, and none of them work without FalkorDB.
2. **Is it really causal?** It is structural causality: failures propagate up dependency edges, so the root is the deepest unhealthy node that explains the symptoms. The counterfactual checks it. It is not learned causal inference; that is Level 8 and above in LEVELS.md.
3. **How do you know it works?** The eval harness: N injected faults with adversarial noise, measured against two baselines.
4. **What stops the AI breaking production?** The LLM can't write Cypher; the verifier can override it; actions need policy-checked human approval, two people for high risk, with an audit trail.
5. **How does it scale?** Batched upserts, indexed lookups, depth-capped traversals, per-tenant graphs; `scripts/bench.py` measures it. Known limits: path enumeration cost on very dense graphs, and the single-process detector. LEVELS.md gives the fix.
