# Scale benchmark: builds a synthetic layered service graph and times RCA + blast radius.
# Usage: python scripts/bench.py --services 5000 --fanout 3 --faults 50
import argparse
import pathlib
import random
import sys
import time
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import graph_ops as G  # noqa: E402
from app import queries as Q  # noqa: E402
from app import rca  # noqa: E402
from app.db import graph, run  # noqa: E402

TENANT = "bench"
BATCH = 2000


def pct(values, q):
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * (len(s) - 1)))], 1) if s else 0.0


def build(n, layers, fanout, rng):
    try:
        graph(TENANT).delete()
    except Exception:
        pass
    names = [f"svc-{i:06d}" for i in range(n)]
    layer_of = {name: min(layers - 1, i * layers // n) for i, name in enumerate(names)}
    for i in range(0, n, BATCH):
        chunk = names[i:i + BATCH]
        run(Q.BULK_CREATE_SERVICES, {"names": chunk, "tiers": [1 + layer_of[x] % 3 for x in chunk]}, tenant=TENANT)
    try:
        run("CREATE INDEX FOR (s:Service) ON (s.name)", tenant=TENANT)
    except Exception:
        pass
    by_layer = defaultdict(list)
    for name in names:
        by_layer[layer_of[name]].append(name)
    src, dst = [], []
    parents = defaultdict(list)
    for name in names:
        layer = layer_of[name]
        if layer >= layers - 1:
            continue
        targets = set()
        for _ in range(rng.randint(1, fanout)):
            target_layer = rng.randint(layer + 1, min(layers - 1, layer + 2))
            targets.add(rng.choice(by_layer[target_layer]))
        for t in targets:
            src.append(name); dst.append(t); parents[t].append(name)
    for i in range(0, len(src), BATCH):
        run(Q.BULK_CREATE_EDGES, {"src": src[i:i + BATCH], "dst": dst[i:i + BATCH]}, tenant=TENANT)
    return names, by_layer, parents, len(src)


def ancestors(node, parents, max_hops=6, cap=200):
    seen, frontier = set(), [node]
    for _ in range(max_hops):
        nxt = []
        for cur in frontier:
            for p in parents.get(cur, []):
                if p not in seen:
                    seen.add(p); nxt.append(p)
        frontier = nxt
        if len(seen) >= cap:
            break
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--services", type=int, default=2000)
    ap.add_argument("--layers", type=int, default=8)
    ap.add_argument("--fanout", type=int, default=3)
    ap.add_argument("--faults", type=int, default=30)
    ap.add_argument("--alerts", type=int, default=15)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    t0 = time.perf_counter()
    names, by_layer, parents, n_edges = build(args.services, args.layers, args.fanout, rng)
    print(f"built {len(names)} services, {n_edges} edges in {time.perf_counter() - t0:.1f}s")
    rank_ms, blast_ms, hits = [], [], 0
    deep = by_layer[args.layers - 1] + by_layer[args.layers - 2]
    for _ in range(args.faults):
        fault = rng.choice(deep)
        anc = list(ancestors(fault, parents))
        alerting = rng.sample(anc, min(args.alerts, len(anc))) + [fault]
        run("MATCH (s:Service {name: $n}) SET s.health = 'down'", {"n": fault}, tenant=TENANT)
        t = time.perf_counter()
        ranked = rca.rank_root_causes(alerting, TENANT)["candidates"]
        rank_ms.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        G.get_blast_radius(fault, TENANT)
        blast_ms.append((time.perf_counter() - t) * 1000)
        hits += int(bool(ranked) and ranked[0]["service"] == fault)
        run("MATCH (s:Service {name: $n}) SET s.health = 'ok'", {"n": fault}, tenant=TENANT)
    print(f"RCA v2   p50 {pct(rank_ms, .5)} ms  p95 {pct(rank_ms, .95)} ms  max {round(max(rank_ms), 1)} ms")
    print(f"blast    p50 {pct(blast_ms, .5)} ms  p95 {pct(blast_ms, .95)} ms  max {round(max(blast_ms), 1)} ms")
    print(f"top-1 on synthetic faults: {hits}/{args.faults}")
    print("Note: variable-length traversal cost grows with fan-out; see LEVELS.md for the Level 10 path.")


if __name__ == "__main__":
    main()
