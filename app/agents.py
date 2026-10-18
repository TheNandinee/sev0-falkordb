# Five roles share one graph:
#   investigator (Groq LLM with tools) or planner (deterministic fallback)
#   verifier:  re-ranks with graph-causal RCA v2 and can override the LLM
#   responder: proposes an approval-gated remediation action
#   scribe:    writes the status update and postmortem from verified facts
# Every step and handoff is written to FalkorDB as agent memory, and streamed to the UI.
import json
import time

import httpx

from . import graph_ops as G
from . import rca
from . import remediation
from .config import settings
from .db import start_trace, stop_trace
from .observability import event, metrics

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
STR = {"type": "string"}
STR_LIST = {"type": "array", "items": {"type": "string"}}


def _tool(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


TOOLS = [
    _tool("get_active_alerts", "List alerts that are firing right now and the services they fire on."),
    _tool("rank_root_causes",
          "Graph-causal RCA: walk DEPENDS_ON from the alerting services to every shared dependency and rank "
          "candidates by coverage, failure frontier, health, recent deploys and incident history.",
          {"alerting_services": STR_LIST}, ["alerting_services"]),
    _tool("explain_dependency_path", "Shortest dependency path from one service to another.",
          {"from_service": STR, "to_service": STR}, ["from_service", "to_service"]),
    _tool("what_if_fixed", "Counterfactual: which firing alerts would clear if this service were fixed.",
          {"service": STR}, ["service"]),
    _tool("get_recent_deploys", "Deploys to the given services in the recent lookback window.",
          {"services": STR_LIST}, ["services"]),
    _tool("get_blast_radius", "Every service that depends on this one, directly or transitively.",
          {"service": STR}, ["service"]),
    _tool("find_similar_incidents", "Past resolved incidents that touched the same services, and how they were fixed.",
          {"services": STR_LIST}, ["services"]),
    _tool("get_owners_and_experts", "Owning team, channel, who is on call, and who fixed this service before.",
          {"service": STR}, ["service"]),
    _tool("submit_diagnosis", "Submit the final diagnosis. Call exactly once, as the last step.", {
        "root_cause_service": STR, "confidence": {"type": "number"}, "summary": STR,
        "suggested_fix": STR, "suspect_deploy_id": STR, "page_handles": STR_LIST},
        ["root_cause_service", "confidence", "summary", "suggested_fix"]),
]

SYSTEM = (
    "You are Sev0, an autonomous on-call SRE agent. The production system is modelled as a graph in FalkorDB "
    "and you can only learn about it through your tools. Rules: "
    "1) Never invent services, deploys, people or incidents; use only names returned by tools. "
    "2) Start with get_active_alerts, then call rank_root_causes with the alerting services. "
    "3) Verify the top candidate: call what_if_fixed for it, explain_dependency_path from at least two alerting "
    "services to it, and get_recent_deploys for it. "
    "4) Call get_blast_radius, find_similar_incidents and get_owners_and_experts for the root cause. "
    "5) Finish by calling submit_diagnosis exactly once. Be decisive. The summary is 2-3 sentences that cite "
    "the dependency path, the counterfactual and any suspect deploy."
)

SCRIBE_SYSTEM = (
    "You are the incident scribe. Using only the facts in the JSON you are given, write two things. "
    "First a Slack status update under 90 words: severity, impact, likely cause, proposed action, owner. "
    "Then a line containing exactly ---POSTMORTEM--- followed by a short postmortem draft with the headings "
    "Summary, Impact, Root cause, Evidence, Action items. Do not invent facts."
)


def _as_list(v):
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def dispatch(tool, args, tenant):
    a = args or {}
    if tool == "get_active_alerts":
        return G.get_active_alerts(tenant)
    if tool == "rank_root_causes":
        return rca.rank_root_causes(_as_list(a.get("alerting_services")), tenant)
    if tool == "explain_dependency_path":
        return G.explain_dependency_path(str(a.get("from_service", "")), str(a.get("to_service", "")), tenant)
    if tool == "what_if_fixed":
        return rca.counterfactual(str(a.get("service", "")), G.get_active_alerts(tenant)["services"], tenant)
    if tool == "get_recent_deploys":
        return G.get_recent_deploys(_as_list(a.get("services")), None, tenant)
    if tool == "get_blast_radius":
        return G.get_blast_radius(str(a.get("service", "")), tenant)
    if tool == "find_similar_incidents":
        return G.find_similar_incidents(_as_list(a.get("services")), tenant)
    if tool == "get_owners_and_experts":
        return G.get_owners_and_experts(str(a.get("service", "")), tenant)
    if tool == "submit_diagnosis":
        return {"accepted": True, **a}
    return {"error": f"unknown tool {tool}"}


def summarize(tool, r):
    if not isinstance(r, dict):
        return str(r)[:160]
    if "error" in r:
        return "error: " + str(r["error"])[:160]
    if tool == "get_active_alerts":
        return f"{len(r['alerts'])} alerts firing on {', '.join(r['services']) or 'nothing'}"
    if tool == "rank_root_causes":
        c = r.get("candidates") or []
        if not c:
            return "no candidates"
        return f"[{r.get('model')}] top: " + ", ".join(f"{x['service']} ({x['score']})" for x in c[:3]) + \
            f" · margin {r.get('margin')}"
    if tool == "explain_dependency_path":
        return " → ".join(r.get("path") or []) or r.get("note", "no path")
    if tool == "what_if_fixed":
        total = len(r.get("would_clear") or []) + len(r.get("remaining") or [])
        return f"fixing {r.get('service')} would clear {len(r.get('would_clear') or [])}/{total} alerts"
    if tool == "get_recent_deploys":
        d = r.get("deploys") or []
        return ", ".join(f"{x['service']} {x['version']} ({x['minutes_ago']}m ago)" for x in d) or "no recent deploys"
    if tool == "get_blast_radius":
        return f"{len(r['impacted'])} services depend on {r['service']}, {r['tier1']} of them tier-1"
    if tool == "find_similar_incidents":
        return ", ".join(f"{x['id']}: {x['title']}" for x in r.get("incidents") or []) or "no similar incidents"
    if tool == "get_owners_and_experts":
        experts = ", ".join(e["person"] for e in r.get("experts") or [])
        return f"team {r.get('team')} {r.get('channel')}; on call {', '.join(r.get('on_call') or [])}; experts {experts or 'none'}"
    if tool == "submit_diagnosis":
        return f"diagnosis submitted: {r.get('root_cause_service')}"
    if tool == "propose_action":
        return f"{r.get('type')} {r.get('target')} proposed · risk {r.get('risk')} · needs {r.get('required')} approval(s)"
    if tool == "draft_comms":
        return f"status update and postmortem drafted ({r.get('by')})"
    return json.dumps(r, default=str)[:160]


class Investigation:
    def __init__(self, incident_id, tenant, on_step=None):
        self.incident_id = incident_id
        self.tenant = tenant
        self.on_step = on_step
        self.steps = []
        self.seq = 0

    def record(self, agent, tool, args, result, cypher=None):
        self.seq += 1
        step = {"seq": self.seq, "agent": agent, "tool": tool, "args": args,
                "summary": summarize(tool, result), "cypher": cypher or []}
        self.steps.append(step)
        metrics.inc("sev0_agent_steps_total", agent=agent, tool=tool)
        try:
            G.log_step(self.incident_id, self.seq, agent, tool, args, result, self.tenant)
        except Exception:
            pass
        if self.on_step:
            try:
                self.on_step(step)
            except Exception:
                pass

    def call(self, agent, tool, args):
        log = start_trace()
        try:
            result = dispatch(tool, args, self.tenant)
        except Exception as e:
            result = {"error": str(e)}
        finally:
            stop_trace()
        self.record(agent, tool, args, result, log)
        return result


def _groq(messages, tools=None, max_tokens=1200):
    body = {"model": settings.groq_model, "messages": messages, "temperature": 0.1, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    started = time.perf_counter()
    r = httpx.post(GROQ_URL, json=body, timeout=60,
                   headers={"Authorization": f"Bearer {settings.groq_api_key}"})
    metrics.observe("sev0_llm_ms", (time.perf_counter() - started) * 1000)
    if r.status_code >= 400:
        metrics.inc("sev0_llm_errors_total", status=str(r.status_code))
        raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]


def llm_investigate(inv):
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Alerts are firing. Investigate and submit a diagnosis."}]
    nudged = False
    for _ in range(settings.max_agent_steps):
        msg = _groq(messages, TOOLS)
        calls = msg.get("tool_calls") or []
        if not calls:
            if nudged:
                return None
            nudged = True
            messages.append({"role": "assistant", "content": msg.get("content") or ""})
            messages.append({"role": "user", "content": "Use your tools, then call submit_diagnosis."})
            continue
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}") or {}
            except json.JSONDecodeError:
                args = {}
            result = inv.call("investigator", name, args)
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, default=str)[:4000]})
            if name == "submit_diagnosis":
                return args
    return None


def planner_investigate(inv):
    alerts = inv.call("planner", "get_active_alerts", {})
    ranked = inv.call("planner", "rank_root_causes", {"alerting_services": alerts["services"]})
    if not ranked.get("candidates"):
        return None
    top = ranked["candidates"][0]["service"]
    inv.call("planner", "what_if_fixed", {"service": top})
    for svc in alerts["services"][:2]:
        inv.call("planner", "explain_dependency_path", {"from_service": svc, "to_service": top})
    inv.call("planner", "get_recent_deploys", {"services": [top]})
    inv.call("planner", "get_blast_radius", {"service": top})
    inv.call("planner", "find_similar_incidents", {"services": alerts["services"] + [top]})
    inv.call("planner", "get_owners_and_experts", {"service": top})
    return {"root_cause_service": top, "confidence": ranked["candidates"][0]["score"]}


def _template_summary(alerts, top, deploy, similar, cf):
    s = (f"{len(top['explains'])} of {len(alerts)} alerting services depend on {top['service']}, which is "
         f"{top.get('health')}" + (" while all of its own dependencies are healthy" if top.get("frontier") else "")
         + ".")
    if cf:
        s += f" Fixing it would clear {len(cf['would_clear'])}/{len(alerts)} alerts."
    if deploy:
        s += f" {deploy['version']} was deployed to it {deploy['minutes_ago']} min ago by {deploy['author']} ({deploy['summary']})."
    if similar:
        s += f" The pattern matches {similar[0]['id']}: {similar[0]['title']}."
    return s


def _template_fix(top, deploy, similar):
    if deploy:
        return f"Roll back {deploy['version']} on {top['service']}, freeze deploys, and watch 5xx and p99 for 10 minutes."
    if similar and similar[0].get("fix"):
        fix = f"Last time ({similar[0]['id']}): {similar[0]['fix']}."
        if similar[0].get("runbook"):
            fix += f" Runbook: {similar[0]['runbook']}."
        return fix
    return f"Escalate to the owning team and check {top['service']} saturation and error logs."


def finalize(inv, draft, alerts):
    ranked_result = inv.call("verifier", "rank_root_causes", {"alerting_services": alerts})
    ranked = ranked_result.get("candidates") or []
    if not ranked:
        raise RuntimeError("No root-cause candidates found in the graph.")
    names = [c["service"] for c in ranked]
    root = draft.get("root_cause_service")
    note = None
    if root not in names[:3]:
        note = (f"Verifier override: {root or 'no service'} is not a top graph-causal candidate for the "
                f"firing alerts, so {names[0]} is used.")
        root = names[0]
        draft = {"confidence": ranked[0]["score"]}
    top = next(c for c in ranked if c["service"] == root)

    paths = []
    for svc in alerts:
        p = G.explain_dependency_path(svc, root, inv.tenant)["path"]
        if len(p) > 1:
            paths.append(p)
    deploys = top.get("deploys") or []
    deploy = next((d for d in deploys if d["id"] == draft.get("suspect_deploy_id")), deploys[0] if deploys else None)
    blast = G.get_blast_radius(root, inv.tenant)
    owners = G.get_owners_and_experts(root, inv.tenant)
    similar = G.find_similar_incidents(alerts + [root], inv.tenant)["incidents"]
    cf = rca.counterfactual(root, alerts, inv.tenant)

    try:
        llm_conf = float(draft.get("confidence"))
    except (TypeError, ValueError):
        llm_conf = top["score"]
    # Confidence blends the LLM, the graph score, the ranking margin and the counterfactual.
    graph_conf = 0.6 * top["score"] + 0.2 * min(1.0, 4 * float(ranked_result.get("margin") or 0)) + 0.2 * cf["fraction"]
    confidence = round(min(max((llm_conf + graph_conf) / 2, 0.0), 0.99), 2)

    page = []
    for h in (owners.get("handles") or []) + [e["handle"] for e in owners.get("experts") or []][:1]:
        if h and h not in page:
            page.append(h)

    severity = "SEV1" if top.get("health") == "down" and blast["tier1"] >= 2 else "SEV2"
    diagnosis = {
        "root_cause_service": root, "kind": top.get("kind"), "health": top.get("health"),
        "confidence": confidence, "frontier": top.get("frontier"),
        "summary": draft.get("summary") or _template_summary(alerts, top, deploy, similar, cf),
        "suggested_fix": draft.get("suggested_fix") or _template_fix(top, deploy, similar),
        "suspect_deploy": deploy, "paths": paths, "page": page,
        "team": owners.get("team"), "channel": owners.get("channel"),
        "experts": owners.get("experts"), "similar": similar[:2],
        "evidence": top["evidence"], "verifier_note": note,
    }
    return {"diagnosis": diagnosis, "ranked": ranked[:3], "blast": blast, "severity": severity,
            "counterfactual": cf, "model": ranked_result.get("model")}


def scribe(inv, result):
    d = result["diagnosis"]
    G.log_handoff(inv.incident_id, "responder", "scribe", "diagnosis and action ready", inv.tenant)
    action = result.get("action") or {}
    facts = {
        "incident": inv.incident_id, "severity": result["severity"], "root_cause": d["root_cause_service"],
        "health": d["health"], "summary": d["summary"], "fix": d["suggested_fix"],
        "suspect_deploy": d["suspect_deploy"], "dependency_paths": [" -> ".join(p) for p in d["paths"]],
        "counterfactual": result.get("counterfactual"),
        "proposed_action": {k: action.get(k) for k in ("type", "target", "risk", "required")},
        "impacted_services": [x["service"] for x in result["blast"]["impacted"]],
        "tier1_impacted": result["blast"]["tier1"], "team": d["team"], "channel": d["channel"],
        "paging": d["page"], "similar_incident": d["similar"][0]["id"] if d["similar"] else None,
    }
    comms = None
    if settings.groq_api_key:
        try:
            msg = _groq([{"role": "system", "content": SCRIBE_SYSTEM},
                         {"role": "user", "content": json.dumps(facts, default=str)}], max_tokens=700)
            text = msg.get("content") or ""
            if "---POSTMORTEM---" in text:
                slack, pm = text.split("---POSTMORTEM---", 1)
                comms = {"slack_update": slack.strip(), "postmortem": pm.strip(), "by": "groq"}
        except Exception:
            comms = None
    if comms is None:
        dep = d["suspect_deploy"]
        slack = (f"[{result['severity']}] {inv.incident_id}: {len(facts['impacted_services'])} services impacted "
                 f"({facts['tier1_impacted']} tier-1). Likely cause: {d['root_cause_service']} is {d['health']}"
                 + (f" after deploy {dep['version']} by {dep['author']}" if dep else "") + ". "
                 + (f"Proposed: {action.get('type')} {action.get('target')} (needs {action.get('required')} approval). " if action else "")
                 + f"Owner: {d['team']} {d['channel']}, paging " + ", ".join("@" + h for h in d["page"]) + ".")
        pm = (f"Summary\n{d['summary']}\n\nImpact\n" + ", ".join(facts["impacted_services"])
              + f"\n\nRoot cause\n{d['root_cause_service']} ({d['health']})"
              + "\n\nEvidence\n" + "\n".join(facts["dependency_paths"])
              + f"\n\nAction items\n1. {d['suggested_fix']}\n2. Add a canary gate for {d['root_cause_service']} deploys."
              + "\n3. Review alert coverage on the root-cause service.")
        comms = {"slack_update": slack, "postmortem": pm, "by": "template"}
    inv.record("scribe", "draft_comms", {"handoff_from": "responder"}, comms)
    return comms


def run_investigation(tenant=None, on_step=None):
    started = time.perf_counter()
    alerts = G.get_active_alerts(tenant)
    if not alerts["services"]:
        return {"error": "No alerts are firing yet. Trigger a scenario (live mode: wait ~20 s for detection)."}
    inc_id = "INC-" + time.strftime("%y%m%d-%H%M%S")
    services = alerts["services"]
    title = "Alerts on " + ", ".join(services[:3]) + ("…" if len(services) > 3 else "")
    G.open_incident(inc_id, title, tenant)
    inv = Investigation(inc_id, tenant, on_step)

    mode, label, note, draft = "planner (no LLM key)", "planner", None, None
    if settings.groq_api_key:
        mode, label = f"groq:{settings.groq_model}", "groq"
        try:
            draft = llm_investigate(inv)
            if draft is None:
                note = "LLM did not submit a diagnosis; the deterministic planner took over."
        except Exception as e:
            note = f"LLM unavailable ({str(e)[:120]}); the deterministic planner took over."
    if draft is None:
        if settings.groq_api_key:
            mode, label = mode + " → planner", "groq_fallback"
        draft = planner_investigate(inv) or {}

    result = finalize(inv, draft, services)
    G.log_handoff(inc_id, "investigator", "responder", "verified diagnosis", tenant)
    action = remediation.propose(inc_id, result["diagnosis"], result["blast"], tenant)
    inv.record("responder", "propose_action", {"target": action["target"]}, action)
    result["action"] = action
    comms = scribe(inv, result)
    G.conclude_incident(inc_id, result, services, tenant)

    ms = (time.perf_counter() - started) * 1000
    metrics.inc("sev0_investigations_total", mode=label)
    metrics.observe("sev0_investigation_ms", ms)
    event("investigation.done", incident=inc_id, root=result["diagnosis"]["root_cause_service"],
          confidence=result["diagnosis"]["confidence"], mode=label, ms=round(ms))
    return {"incident_id": inc_id, "title": title, "mode": mode, "note": note, "duration_ms": round(ms),
            "steps": inv.steps, **result, "comms": comms}
