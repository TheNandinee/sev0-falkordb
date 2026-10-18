# Usage: python scripts/run_evals.py --n 200 --seed 7
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.evals import run_evals  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--noise", type=float, default=0.3)
    ap.add_argument("--silent", type=float, default=0.3)
    args = ap.parse_args()
    r = run_evals(n=args.n, seed_value=args.seed, noise=args.noise, silent_root=args.silent)
    print(f"\nRCA eval: {r['n']} injected faults, seed {r['seed']}, noise {r['noise']}, silent roots {r['silent_root']}\n")
    print(f"{'method':<28}{'top-1':>8}{'top-3':>8}")
    for name, label in (("naive", "naive (highest error rate)"), ("v1", "v1 heuristic"), ("v2", "v2 graph-causal")):
        print(f"{label:<28}{r[name]['top1'] * 100:>7.1f}%{r[name]['top3'] * 100:>7.1f}%")
    print(f"\nlatency v2: p50 {r['latency_ms']['v2']['p50']} ms, p95 {r['latency_ms']['v2']['p95']} ms")
    if r["failures_v2"]:
        print("\nsample v2 misses:")
        print(json.dumps(r["failures_v2"][:5], indent=2))


if __name__ == "__main__":
    main()
