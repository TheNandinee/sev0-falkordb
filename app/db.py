# Thin FalkorDB layer: one graph per tenant, parameterised queries, rows as dicts,
# per-request Cypher trace (shown in the UI) and query metrics.
import contextvars
import re
import time

from falkordb import FalkorDB

from .config import settings
from .observability import metrics

_client = None
_trace = contextvars.ContextVar("cypher_trace", default=None)


def client():
    global _client
    if _client is None:
        _client = FalkorDB(host=settings.falkor_host, port=settings.falkor_port,
                           password=settings.falkor_password or None)
    return _client


def ping():
    conn = getattr(client(), "connection", None)
    if conn is not None:
        return conn.ping()
    return run("RETURN 1 AS ok")


def graph_name(tenant=None):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", tenant or settings.default_tenant)
    return f"{settings.graph_prefix}{safe or settings.default_tenant}"


def graph(tenant=None):
    return client().select_graph(graph_name(tenant))


def start_trace():
    log = []
    _trace.set(log)
    return log


def stop_trace():
    _trace.set(None)


def _clean(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        return value.replace("\\", "/").replace('"', "'").replace("\n", " ").replace("\r", " ")
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _plain(value):
    if hasattr(value, "labels") and hasattr(value, "properties"):
        return dict(value.properties)
    if hasattr(value, "relation") and hasattr(value, "properties"):
        return dict(value.properties)
    if hasattr(value, "nodes") and callable(getattr(value, "nodes")):
        return [_plain(n) for n in value.nodes()]
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, bytes):
        return value.decode()
    return value


def run(cypher, params=None, tenant=None, readonly=False):
    g = graph(tenant)
    p = {k: _clean(v) for k, v in (params or {}).items()}
    started = time.perf_counter()
    try:
        res = g.ro_query(cypher, p or None) if readonly else g.query(cypher, p or None)
    except Exception:
        metrics.inc("sev0_cypher_errors_total")
        raise
    ms = (time.perf_counter() - started) * 1000
    metrics.inc("sev0_cypher_queries_total", mode="ro" if readonly else "rw")
    metrics.observe("sev0_cypher_ms", ms)
    cols = []
    for h in res.header or []:
        name = h[1] if isinstance(h, (list, tuple)) and len(h) > 1 else h
        cols.append(name.decode() if isinstance(name, bytes) else str(name))
    rows = [dict(zip(cols, [_plain(v) for v in row])) for row in (res.result_set or [])]
    log = _trace.get()
    if log is not None:
        log.append({"cypher": " ".join(cypher.split()), "params": p, "rows": len(rows), "ms": round(ms, 1)})
    return rows
