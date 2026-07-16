# Autonomous Document Agent

A small autonomous agent that takes a natural-language request, plans its own
task list, executes each step, and returns a polished `.docx` business
document — exposed over a FastAPI `POST /agent` endpoint.

## Architecture

```
client ──POST /agent──▶ FastAPI (main.py)
                             │
                             ▼
                        Agent (agent.py)
                        ├─ 1. plan()     → LLMClient.complete()  ──▶ Groq API
                        │                      │ (fails/unset)
                        │                      ▼
                        │                 rule-based planner (keyword scoring
                        │                 over DOCUMENT_TEMPLATES)
                        ├─ 2. execute()  → per-section content, same
                        │                  LLM-first / rule-based-fallback pattern
                        └─ 3. build_docx() (doc_generator.py, python-docx)
                             │
                             ▼
                     AgentResponse: plan + task list + assumptions +
                     download_url ──▶ GET /download/{file} returns the .docx
```

**Files**

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app, request validation/guardrails, `/agent` and `/download` routes |
| `agent.py` | Planning, execution, task tracking, assumption detection |
| `llm_client.py` | Groq API wrapper with retry + backoff + fallback signaling |
| `doc_generator.py` | Builds the final `.docx` with python-docx |
| `models.py` | Pydantic request/response schemas |
| `test_client.py` | Runs the two required test cases end-to-end |

## Agent workflow (autonomous planning & execution)

1. **Plan** — the agent asks itself "what kind of document does this need,
   and what sections should it contain?" It first tries the LLM (Groq,
   `llama-3.1-8b-instant`, free tier) with a JSON-mode prompt. If the LLM is
   unreachable, unconfigured, or returns garbage, it falls back to a
   deterministic keyword-scoring planner over 7 built-in document templates
   (proposal, meeting minutes, project plan, business report, technical
   design, SOP, product spec).
2. **Execute** — one task per planned section. Each task independently tries
   the LLM to write that section's content, and independently falls back to
   a template writer if that one call fails — so a single flaky LLM call
   degrades one section, not the whole document.
3. **Build** — `doc_generator.py` assembles a title page, an "Agent
   Assumptions & Notes" callout box (only if the agent had to guess at
   missing info), and one heading + content block per section, using
   python-docx.
4. **Report** — the API returns the full task list (with per-step status:
   `done`, `recovered (used fallback writer)`, etc.), the assumptions made,
   and a download link for the `.docx`.

## Mandatory Engineering Improvement: Retry & Fallback Logic

**What was implemented:** Every LLM call (planning and each section's
content generation) is wrapped in exponential-backoff retries
(`llm_client.py`). If the LLM is unconfigured, rate-limited, times out, or
returns unparsable output after all retries, the client raises
`LLMUnavailableError` instead of propagating a raw exception. The `Agent`
catches this at two levels:
- **Plan-level**: falls back to a rule-based document-type classifier and
  section list.
- **Section-level**: falls back to a template-based writer for that single
  section only.

**Why this one:** A document-generation agent is only actually useful if it
*reliably* produces a document. Free-tier LLM APIs (Groq, Gemini, etc.) are
exactly the kind of dependency that will occasionally rate-limit, time out,
or hiccup — and in this sandboxed/CI environment, outbound calls to
`api.groq.com` are blocked entirely, which is a realistic stand-in for "the
LLM is down." Rather than let that turn into a 500 error, the agent
degrades gracefully and still returns a complete, well-formatted document.

**How it improves the agent:** The `/agent` endpoint's demonstrated
end-to-end test runs (see below) never fail even with zero LLM connectivity
— every request still produces a correctly-typed document with correctly
detected assumptions. The response also tells the caller *which* mode was
used (`llm_mode: "llm" | "fallback"`) and per-task whether it needed
recovery, so the behavior is transparent rather than silently degraded.

## Setup

```bash
pip install -r requirements.txt

# optional but recommended — get a free key at https://console.groq.com
export GROQ_API_KEY=your_free_groq_key

uvicorn main:app --reload --port 8000
```

Without `GROQ_API_KEY` set, the agent runs entirely on the rule-based
fallback path — useful for grading/testing without any API credits at all,
per the assignment's "free or locally runnable" requirement. To use a local
model instead, point `GROQ_API_URL`-equivalent logic in `llm_client.py` at
an Ollama/LM Studio OpenAI-compatible endpoint (both expose the same
`/v1/chat/completions` schema Groq uses).

## Two required test inputs

Run automatically via:

```bash
python test_client.py
```

1. **Standard business request:**
   > "Create meeting minutes for our weekly project sync between the
   > engineering team and the client, covering sprint progress, blockers,
   > and next week's action items."

   → Agent classifies this as `meeting_minutes`, plans 7 standard sections
   (Meeting Details, Attendees, Agenda, Discussion Summary, Decisions Made,
   Action Items, Next Meeting), executes each as a task, and returns a
   ready-to-send `.docx`.

2. **Complex / ambiguous request:**
   > "We need something to send to a potential client about our new AI
   > automation service, and also something internal for the team to track
   > how we'll actually deliver it, but I'm not sure about pricing or the
   > timeline yet — just make reasonable calls and get it done."

   This request is deliberately ambiguous: it asks for two different
   documents (external pitch + internal tracker), gives no budget, no
   timeline, and explicitly tells the agent to just decide. The agent:
   - picks **one** coherent document type (`project_plan`) rather than
     failing or producing two conflicting drafts,
   - detects and logs 4 explicit assumptions (no budget → placeholder
     range; no deadline → default 4–6 week phased timeline; multiple
     objectives → merged into one document; and the missing pricing/scope
     decision is called out in the assumptions list),
   - still produces a complete, correctly structured `.docx`.

Both cases were run in this environment against the real FastAPI server;
`api.groq.com` is not reachable from the sandbox network, so both runs
exercised the **fallback path end-to-end** — a live demonstration that the
retry & fallback logic keeps the agent fully functional with zero LLM
availability. Sample generated documents are included in `outputs/`.

## Debugging insight (for video)

Talking point: the background `uvicorn` process kept dying between sandbox
commands because each tool invocation ran in a fresh subshell, killing the
backgrounded server when its parent shell exited. Root cause: `&` alone
doesn't detach a process from its controlling shell/session — the shell
runner reaped it on exit. Fix: launch with `setsid ... &` so the server
gets its own session and survives past the command that started it. This
is a good real-world parallel to the exact class of "silent dependency
failure" the retry/fallback logic is designed to catch: the fix isn't just
"try again," it's understanding *why* the process died before deciding how
to recover.

## Tradeoff discussion (for video)

**Autonomous Planning vs. Deterministic Workflows.** The agent lets the LLM
freely choose document type and section structure rather than hardcoding a
fixed template per request category. This buys real autonomy — it can
reasonably handle requests that don't map cleanly onto one template (the
ambiguous test case) — at the cost of predictability: two similar requests
could get slightly different section sets, which matters for downstream
systems expecting a stable schema. The mitigation implemented here is a
constrained middle ground: the LLM chooses freely, but always within a
JSON schema (`document_type`, `title`, `sections`, `assumptions`,
`reasoning`) and against a rule-based fallback that *is* fully
deterministic — so the system has autonomy when the LLM is available and
falls back to predictable behavior when it isn't, rather than picking one
extreme.

## API reference

`POST /agent`
```json
{ "request": "Draft a project proposal for a mobile banking app redesign." }
```

Response (truncated):
```json
{
  "request": "...",
  "plan": { "document_type": "proposal", "title": "...", "sections": ["..."], "assumptions": ["..."] },
  "tasks": [ { "step_number": 0, "name": "Analyze request & create execution plan", "status": "done" }, "..." ],
  "assumptions": ["..."],
  "llm_mode": "llm",
  "document_filename": "Proposal_..._a1b2c3d4.docx",
  "download_url": "/download/Proposal_..._a1b2c3d4.docx",
  "summary": "Generated a proposal titled '...' with 7 sections (LLM-authored). 1 assumption(s) were made."
}
```

`GET /download/{filename}` → returns the `.docx` file.

`GET /health` → `{"status": "ok", "llm_configured": bool, "model": "..."}`
