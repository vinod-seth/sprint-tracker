# Sprint Tracker

A local web dashboard for the VS-EnterpriseAI Azure DevOps project:

- **See sprint progress** — pick any of the 30 sprints, see a progress bar,
  points done/total, and every task with its priority, area, and state.
- **Update tasks inline** — change a task's state straight from the table.
- **Talk to AI about a task** — click 💬 AI on any task to open a chat panel.
  The assistant (Claude) can discuss scope, split work, adjust estimates, and
  **apply agreed changes directly to Azure DevOps** (title, state, description,
  priority, points).

## Setup

```powershell
cd sprint-tracker
pip install -r requirements.txt
copy .env.example .env    # then edit .env
python app.py             # open http://localhost:5000
```

`.env` values:

| Key | Notes |
|---|---|
| `AZURE_DEVOPS_ORG` / `AZURE_DEVOPS_PAT` / `AZURE_DEVOPS_PROJECT` | Optional — falls back to `../vs-master-plan/vs-master-plan/.env` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Set at least one to enable the AI chat panel |
| `AI_PROVIDER` | Optional: force `anthropic` or `openai` (default: Anthropic if both keys set) |
| `OPENAI_MODEL` | Optional: OpenAI model override (default `gpt-4o`) |

## How the AI chat works

Each chat is scoped to one work item. The backend runs a tool-use loop —
Claude (`claude-opus-4-8`) or OpenAI (`gpt-4o`), depending on which key is
configured — with two tools:

- `get_task` — re-reads the live work item
- `update_task` — applies a JSON-patch update to Azure DevOps

The model only calls `update_task` after a change is agreed in conversation,
and the UI refreshes the task row and shows "task updated ✓" when it does.

## Token optimization

- **Prompt caching** — the large system prompt (base instructions + context
  document) carries a cache breakpoint on the Anthropic path, and a second
  breakpoint on the last chat message caches the whole conversation prefix;
  follow-up turns replay it at ~10% of input price. OpenAI caches repeated
  prompt prefixes (>1024 tokens) automatically.
- **History window** — only the last `CHAT_MAX_HISTORY` (default 12) messages
  go to the model; older ones are replaced with a one-line omission note.
- **Slim task context** — task descriptions are truncated to 400 chars in the
  injected context (the model calls `get_task` when it needs the full record).
- **Effort control** — the Anthropic path runs at `AI_EFFORT=low` (right for a
  chat panel); raise to `medium`/`high` if answers feel shallow.
- **Output cap** — `CHAT_MAX_TOKENS` (default 4000) bounds each reply.
- **Visibility** — each reply shows `tokens: X in / Y out (Z cached)` in the
  chat panel, and the server logs per-turn usage.

Note: editing `vs-ai-assistant-context.md` invalidates the provider-side
prompt cache (the prefix changed), so the first chat turn after an edit pays
full input price — that's expected.

## Assistant context (`vs-ai-assistant-context.md`)

The assistant's system prompt includes `vs-ai-assistant-context.md` — the
application's purpose, plan structure, financial decisions, capacity rules,
and compliance guardrails. The file is re-read whenever it changes (no restart
needed), so keep §10 "Current State Snapshot" updated weekly. It contains
personal financial data: it is sent only to the configured AI provider as
system context — keep the file itself private.
