"""
Sprint Tracker — Azure DevOps sprint dashboard with AI task assistant.

  python app.py            # http://localhost:5000

Reads AZURE_DEVOPS_ORG / AZURE_DEVOPS_PAT / AZURE_DEVOPS_PROJECT from .env
(falls back to ../vs-master-plan/vs-master-plan/.env for the DevOps values).
ANTHROPIC_API_KEY enables the per-task AI chat.
"""

import base64
import json
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
# Fall back to the plan generator's .env for DevOps credentials
load_dotenv(HERE.parent / "vs-master-plan" / "vs-master-plan" / ".env")

ORG = os.getenv("AZURE_DEVOPS_ORG", "VS-EnterpriseAI")
PAT = os.getenv("AZURE_DEVOPS_PAT", "")
PROJECT = os.getenv("AZURE_DEVOPS_PROJECT", "VS-EnterpriseAI")
TEAM = os.getenv("AZURE_DEVOPS_TEAM", f"{PROJECT} Team")
BASE = f"https://dev.azure.com/{ORG}"

def ai_provider():
    """'anthropic' or 'openai', by AI_PROVIDER env or whichever key is set."""
    forced = os.getenv("AI_PROVIDER", "").lower()
    if forced in ("anthropic", "openai"):
        return forced
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return None


FIELDS = [
    "System.Id", "System.Title", "System.State", "System.WorkItemType",
    "System.AreaPath", "System.IterationPath", "System.Description",
    "Microsoft.VSTS.Common.Priority", "Microsoft.VSTS.Scheduling.OriginalEstimate",
]

app = Flask(__name__, static_folder="static")


# ── Azure DevOps client ──────────────────────────────────────────────────────

def _headers(patch=False):
    auth = base64.b64encode(f":{PAT}".encode()).decode()
    ct = "application/json-patch+json" if patch else "application/json"
    return {"Authorization": f"Basic {auth}", "Content-Type": ct}


def ado_get(path, **params):
    r = requests.get(f"{BASE}/{path}", headers=_headers(), params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def wiql(query):
    r = requests.post(
        f"{BASE}/{PROJECT}/_apis/wit/wiql?api-version=7.0",
        headers=_headers(), json={"query": query}, timeout=20)
    r.raise_for_status()
    return [w["id"] for w in r.json().get("workItems", [])]


def get_items(ids):
    if not ids:
        return []
    items = []
    for i in range(0, len(ids), 200):
        chunk = ",".join(map(str, ids[i:i + 200]))
        data = ado_get(f"{PROJECT}/_apis/wit/workitems",
                       ids=chunk, fields=",".join(FIELDS), **{"api-version": "7.0"})
        items.extend(data["value"])
    return [_shape(w) for w in items]


def _shape(w):
    f = w["fields"]
    est = f.get("Microsoft.VSTS.Scheduling.OriginalEstimate")
    return {
        "id": w["id"],
        "title": f.get("System.Title", ""),
        "state": f.get("System.State", ""),
        "type": f.get("System.WorkItemType", ""),
        "area": f.get("System.AreaPath", "").split("\\")[-1],
        "iteration": f.get("System.IterationPath", ""),
        "description": f.get("System.Description", ""),
        "priority": f.get("Microsoft.VSTS.Common.Priority"),
        # create_plan.py stores hours = points * 0.5
        "points": round(est * 2) if est else None,
        "url": f"{BASE}/{PROJECT}/_workitems/edit/{w['id']}",
    }


def update_item(item_id, fields):
    """fields: dict of friendly-name -> value. Returns the updated item."""
    path_map = {
        "title": "/fields/System.Title",
        "state": "/fields/System.State",
        "description": "/fields/System.Description",
        "priority": "/fields/Microsoft.VSTS.Common.Priority",
        "iteration": "/fields/System.IterationPath",
    }
    ops = []
    for key, value in fields.items():
        if key == "points":
            ops.append({"op": "add",
                        "path": "/fields/Microsoft.VSTS.Scheduling.OriginalEstimate",
                        "value": float(value) * 0.5})
        elif key in path_map:
            ops.append({"op": "add", "path": path_map[key], "value": value})
    if not ops:
        raise ValueError(f"No updatable fields in {list(fields)}")
    r = requests.patch(
        f"{BASE}/{PROJECT}/_apis/wit/workitems/{item_id}?api-version=7.0",
        headers=_headers(patch=True), json=ops, timeout=20)
    r.raise_for_status()
    return _shape(r.json())


def create_task(fields):
    """Create a new Task work item. fields: title (required), description,
    priority, points, iteration (full path)."""
    if not fields.get("title"):
        raise ValueError("title is required")
    ops = [{"op": "add", "path": "/fields/System.Title", "value": fields["title"]}]
    if fields.get("description"):
        ops.append({"op": "add", "path": "/fields/System.Description",
                    "value": fields["description"]})
    if fields.get("priority"):
        ops.append({"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority",
                    "value": int(fields["priority"])})
    if fields.get("points"):
        ops.append({"op": "add",
                    "path": "/fields/Microsoft.VSTS.Scheduling.OriginalEstimate",
                    "value": float(fields["points"]) * 0.5})
    if fields.get("iteration"):
        ops.append({"op": "add", "path": "/fields/System.IterationPath",
                    "value": fields["iteration"]})
    r = requests.post(
        f"{BASE}/{PROJECT}/_apis/wit/workitems/$Task?api-version=7.0",
        headers=_headers(patch=True), json=ops, timeout=25)
    r.raise_for_status()
    return _shape(r.json())


def team_iterations():
    data = ado_get(f"{PROJECT}/{TEAM}/_apis/work/teamsettings/iterations",
                   **{"api-version": "7.0"})
    return data["value"]


def resolve_sprint(name):
    """Accepts 'Sprint 05', 'sprint 5', '5' or a full iteration path;
    returns the full iteration path."""
    s = str(name).strip()
    its = team_iterations()
    for it in its:
        if s.lower() in (it["name"].lower(), it["path"].lower()):
            return it["path"]
    m = re.search(r"\d+", s)
    if m:
        want = int(m.group())
        for it in its:
            m2 = re.search(r"\d+", it["name"])
            if m2 and int(m2.group()) == want:
                return it["path"]
    raise ValueError(f"Unknown sprint: {name!r}")


def _shape_comment(c):
    return {
        "id": c.get("id"),
        "text": c.get("text", ""),
        "by": (c.get("createdBy") or {}).get("displayName", ""),
        "date": (c.get("createdDate") or "")[:16].replace("T", " "),
    }


def get_comments(item_id):
    data = ado_get(f"{PROJECT}/_apis/wit/workItems/{item_id}/comments",
                   **{"api-version": "7.1-preview.3"})
    return [_shape_comment(c) for c in data.get("comments", [])]


def add_comment(item_id, text):
    r = requests.post(
        f"{BASE}/{PROJECT}/_apis/wit/workItems/{item_id}/comments"
        f"?api-version=7.1-preview.3",
        headers=_headers(), json={"text": text}, timeout=20)
    r.raise_for_status()
    return _shape_comment(r.json())


# ── API routes ───────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify({
        "project": PROJECT,
        "org": ORG,
        "ai_enabled": ai_provider() is not None,
        "ai_provider": ai_provider(),
    })


@app.get("/api/sprints")
def sprints():
    data = ado_get(f"{PROJECT}/{TEAM}/_apis/work/teamsettings/iterations",
                   **{"api-version": "7.0"})
    out = []
    for it in data["value"]:
        attrs = it.get("attributes", {})
        out.append({
            "name": it["name"],
            "path": it["path"],
            "start": (attrs.get("startDate") or "")[:10],
            "finish": (attrs.get("finishDate") or "")[:10],
            "timeframe": attrs.get("timeFrame", ""),
        })
    return jsonify(out)


@app.get("/api/sprints/<path:iteration_path>/tasks")
def sprint_tasks(iteration_path):
    ids = wiql(
        f"SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{PROJECT}' "
        f"AND [System.IterationPath] = '{iteration_path}' "
        f"AND [System.WorkItemType] = 'Task' "
        f"ORDER BY [Microsoft.VSTS.Common.Priority], [System.Id]")
    return jsonify(get_items(ids))


@app.get("/api/states")
def states():
    data = ado_get(f"{PROJECT}/_apis/wit/workitemtypes/Task/states",
                   **{"api-version": "7.0"})
    return jsonify([s["name"] for s in data["value"]])


def _ado_error(e):
    if isinstance(e, requests.HTTPError) and e.response is not None:
        try:
            return e.response.json().get("message", str(e))
        except ValueError:
            return str(e)
    return str(e)


@app.patch("/api/tasks/<int:item_id>")
def patch_task(item_id):
    try:
        return jsonify(update_item(item_id, request.get_json(force=True)))
    except (requests.HTTPError, ValueError) as e:
        return jsonify({"error": _ado_error(e)}), 400


@app.post("/api/tasks")
def new_task():
    try:
        return jsonify(create_task(request.get_json(force=True))), 201
    except (requests.HTTPError, ValueError) as e:
        return jsonify({"error": _ado_error(e)}), 400


@app.get("/api/tasks/<int:item_id>/comments")
def task_comments(item_id):
    try:
        return jsonify(get_comments(item_id))
    except requests.HTTPError as e:
        return jsonify({"error": _ado_error(e)}), 400


@app.post("/api/tasks/<int:item_id>/comments")
def post_comment(item_id):
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    try:
        return jsonify(add_comment(item_id, text)), 201
    except requests.HTTPError as e:
        return jsonify({"error": _ado_error(e)}), 400


# ── AI chat ──────────────────────────────────────────────────────────────────

CHAT_TOOLS = [
    {
        "name": "get_task",
        "description": "Fetch the current, full details of an Azure DevOps work item "
                       "by its ID, including title, state, description, priority, "
                       "story points, sprint, and area.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "update_task",
        "description": "Update fields of an Azure DevOps work item. Call this when the "
                       "user has agreed on a change to the task. Only pass the fields "
                       "being changed. 'points' is the effort estimate in story points; "
                       "'state' must be one of the project's valid Task states; "
                       "'description' accepts HTML.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "title": {"type": "string"},
                "state": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
                "points": {"type": "number"},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "add_comment",
        "description": "Add a comment to an Azure DevOps work item — use it to log "
                       "the user's progress updates, blockers, and the decisions "
                       "agreed in this chat. Plain text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["task_id", "text"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "move_task",
        "description": "Move a task to a different sprint. 'sprint' can be a name "
                       "like 'Sprint 05' or just the number. Use list_sprints and "
                       "list_sprint_tasks first to pick a sensible target week, and "
                       "only move after the user has agreed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "sprint": {"type": "string"},
            },
            "required": ["task_id", "sprint"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "create_task",
        "description": "Create a new Task work item — e.g. when splitting a large "
                       "task into smaller ones the user agreed to. 'sprint' is a "
                       "name like 'Sprint 07'; 'points' is the effort estimate; "
                       "'description' accepts HTML.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sprint": {"type": "string"},
                "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
                "points": {"type": "number"},
                "description": {"type": "string"},
            },
            "required": ["title", "sprint"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "list_sprints",
        "description": "List all sprints with their names, start/finish dates and "
                       "which one is current. Use this before proposing to move "
                       "work to another week.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "list_sprint_tasks",
        "description": "List the tasks in a given sprint with their points and "
                       "states, plus the total planned points. Use it to check a "
                       "target week's load before moving work there (weekly "
                       "capacity is about 12 points).",
        "input_schema": {
            "type": "object",
            "properties": {"sprint": {"type": "string"}},
            "required": ["sprint"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

BASE_PROMPT = (
    "You are a sprint assistant for the user's personal Azure DevOps project "
    f"'{PROJECT}'. The user is discussing one specific task with you. Help them "
    "think it through — clarify scope, break work down, adjust estimates, or "
    "update status — and when a concrete change is agreed, apply it with the "
    "tools. Use get_task if you need the latest state.\n\n"
    "When the user reports progress or a blocker (no time this week, money "
    "needed elsewhere, a dependency slipped, life happened):\n"
    "1. Acknowledge it and record their update as a comment on the task with "
    "add_comment, so the history is kept on the work item.\n"
    "2. Then ASK whether they want to adjust the plan, and propose 2-3 concrete "
    "options — e.g. move the task to a specific later sprint (use list_sprints "
    "for dates and list_sprint_tasks to check that week's load; weekly capacity "
    "is about 12 points, so prefer the earliest week with room), split it into "
    "smaller tasks with create_task, reduce its points, or lower its priority. "
    "Say which option you recommend and why.\n"
    "3. Apply a change ONLY after the user picks an option — then use move_task / "
    "update_task / create_task, and log a short add_comment summarising what was "
    "decided and why.\n\n"
    "Confirm what you changed after a tool call. Keep replies short and "
    "conversational; this is a chat panel, not a report. "
    "Do not invent fields or change anything the user did not ask for."
)

CONTEXT_FILE = HERE / "vs-ai-assistant-context.md"
_context_cache = {"mtime": None, "text": ""}


def system_prompt():
    """BASE_PROMPT plus the app-purpose context document, reloaded on change."""
    try:
        mtime = CONTEXT_FILE.stat().st_mtime
        if _context_cache["mtime"] != mtime:
            _context_cache["text"] = CONTEXT_FILE.read_text(encoding="utf-8")
            _context_cache["mtime"] = mtime
    except OSError:
        _context_cache.update(mtime=None, text="")
    if not _context_cache["text"]:
        return BASE_PROMPT
    return (
        BASE_PROMPT
        + "\n\nThe full context for this application — its purpose, the plan "
          "structure, financial decisions, capacity rules, and compliance "
          "guardrails — follows. Treat it as authoritative. One override: "
          "apply approved task updates with the update_task tool; the "
          "text 'UPDATE:' block format described in the document is superseded "
          "by the tool.\n\n" + _context_cache["text"]
    )


# ── token optimization knobs ─────────────────────────────────────────────────
# CHAT_MAX_HISTORY  – only the last N chat messages are sent to the model
# AI_EFFORT         – Anthropic effort level (low is plenty for a chat panel)
# CHAT_MAX_TOKENS   – hard cap on the model's output per turn
MAX_HISTORY = int(os.getenv("CHAT_MAX_HISTORY", "12"))
AI_EFFORT = os.getenv("AI_EFFORT", "low")
CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "4000"))


def _slim_task(task, desc_limit=400):
    """Task dict with the description trimmed — the model can call get_task
    for the full record; no need to pay for long HTML on every turn."""
    slim = dict(task)
    desc = slim.get("description") or ""
    if len(desc) > desc_limit:
        slim["description"] = desc[:desc_limit] + " …[truncated — use get_task]"
    slim.pop("url", None)
    return slim


def _trim_history(messages):
    if len(messages) <= MAX_HISTORY:
        return messages
    dropped = len(messages) - MAX_HISTORY
    note = {"role": "user",
            "content": f"[{dropped} earlier messages omitted to save tokens]"}
    return [note] + messages[-MAX_HISTORY:]


def _context_message(task):
    return ("Context — the task under discussion (JSON):\n"
            + json.dumps(_slim_task(task)) + "\n\nConversation follows.")


def execute_tool(name, tool_input):
    """Run a chat tool. Returns (result_text, updated_task_or_None)."""
    if name == "get_task":
        return json.dumps(get_items([tool_input["task_id"]])[0]), None
    if name == "update_task":
        fields = {k: v for k, v in tool_input.items() if k != "task_id"}
        updated = update_item(tool_input["task_id"], fields)
        return "Updated successfully: " + json.dumps(updated), updated
    if name == "add_comment":
        c = add_comment(tool_input["task_id"], tool_input["text"])
        return "Comment added: " + json.dumps(c), None
    if name == "move_task":
        path = resolve_sprint(tool_input["sprint"])
        updated = update_item(tool_input["task_id"], {"iteration": path})
        return "Moved: " + json.dumps(updated), updated
    if name == "create_task":
        fields = dict(tool_input)
        fields["iteration"] = resolve_sprint(fields.pop("sprint"))
        created = create_task(fields)
        return "Created: " + json.dumps(created), created
    if name == "list_sprints":
        out = [{"name": it["name"],
                "start": (it.get("attributes", {}).get("startDate") or "")[:10],
                "finish": (it.get("attributes", {}).get("finishDate") or "")[:10],
                "timeframe": it.get("attributes", {}).get("timeFrame", "")}
               for it in team_iterations()]
        return json.dumps(out), None
    if name == "list_sprint_tasks":
        path = resolve_sprint(tool_input["sprint"])
        ids = wiql(
            f"SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{PROJECT}' "
            f"AND [System.IterationPath] = '{path}' "
            f"AND [System.WorkItemType] = 'Task'")
        items = get_items(ids)
        slim = [{"id": t["id"], "title": t["title"], "state": t["state"],
                 "points": t["points"]} for t in items]
        total = sum(t["points"] or 0 for t in items)
        return json.dumps({"sprint": path, "total_points": total,
                           "tasks": slim}), None
    raise ValueError(f"Unknown tool {name}")


def run_chat_anthropic(messages, task):
    """Claude tool-use loop. Returns (reply_text, updated_task_or_None, usage)."""
    import anthropic

    client = anthropic.Anthropic()
    updated = None
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    convo = [{"role": "user", "content": _context_message(task)}] \
        + _trim_history(messages)
    # Cache breakpoint on the last user message: system+tools+context+history
    # all replay from cache on the next turn of this chat.
    last = convo[-1]
    if isinstance(last["content"], str):
        last["content"] = [{"type": "text", "text": last["content"],
                            "cache_control": {"type": "ephemeral"}}]

    while True:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=CHAT_MAX_TOKENS,
            system=[{"type": "text", "text": system_prompt(),
                     "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            output_config={"effort": AI_EFFORT},
            tools=CHAT_TOOLS,
            messages=convo,
        )
        usage["input"] += response.usage.input_tokens
        usage["output"] += response.usage.output_tokens
        usage["cache_read"] += response.usage.cache_read_input_tokens or 0
        usage["cache_write"] += response.usage.cache_creation_input_tokens or 0

        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text")
            return text, updated, usage

        convo.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                out, upd = execute_tool(block.name, block.input)
                updated = upd or updated
                results.append({"type": "tool_result",
                                "tool_use_id": block.id, "content": out})
            except Exception as e:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"Error: {e}", "is_error": True})
        convo.append({"role": "user", "content": results})


def run_chat_openai(messages, task):
    """OpenAI function-calling loop. Returns (reply_text, updated_task_or_None, usage)."""
    from openai import OpenAI

    client = OpenAI()
    model = os.getenv("OPENAI_MODEL", "gpt-4o")
    updated = None
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    tools = [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    } for t in CHAT_TOOLS]
    # Stable prefix (system + task context) first — OpenAI caches prompts
    # >1024 tokens automatically when the prefix repeats across turns.
    convo = ([{"role": "system", "content": system_prompt()},
              {"role": "user", "content": _context_message(task)}]
             + _trim_history(messages))

    while True:
        response = client.chat.completions.create(
            model=model, messages=convo, tools=tools,
            max_completion_tokens=CHAT_MAX_TOKENS)
        u = response.usage
        usage["input"] += u.prompt_tokens
        usage["output"] += u.completion_tokens
        details = getattr(u, "prompt_tokens_details", None)
        if details and getattr(details, "cached_tokens", None):
            usage["cache_read"] += details.cached_tokens

        msg = response.choices[0].message
        if not msg.tool_calls:
            return msg.content or "", updated, usage

        convo.append({"role": "assistant", "content": msg.content,
                      "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            try:
                out, upd = execute_tool(
                    tc.function.name, json.loads(tc.function.arguments))
                updated = upd or updated
            except Exception as e:
                out = f"Error: {e}"
            convo.append({"role": "tool", "tool_call_id": tc.id, "content": out})


@app.post("/api/chat")
def chat():
    provider = ai_provider()
    if provider is None:
        return jsonify({"error": "No AI key configured. Set ANTHROPIC_API_KEY or "
                                 "OPENAI_API_KEY in sprint-tracker/.env."}), 503
    body = request.get_json(force=True)
    task_id, messages = body.get("task_id"), body.get("messages", [])
    if not task_id or not messages:
        return jsonify({"error": "task_id and messages are required"}), 400
    try:
        task = get_items([task_id])[0]
        run = run_chat_openai if provider == "openai" else run_chat_anthropic
        reply, updated, usage = run(messages, task)
        print(f"[chat] task={task_id} in={usage['input']} out={usage['output']} "
              f"cache_read={usage['cache_read']} cache_write={usage['cache_write']}")
        return jsonify({"reply": reply, "task": updated, "usage": usage})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    if not PAT:
        print("WARNING: AZURE_DEVOPS_PAT is not set — the dashboard will not load.")
    app.run(debug=True, port=5000)
