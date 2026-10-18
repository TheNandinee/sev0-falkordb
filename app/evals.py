# Offline RCA evaluation: inject random faults into an isolated tenant graph,
# derive the alert set by propagating through the real topology (with noise, silent roots
# and decoy deploys), then measure top-1 / top-3 accuracy of naive vs v1 vs v2.
import random
import time
from collections import defaultdict

from . import graph_ops as G
from . import queries as Q
from . import rca
from . import seed as S
from .db import run
from .observability import event

EVAL_TENANT = "eval"
SERVICES = [s[0] for s in S.SERVICES]
PARENTS = defaultdict(list)
for _a, _b, _critical in S.DEPENDENCIES:
    PARENTS[_b].append((_a, _critical))


def impacted_by(fault, rng):
    seen, stack = set(), [fault]
    while stack:
        cur = stack.pop()
        for parent, critical in PARENTS.get(cur, []):
            if parent in seen:
                continue
            if critical or rng.random() < 0.5:
                seen.add(parent)
                stack.append(parent)
    seen.discard(fault)
    return seen


def _pct(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1)))], 1)


def run_evals(n=100, seed_value=7, noise=0.3, silent_root=0.3, tenant=EVAL_TENANT):
    rng = random.Random(seed_value)
    S.seed(tenant)
    hits = {m: {"top1": 0, "top3": 0} for m in ("naive", "v1", "v2")}
    latency = {"v1": [], "v2": []}
    failures = []
    for k in range(n):
        fault = rng.choice(SERVICES)
        impacted = impacted_by(fault, rng)
        alerting = set(impacted)
        silent = bool(impacted) and rng.random() < silent_root
        if not silent:
            alerting.add(fault)
        others = [s for s in SERVICES if s not in impacted and s != fault]
        noise_svc = rng.choice(others) if others and rng.random() < noise else None
        if noise_svc:
            alerting.add(noise_svc)
        err = {fault: rng.uniform(25, 60)}
        for s in impacted:
            err[s] = rng.uniform(5, 45)
        if noise_svc:
            err[noise_svc] = rng.uniform(5, 20)
        names = sorted(err)
        run(Q.RESET_HEALTH, tenant=tenant)
        run(Q.UPDATE_HEALTH, {
            "names": names,
            "health": ["down" if (s == fault or err[s] >= 30) else "degraded" for s in names],
            "err": [round(err[s], 2) for s in names], "p99": [1000 for _ in names],
        }, tenant=tenant)
        now = int(time.time())
        if rng.random() < 0.4:
            S.create_deploy(f"EVAL-{k}-true", fault, "v9.9.9", "eval deploy", "neel", 10, 1, now=now, tenant=tenant)
        if rng.random() < 0.4:
            S.create_deploy(f"EVAL-{k}-decoy", rng.choice(SERVICES), "v0.0.1", "decoy deploy", "dev", 10, 0,
                            now=now, tenant=tenant)
        alert_list = sorted(alerting)
        t0 = time.perf_counter()
        v1 = [c["service"] for c in G.rank_root_causes(alert_list, tenant)["candidates"]]
        t1 = time.perf_counter()
        v2 = [c["service"] for c in rca.rank_root_causes(alert_list, tenant)["candidates"]]
        t2 = time.perf_counter()
        latency["v1"].append((t1 - t0) * 1000)
        latency["v2"].append((t2 - t1) * 1000)
        naive = sorted(alert_list, key=lambda s: -err.get(s, 0))
        for name, ranking in (("naive", naive), ("v1", v1), ("v2", v2)):
            if ranking[:1] == [fault]:
                hits[name]["top1"] += 1
            if fault in ranking[:3]:
                hits[name]["top3"] += 1
        if v2[:1] != [fault] and len(failures) < 10:
            failures.append({"fault": fault, "silent": silent, "noise": noise_svc, "v2_top3": v2[:3]})
        run(Q.AGE_EVAL_DEPLOYS, tenant=tenant)
    summary = {m: {"top1": round(h["top1"] / n, 3), "top3": round(h["top3"] / n, 3)} for m, h in hits.items()}
    summary["latency_ms"] = {m: {"p50": _pct(v, 0.5), "p95": _pct(v, 0.95)} for m, v in latency.items()}
    summary.update({"n": n, "seed": seed_value, "noise": noise, "silent_root": silent_root, "failures_v2": failures})
    event("evals.done", n=n, v2_top1=summary["v2"]["top1"], v2_top3=summary["v2"]["top3"])
    return summary
