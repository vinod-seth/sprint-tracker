"""
planner.py — AI project planner for the Sprint Tracker.

Endpoints (registered as a Flask blueprint by app.py):
  POST /api/plan/generate   {description, weeks, capacity, refine?, previous_plan?}
                            -> {plan, stats}   (AI work breakdown + sprint schedule)
  POST /api/plan/create     {project, org?, plan, weeks}
                            -> starts a background job that provisions the Azure
                               DevOps project, sprints, areas and work items
  GET  /api/plan/progress   -> live job status for the UI progress bar

Uses the same PAT as the dashboard (from .env) — it must be allowed to create
projects in the target organization.
"""

import base64
import html
import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from flask import Blueprint, jsonify, request

bp = Blueprint("planner", __name__)

AGILE_TEMPLATE_ID = "adcc42ab-9882-485e-a3ed-7678f01f66bc"


tracker = None   # set by app.py at blueprint registration


def _tracker():
    """The running app module. app.py assigns it when registering the
    blueprint — a plain `import app` would create a second module copy when
    the server runs as `python app.py` (module __main__)."""
    if tracker is not None:
        return tracker
    import app
    return app


# ── AI plan generation ───────────────────────────────────────────────────────

PLAN_SYSTEM = """You are an expert program and delivery planner. Given a project
description, produce a complete work breakdown and sprint plan. Reply with JSON
ONLY — no prose, no markdown fences.

Schema:
{
  "areas": ["Short-Area-Name", ...],          // 2-6 work streams / life domains
  "epics": [
    {
      "title": "...", "description": "...", "area": "one of areas",
      "features": [
        {
          "title": "...",
          "tasks": [
            {
              "title": "...",                 // specific and actionable, <= 70 chars
              "description": "...",           // 2-4 sentences: what to do, why it
                                              // matters, ending with "Done when: ..."
              "sprint": 1,                    // 1..WEEKS; respect dependencies —
                                              // prerequisite work in earlier sprints
              "priority": 1,                  // 1 = must, 2 = should, 3 = nice
              "points": 1                     // effort: 1, 2, 3 or 5
                                              // (1 point ~ 30 minutes of focused work)
            }
          ]
        }
      ]
    }
  ]
}

Rules:
- Plan for WEEKS one-week sprints, aiming for about CAPACITY total points per
  sprint. Spread the load evenly; never leave early sprints empty.
- 3-6 epics; each epic 2-5 features; each feature 2-8 tasks.
- Front-load priority-1 work; put polish and stretch goals later.
- Add periodic checkpoint/review tasks (e.g. monthly review, mid-point check).
- Every description must be self-contained (the reader sees only that task) and
  must end with "Done when: ...".
"""


def _ai_json(system, user, max_tokens=14000):
    """Call the configured AI provider and parse a JSON object reply."""
    tracker = _tracker()
    provider = tracker.ai_provider()
    if provider is None:
        raise RuntimeError("No AI key configured. Set OPENAI_API_KEY or "
                           "ANTHROPIC_API_KEY in sprint-tracker/.env.")
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI()
        r = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o"),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_completion_tokens=max_tokens)
        text = r.choices[0].message.content or ""
    else:
        import anthropic
        client = anthropic.Anthropic()
        r = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in r.content if b.type == "text")
    text = re.sub(r"^```(json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def _rebalance(epics, cap, max_sprint):
    """Enforce the capacity cap: overflow shifts forward, P1 claims room first.
    Same algorithm as create_plan.py. Mutates task['sprint'] in place."""
    flat = []
    order = 0
    for e in epics:
        for f in e.get("features", []):
            for t in f.get("tasks", []):
                flat.append((t["sprint"], t["priority"], order, t))
                order += 1
    flat.sort(key=lambda x: (x[0], x[1], x[2]))
    used = defaultdict(float)
    for req, _prio, _o, task in flat:
        pts = task["points"]
        s = req
        while s < max_sprint and used[s] + pts > cap:
            s += 1
        used[s] += pts
        task["sprint"] = s


def _validate(raw, weeks, capacity):
    """Coerce the AI's JSON into a safe, consistent plan structure."""
    if not isinstance(raw, dict) or not raw.get("epics"):
        raise ValueError("AI reply is missing 'epics'")
    areas = [str(a).strip()[:60] for a in (raw.get("areas") or []) if str(a).strip()]
    areas = areas[:8] or ["General"]
    epics = []
    for e in raw["epics"]:
        if not e.get("title"):
            continue
        area = str(e.get("area") or areas[0]).strip()
        if area not in areas:
            area = areas[0]
        epic = {"title": str(e["title"]).strip()[:120],
                "description": str(e.get("description") or "").strip(),
                "area": area, "features": []}
        for f in e.get("features") or []:
            if not f.get("title"):
                continue
            feat = {"title": str(f["title"]).strip()[:120], "tasks": []}
            for t in f.get("tasks") or []:
                if not t.get("title"):
                    continue
                try:
                    sprint = max(1, min(weeks, int(t.get("sprint", 1))))
                except (TypeError, ValueError):
                    sprint = 1
                try:
                    priority = max(1, min(3, int(t.get("priority", 2))))
                except (TypeError, ValueError):
                    priority = 2
                try:
                    points = float(t.get("points", 1))
                except (TypeError, ValueError):
                    points = 1.0
                points = max(0.5, min(20.0, points))
                feat["tasks"].append({
                    "title": str(t["title"]).strip()[:255],
                    "description": str(t.get("description") or "").strip(),
                    "sprint": sprint, "priority": priority, "points": points,
                })
            if feat["tasks"]:
                epic["features"].append(feat)
        if epic["features"]:
            epics.append(epic)
    if not epics:
        raise ValueError("AI reply contained no usable tasks")
    _rebalance(epics, capacity, weeks)
    return {"areas": areas, "epics": epics}


def _stats(plan, weeks):
    load = defaultdict(float)
    n_feats = n_tasks = 0
    total = 0.0
    for e in plan["epics"]:
        for f in e["features"]:
            n_feats += 1
            for t in f["tasks"]:
                n_tasks += 1
                total += t["points"]
                load[t["sprint"]] += t["points"]
    return {
        "epics": len(plan["epics"]), "features": n_feats, "tasks": n_tasks,
        "total_points": total,
        "avg_points": round(total / weeks, 1) if weeks else 0,
        "sprint_load": [{"sprint": n, "points": load.get(n, 0)}
                        for n in range(1, weeks + 1)],
    }


@bp.post("/api/plan/generate")
def plan_generate():
    body = request.get_json(force=True)
    description = (body.get("description") or "").strip()
    if len(description) < 10:
        return jsonify({"error": "Describe the project in a sentence or two."}), 400
    try:
        weeks = max(2, min(52, int(body.get("weeks", 12))))
        capacity = max(2.0, min(100.0, float(body.get("capacity", 12))))
    except (TypeError, ValueError):
        return jsonify({"error": "weeks and capacity must be numbers"}), 400

    user = (f"PROJECT DESCRIPTION:\n{description}\n\n"
            f"WEEKS={weeks}\nCAPACITY={capacity} points per week")
    refine = (body.get("refine") or "").strip()
    if refine and body.get("previous_plan"):
        user += ("\n\nPREVIOUS PLAN (revise it per the instructions, keep what "
                 "is not mentioned):\n" + json.dumps(body["previous_plan"]) +
                 "\n\nREVISION INSTRUCTIONS:\n" + refine)

    last_err = None
    for attempt in range(2):
        try:
            raw = _ai_json(PLAN_SYSTEM, user if not last_err else
                           user + f"\n\nYour previous reply was invalid "
                                  f"({last_err}). Reply with valid JSON only.")
            plan = _validate(raw, weeks, capacity)
            return jsonify({"plan": plan, "weeks": weeks,
                            "stats": _stats(plan, weeks)})
        except (json.JSONDecodeError, ValueError) as e:
            last_err = str(e)[:200]
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            return jsonify({"error": f"AI call failed: {e}"}), 502
    return jsonify({"error": f"AI returned an invalid plan twice: {last_err}"}), 502


# ── Ideation chat ────────────────────────────────────────────────────────────

IDEATION_SYSTEM = """You are a project ideation partner inside a sprint-planning
app. The user wants to shape a rough idea into a clear project specification.

Help them think it through: ask focused questions (one or two at a time) about
goals, concrete deliverables, what is in and out of scope, constraints, how many
hours per week they can invest, desired duration, and success criteria. Offer
concrete suggestions and trade-offs. Keep replies short and conversational —
this is a chat panel, not a report.

When — and only when — the user says they are satisfied or asks you to finalize,
call finalize_specification with:
- specification: a complete, self-contained project brief (goal, deliverables,
  scope in/out, constraints, success criteria) written so a planning AI can
  break it into epics, features and tasks. No sprint-by-sprint schedule — that
  is the generator's job.
- weeks: suggested duration in weeks, if it was discussed.
- capacity: suggested effort points per week (1 point ~ 30 minutes), if the
  user's time budget was discussed.
After calling the tool, tell the user the spec is loaded into the generator
below and they can still edit it before pressing Generate plan."""

SPEC_TOOL_DESC = ("Load the finalized project specification into the plan "
                  "generator form. Call only when the user confirms they are "
                  "satisfied with the specification.")
SPEC_TOOL_PARAMS = {
    "type": "object",
    "properties": {
        "specification": {"type": "string"},
        "weeks": {"type": "integer"},
        "capacity": {"type": "number"},
    },
    "required": ["specification"],
    "additionalProperties": False,
}


def _ideation_openai(messages):
    from openai import OpenAI
    client = OpenAI()
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    tools = [{"type": "function",
              "function": {"name": "finalize_specification",
                           "description": SPEC_TOOL_DESC,
                           "parameters": SPEC_TOOL_PARAMS}}]
    convo = [{"role": "system", "content": IDEATION_SYSTEM}] + messages
    spec = None
    while True:
        r = client.chat.completions.create(model=model, messages=convo,
                                           tools=tools,
                                           max_completion_tokens=2000)
        msg = r.choices[0].message
        if not msg.tool_calls:
            return msg.content or "", spec
        convo.append({"role": "assistant", "content": msg.content,
                      "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            try:
                spec = json.loads(tc.function.arguments)
                out = "Specification captured and loaded into the generator."
            except ValueError as e:
                out = f"Error: {e}"
            convo.append({"role": "tool", "tool_call_id": tc.id, "content": out})


def _ideation_anthropic(messages):
    import anthropic
    client = anthropic.Anthropic()
    tools = [{"name": "finalize_specification",
              "description": SPEC_TOOL_DESC,
              "input_schema": SPEC_TOOL_PARAMS}]
    convo = list(messages)
    spec = None
    while True:
        r = client.messages.create(model="claude-opus-4-8", max_tokens=2000,
                                   system=IDEATION_SYSTEM, tools=tools,
                                   messages=convo)
        if r.stop_reason != "tool_use":
            return "".join(b.text for b in r.content if b.type == "text"), spec
        convo.append({"role": "assistant", "content": r.content})
        results = []
        for block in r.content:
            if block.type != "tool_use":
                continue
            spec = dict(block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": "Specification captured and loaded "
                                       "into the generator."})
        convo.append({"role": "user", "content": results})


@bp.post("/api/plan/chat")
def plan_chat():
    tracker = _tracker()
    provider = tracker.ai_provider()
    if provider is None:
        return jsonify({"error": "No AI key configured. Set OPENAI_API_KEY or "
                                 "ANTHROPIC_API_KEY in sprint-tracker/.env."}), 503
    messages = request.get_json(force=True).get("messages") or []
    if not messages:
        return jsonify({"error": "messages is required"}), 400
    messages = messages[-24:]   # keep the request bounded
    try:
        run = _ideation_openai if provider == "openai" else _ideation_anthropic
        reply, spec = run(messages)
        if spec:   # sanitize before it reaches the form
            try:
                spec["weeks"] = max(2, min(52, int(spec.get("weeks") or 12)))
            except (TypeError, ValueError):
                spec["weeks"] = 12
            try:
                spec["capacity"] = max(2.0, min(100.0,
                                                float(spec.get("capacity") or 12)))
            except (TypeError, ValueError):
                spec["capacity"] = 12.0
        return jsonify({"reply": reply, "spec": spec})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Azure DevOps provisioning (background job) ───────────────────────────────

JOB = {"running": False, "stage": "", "done": 0, "total": 0,
       "log": [], "error": None, "url": None, "project": None}
_job_lock = threading.Lock()


def _log(msg):
    JOB["log"].append(msg)
    del JOB["log"][:-200]


def _hdrs(pat, patch=False):
    auth = base64.b64encode(f":{pat}".encode()).decode()
    ct = "application/json-patch+json" if patch else "application/json"
    return {"Authorization": f"Basic {auth}", "Content-Type": ct}


def _desc_html(text):
    return "<p>" + html.escape(text).replace("\n", "<br>") + "</p>" if text else ""


def _ensure_project(base, project, pat):
    r = requests.get(f"{base}/_apis/projects/{project}?api-version=7.0",
                     headers=_hdrs(pat), timeout=20)
    if r.status_code == 200:
        _log(f"Project '{project}' already exists — planning into it")
        return
    _log(f"Creating project '{project}'…")
    r = requests.post(f"{base}/_apis/projects?api-version=7.0", headers=_hdrs(pat),
                      json={"name": project,
                            "description": "Created by Sprint Tracker AI Planner",
                            "visibility": "private",
                            "capabilities": {
                                "versioncontrol": {"sourceControlType": "Git"},
                                "processTemplate": {"templateTypeId": AGILE_TEMPLATE_ID},
                            }}, timeout=25)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Project creation failed "
                           f"({r.status_code}): {r.text[:200]}")
    for _ in range(30):
        time.sleep(3)
        r = requests.get(f"{base}/_apis/projects/{project}?api-version=7.0",
                         headers=_hdrs(pat), timeout=20)
        if r.status_code == 200 and r.json().get("state") == "wellFormed":
            _log("Project provisioned ✓")
            time.sleep(2)
            return
    raise RuntimeError("Timed out waiting for the project to provision")


def _create_areas(base, project, areas, pat):
    url = f"{base}/{project}/_apis/wit/classificationnodes/Areas?api-version=7.0"
    for a in areas:
        r = requests.post(url, headers=_hdrs(pat), json={"name": a}, timeout=20)
        if r.status_code in (200, 201, 409):
            _log(f"Area: {a}")
        else:
            _log(f"Area '{a}' failed ({r.status_code}) — tasks fall back to root")
        time.sleep(0.2)


def _create_sprints(base, project, weeks, pat):
    node_url = (f"{base}/{project}/_apis/wit/classificationnodes/Iterations"
                f"?api-version=7.0")
    team_url = (f"{base}/{project}/{project} Team/_apis/work/teamsettings/"
                f"iterations?api-version=7.0")
    today = datetime.now()
    start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)  # next Monday
    for n in range(1, weeks + 1):
        name = f"Sprint {n:02d}"
        end = start + timedelta(days=6)
        r = requests.post(node_url, headers=_hdrs(pat), json={
            "name": name,
            "attributes": {"startDate": start.strftime("%Y-%m-%dT00:00:00Z"),
                           "finishDate": end.strftime("%Y-%m-%dT00:00:00Z")},
        }, timeout=20)
        if r.status_code not in (200, 201, 409):
            _log(f"{name} failed ({r.status_code})")
        rid = requests.get(
            f"{base}/{project}/_apis/wit/classificationnodes/Iterations/{name}"
            f"?api-version=7.0", headers=_hdrs(pat), timeout=20)
        if rid.ok and rid.json().get("identifier"):
            requests.post(team_url, headers=_hdrs(pat),
                          json={"id": rid.json()["identifier"]}, timeout=20)
        start = end + timedelta(days=1)
    _log(f"{weeks} weekly sprints created ✓")


def _make_item(base, project, itype, title, pat, desc="", parent=None,
               sprint=None, area=None, priority=2, points=None):
    url = f"{base}/{project}/_apis/wit/workitems/${itype}?api-version=7.0"

    def build(full=True):
        ops = [{"op": "add", "path": "/fields/System.Title", "value": title}]
        if desc:
            ops.append({"op": "add", "path": "/fields/System.Description",
                        "value": _desc_html(desc)})
        ops.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority",
                    "value": priority})
        if area:
            ops.append({"op": "add", "path": "/fields/System.AreaPath",
                        "value": f"{project}\\{area}"})
        if full and points and itype == "Task":
            ops.append({"op": "add",
                        "path": "/fields/Microsoft.VSTS.Scheduling.OriginalEstimate",
                        "value": float(points) * 0.5})
        if full and sprint:
            ops.append({"op": "add", "path": "/fields/System.IterationPath",
                        "value": f"{project}\\Sprint {sprint:02d}"})
        if parent:
            ops.append({"op": "add", "path": "/relations/-",
                        "value": {"rel": "System.LinkTypes.Hierarchy-Reverse",
                                  "url": f"{base}/_apis/wit/workItems/{parent}"}})
        return ops

    for attempt in range(3):
        r = requests.post(url, headers=_hdrs(pat, patch=True),
                          json=build(), timeout=25)
        if r.status_code in (200, 201):
            return r.json()["id"]
        if r.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 400:   # sprint/estimate field rejected — retry minimal
            r2 = requests.post(url, headers=_hdrs(pat, patch=True),
                               json=build(full=False), timeout=25)
            if r2.status_code in (200, 201):
                _log(f"(minimal fallback) {title[:50]}")
                return r2.json()["id"]
        _log(f"✗ {itype} failed ({r.status_code}): {title[:50]}")
        return None
    return None


def _worker(org, project, plan, weeks, pat):
    base = f"https://dev.azure.com/{org}"
    try:
        JOB["stage"] = "project"
        _ensure_project(base, project, pat)
        JOB["stage"] = "areas"
        _create_areas(base, project, plan["areas"], pat)
        JOB["stage"] = "sprints"
        _create_sprints(base, project, weeks, pat)

        JOB["stage"] = "items"
        for epic in plan["epics"]:
            eid = _make_item(base, project, "Epic", epic["title"], pat,
                             desc=epic.get("description", ""), area=epic["area"])
            JOB["done"] += 1
            if not eid:
                JOB["done"] += sum(1 + len(f["tasks"]) for f in epic["features"])
                continue
            _log(f"📌 {epic['title'][:60]}")
            time.sleep(0.3)
            for feat in epic["features"]:
                fid = _make_item(base, project, "Feature", feat["title"], pat,
                                 parent=eid, area=epic["area"])
                JOB["done"] += 1
                if not fid:
                    JOB["done"] += len(feat["tasks"])
                    continue
                time.sleep(0.3)
                for t in feat["tasks"]:
                    tid = _make_item(base, project, "Task", t["title"], pat,
                                     desc=t.get("description", ""), parent=fid,
                                     sprint=t["sprint"], area=epic["area"],
                                     priority=t["priority"], points=t["points"])
                    JOB["done"] += 1
                    if tid:
                        _log(f"  ✓ [S{t['sprint']:02d}] {t['title'][:55]}")
                    time.sleep(0.2)

        JOB["stage"] = "done"
        JOB["url"] = f"{base}/{project}"
        _log("All done ✓ — dashboard switched to the new project")
        _tracker().set_active(org, project)
    except Exception as e:
        JOB["error"] = str(e)[:300]
        _log(f"✗ ERROR: {JOB['error']}")
    finally:
        JOB["running"] = False


@bp.post("/api/plan/create")
def plan_create():
    tracker = _tracker()
    body = request.get_json(force=True)
    project = (body.get("project") or "").strip()
    org = (body.get("org") or tracker.ORG).strip()
    plan = body.get("plan")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}", project or ""):
        return jsonify({"error": "Enter a valid project name (letters, digits, "
                                 "spaces, . _ -)."}), 400
    if not plan or not plan.get("epics"):
        return jsonify({"error": "Generate a plan first."}), 400
    try:
        weeks = max(2, min(52, int(body.get("weeks", 12))))
    except (TypeError, ValueError):
        return jsonify({"error": "weeks must be a number"}), 400
    if not tracker.PAT:
        return jsonify({"error": "AZURE_DEVOPS_PAT is not configured."}), 503

    with _job_lock:
        if JOB["running"]:
            return jsonify({"error": "A plan is already being created."}), 409
        total = sum(1 + sum(1 + len(f["tasks"]) for f in e["features"])
                    for e in plan["epics"])
        JOB.update(running=True, stage="starting", done=0, total=total,
                   log=[], error=None, url=None, project=project)
    threading.Thread(target=_worker,
                     args=(org, project, plan, weeks, tracker.PAT),
                     daemon=True).start()
    return jsonify({"started": True, "total": total})


@bp.get("/api/plan/progress")
def plan_progress():
    return jsonify({**{k: v for k, v in JOB.items() if k != "log"},
                    "log": JOB["log"][-30:]})
