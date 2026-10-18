# Structured JSON logs and a dependency-free Prometheus exporter.
import json
import logging
import sys
import threading
import time
from collections import defaultdict

log = logging.getLogger("sev0")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {"ts": round(time.time(), 3), "level": record.levelname,
                   "logger": record.name, "msg": record.getMessage()}
        payload.update(getattr(record, "fields", {}) or {})
        return json.dumps(payload, default=str)


def setup_logging(level="INFO"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def event(msg, **fields):
    log.info(msg, extra={"fields": fields})


class Metrics:
    BUCKETS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

    def __init__(self):
        self._lock = threading.Lock()
        self.counters = defaultdict(float)
        self.hist = defaultdict(lambda: [0] * (len(self.BUCKETS) + 1))
        self.sums = defaultdict(float)
        self.counts = defaultdict(int)

    @staticmethod
    def _key(name, labels):
        return name, tuple(sorted((k, str(v)) for k, v in labels.items()))

    def inc(self, name, value=1.0, **labels):
        with self._lock:
            self.counters[self._key(name, labels)] += value

    def observe(self, name, ms, **labels):
        key = self._key(name, labels)
        with self._lock:
            buckets = self.hist[key]
            for i, edge in enumerate(self.BUCKETS):
                if ms <= edge:
                    buckets[i] += 1
                    break
            else:
                buckets[-1] += 1
            self.sums[key] += ms
            self.counts[key] += 1

    def render(self):
        def fmt(labels):
            return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""

        lines = []
        with self._lock:
            for (name, labels), value in sorted(self.counters.items()):
                lines.append(f"{name}{fmt(labels)} {value}")
            for (name, labels), buckets in sorted(self.hist.items()):
                cumulative = 0
                for i, edge in enumerate(self.BUCKETS):
                    cumulative += buckets[i]
                    lines.append(f"{name}_bucket{fmt(labels + (('le', str(edge)),))} {cumulative}")
                cumulative += buckets[-1]
                lines.append(f"{name}_bucket{fmt(labels + (('le', '+Inf'),))} {cumulative}")
                lines.append(f"{name}_sum{fmt(labels)} {round(self.sums[(name, labels)], 3)}")
                lines.append(f"{name}_count{fmt(labels)} {self.counts[(name, labels)]}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
