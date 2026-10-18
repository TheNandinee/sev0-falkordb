# Build levels: where Sev0 sits (honestly)

| Level | What it means | Sev0 v2 |
|---|---|---|
| 1 | Chatbot with the architecture pasted into the prompt | — |
| 2 | RAG over runbooks | — |
| 3 | Graph used as decoration | — |
| 4 | LLM writes its own Cypher | — (rejected on purpose) |
| 5 | Curated graph tools, multi-hop traversals, evidence paths | ✅ |
| 6 | Graph-native agent memory, verifier over the LLM, works without the LLM, multi-tenant | ✅ |
| 7 | Live topology from traces, temporal graph, deploy webhooks, real-time anomaly detection | ✅ (simplified span format; a native OTel exporter is next) |
| 8 | Causal RCA, counterfactuals, rigorous evaluation | ◐ structural-causal frontier, counterfactuals, fault-injection evals with ablation. Missing: learned causal models and evaluation on real incident history. |
| 9 | Safe autonomous remediation, policy, audit, enterprise security | ◐ policy-as-graph approvals, two-person rule, audit edges, tenant API keys, rate limits, signed webhooks, metrics and probes, CI. Missing: SSO/RBAC, real executors (Argo, Kubernetes), SOC 2. |
| 10 | Fleet scale and a data moat | ✗ (see below) |

## What Level 10 would actually take
1. Split ingest, detection and agents into separate services connected by a stream such as Kafka, with horizontally scaled detectors partitioned by tenant.
2. Precomputed, incrementally maintained reachability, so RCA is a lookup instead of path enumeration on dense graphs with 100k or more nodes.
3. Sharded tenant graphs across FalkorDB clusters, with replicas for reads.
4. Learned priors across customers with privacy guarantees, and causal discovery from metric time series.
5. Real integrations: OTel, Prometheus, Kubernetes, Argo, PagerDuty and Slack, plus SOC 2 and SSO.

That is years of work for a funded team. Sev0 v2 proves the core thesis, that a graph plus agents produces verifiable incident response, at Level 8.
