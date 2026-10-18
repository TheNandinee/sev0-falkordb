# Built-in traffic generator: produces realistic distributed traces from the service topology,
# with fault injection that propagates up critical dependencies. Stands in for an OTel collector.
import random
import threading
import time
from collections import defaultdict

from . import seed as S
from .config import settings
from .observability import event
from .telemetry import hub

BASE_P99 = {name: p99 for name, _kind, _tier, p99 in S.SERVICES}
CHILDREN = defaultdict(list)
for _a, _b, _critical in S.DEPENDENCIES:
    CHILDREN[_a].append((_b, _critical))
ENTRYPOINTS = [("web-frontend", 0.4), ("mobile-bff", 0.4), ("notification-service", 0.1), ("delivery-service", 0.1)]

SCENARIO_FAULTS = {
    "pricing-deploy": {"pricing-service": {"error": 0.45, "latency": 2500}},
    "psp-outage": {"psp-gateway": {"error": 0.6, "latency": 8000}},
    "redis-meltdown": {"redis-cache": {"error": 0.7, "latency": 1500}},
}


class Simulator:
    def __init__(self, tenant=None, rps=10):
        self.tenant = tenant
        self.rps = max(1, rps)
        self.faults = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.rng = random.Random()

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set())

    def start(self):
        if self.running:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="sev0-simulator")
        self.thread.start()
        event("simulator.started", rps=self.rps)

    def stop(self):
        self.stop_event.set()
        event("simulator.stopped")

    def inject(self, scenario):
        now = int(time.time())
        for service, version, summary, author, minutes_ago, risky in S.SCENARIOS[scenario]["deploys"]:
            S.create_deploy(f"DEP-{service}-{now}", service, version, summary, author, minutes_ago, risky,
                            now=now, tenant=self.tenant)
        with self.lock:
            self.faults = {k: dict(v) for k, v in SCENARIO_FAULTS[scenario].items()}
        event("simulator.fault_injected", scenario=scenario, faults=list(self.faults))
        return {"scenario": scenario, "label": S.SCENARIOS[scenario]["label"], "faults": list(self.faults)}

    def clear(self, service=None):
        with self.lock:
            if service:
                self.faults.pop(service, None)
            else:
                self.faults = {}

    def status(self):
        with self.lock:
            faults = dict(self.faults)
        return {"running": self.running, "rps": self.rps, "faults": faults}

    def _call(self, service, parent_id, spans, depth, faults):
        rng = self.rng
        span_id = "%016x" % rng.getrandbits(64)
        fault = faults.get(service)
        latency = BASE_P99.get(service, 50) * rng.uniform(0.3, 0.8)
        error = rng.random() < 0.002
        if fault:
            latency += fault["latency"] * rng.uniform(0.6, 1.2)
            error = error or rng.random() < fault["error"]
        slowest = 0.0
        if depth < 8:
            for child, critical in CHILDREN.get(service, []):
                if not critical and rng.random() < 0.5:
                    continue
                child_err, child_lat = self._call(child, span_id, spans, depth + 1, faults)
                slowest = max(slowest, child_lat)
                if child_err and critical and rng.random() < 0.85:
                    error = True
        total = latency + slowest
        spans.append({"span_id": span_id, "parent_span_id": parent_id, "service": service,
                      "duration_ms": round(total, 1), "error": error})
        return error, total

    def _loop(self):
        store = hub.store(self.tenant)
        names = [e[0] for e in ENTRYPOINTS]
        weights = [e[1] for e in ENTRYPOINTS]
        while not self.stop_event.is_set():
            started = time.time()
            with self.lock:
                faults = dict(self.faults)
            spans = []
            try:
                for _ in range(self.rps):
                    self._call(self.rng.choices(names, weights)[0], None, spans, 0, faults)
                store.ingest(spans)
            except Exception as e:
                event("simulator.error", error=str(e))
            self.stop_event.wait(max(0.0, 1.0 - (time.time() - started)))


simulator = Simulator(rps=settings.simulator_rps)
