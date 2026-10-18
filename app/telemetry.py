# Live telemetry: span ingestion -> topology discovery (temporal edges) -> anomaly detection -> alerts.
# Metrics stay in memory (like a small TSDB); topology, health and alerts are written to the graph.
import threading
import time
from collections import defaultdict, deque

from . import queries as Q
from .config import settings
from .db import run
from .observability import event, metrics

WINDOW_BUCKETS = 6        # detection window = 6 x interval (30 s by default)
MIN_SAMPLES = 20
HISTORY_BUCKETS = 120


def _p99(values):
    if not values:
        return 0.0
    s = sorted(values)
    return s[int(0.99 * (len(s) - 1))]


class TelemetryStore:
    def __init__(self, tenant):
        self.tenant = tenant
        self.lock = threading.Lock()
        self.current = defaultdict(lambda: {"count": 0, "errors": 0, "lat": []})
        self.history = defaultdict(lambda: deque(maxlen=HISTORY_BUCKETS))
        self.edges = defaultdict(lambda: [0, 0])
        self.baseline = {}
        self.anomalous = {}
        self.spans_total = 0
        self._last_total = 0
        self.spans_per_s = 0.0

    def ingest(self, spans):
        index = {s.get("span_id"): s for s in spans if s.get("span_id")}
        with self.lock:
            for s in spans:
                svc = s.get("service")
                if not svc:
                    continue
                st = self.current[svc]
                err = bool(s.get("error"))
                st["count"] += 1
                st["errors"] += int(err)
                if len(st["lat"]) < 5000:
                    st["lat"].append(float(s.get("duration_ms") or 0))
                parent_svc = s.get("parent_service")
                if not parent_svc:
                    parent = index.get(s.get("parent_span_id"))
                    parent_svc = parent.get("service") if parent else None
                if parent_svc and parent_svc != svc:
                    edge = self.edges[(parent_svc, svc)]
                    edge[0] += 1
                    edge[1] += int(err)
            self.spans_total += len(spans)
        metrics.inc("sev0_spans_ingested_total", len(spans))
        return len(spans)

    def flush(self, interval):
        now = int(time.time())
        with self.lock:
            closed = {svc: {"count": st["count"], "errors": st["errors"], "p99": _p99(st["lat"])}
                      for svc, st in self.current.items()}
            self.current.clear()
            edges = dict(self.edges)
            self.edges.clear()
            for svc, bucket in closed.items():
                self.history[svc].append(bucket)
            self.spans_per_s = round((self.spans_total - self._last_total) / max(interval, 1), 1)
            self._last_total = self.spans_total
        if edges:
            keys = list(edges)
            run(Q.UPSERT_OBSERVED_EDGES, {
                "src": [k[0] for k in keys], "dst": [k[1] for k in keys],
                "calls": [edges[k][0] for k in keys], "errs": [edges[k][1] for k in keys], "now": now,
            }, tenant=self.tenant)
        self._detect(now)

    def _detect(self, now):
        names, health, err_pct, p99s = [], [], [], []
        for svc, hist in list(self.history.items()):
            recent = list(hist)[-WINDOW_BUCKETS:]
            count = sum(b["count"] for b in recent)
            if count < MIN_SAMPLES:
                continue
            err = sum(b["errors"] for b in recent) / count
            p99 = max(b["p99"] for b in recent)
            base = self.baseline.get(svc)
            if base is None or base["n"] < WINDOW_BUCKETS:
                if base is None:
                    base = self.baseline[svc] = {"err": err, "p99": p99, "n": 0}
                base["n"] += 1
                base["err"] = 0.7 * base["err"] + 0.3 * err
                base["p99"] = 0.7 * base["p99"] + 0.3 * p99
                names.append(svc); health.append("ok"); err_pct.append(round(err * 100, 2)); p99s.append(int(p99))
                continue
            err_anom = err > max(0.05, 4 * base["err"] + 0.01)
            lat_anom = p99 > max(3 * base["p99"], base["p99"] + 250)
            anomalous = err_anom or lat_anom
            if not anomalous:
                base["err"] = 0.9 * base["err"] + 0.1 * err
                base["p99"] = 0.9 * base["p99"] + 0.1 * p99
            state = "down" if err >= 0.3 else ("degraded" if anomalous else "ok")
            names.append(svc); health.append(state); err_pct.append(round(err * 100, 2)); p99s.append(int(p99))
            if anomalous and svc not in self.anomalous:
                metric, value = ("error_rate_pct", round(err * 100, 1)) if err_anom else ("p99_latency_ms", int(p99))
                self.anomalous[svc] = now
                run(Q.FIRE_DETECTED_ALERT, {"service": svc, "id": f"AL-{svc}-{now}", "metric": metric,
                                            "value": value, "at": now}, tenant=self.tenant)
                metrics.inc("sev0_alerts_fired_total")
                event("alert.fired", tenant=self.tenant, service=svc, metric=metric, value=value)
            elif not anomalous and svc in self.anomalous:
                del self.anomalous[svc]
                run(Q.CLEAR_SERVICE_ALERTS, {"service": svc, "at": now}, tenant=self.tenant)
                metrics.inc("sev0_alerts_cleared_total")
                event("alert.cleared", tenant=self.tenant, service=svc)
        if names:
            run(Q.UPDATE_HEALTH, {"names": names, "health": health, "err": err_pct, "p99": p99s}, tenant=self.tenant)
        for row in run(Q.MITIGATE_INCIDENTS, {"now": now}, tenant=self.tenant):
            event("incident.mitigated", tenant=self.tenant, incident=row["id"])

    def status(self):
        ready = sum(1 for b in self.baseline.values() if b["n"] >= WINDOW_BUCKETS)
        return {"spans_total": self.spans_total, "spans_per_s": self.spans_per_s,
                "anomalous": sorted(self.anomalous), "baseline_ready": ready >= 10}


class TelemetryHub:
    def __init__(self):
        self.stores = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self._last_expire = 0.0

    def store(self, tenant=None):
        key = tenant or settings.default_tenant
        with self.lock:
            if key not in self.stores:
                self.stores[key] = TelemetryStore(tenant)
            return self.stores[key]

    def status(self, tenant=None):
        st = self.stores.get(tenant or settings.default_tenant)
        return st.status() if st else {"spans_total": 0, "spans_per_s": 0, "anomalous": [], "baseline_ready": False}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="sev0-detector")
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        interval = settings.detector_interval_s
        while not self.stop_event.wait(interval):
            for st in list(self.stores.values()):
                try:
                    st.flush(interval)
                except Exception as e:
                    event("detector.error", tenant=st.tenant, error=str(e))
            if time.time() - self._last_expire > 60:
                self._last_expire = time.time()
                now = int(time.time())
                for st in list(self.stores.values()):
                    try:
                        run(Q.EXPIRE_STALE_EDGES, {"cutoff": now - settings.edge_ttl_s, "now": now}, tenant=st.tenant)
                    except Exception as e:
                        event("topology.expire_error", tenant=st.tenant, error=str(e))


hub = TelemetryHub()
