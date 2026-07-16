"""
main.py

FastAPI service exposing the autonomous document-writing agent.

Run:
    export GROQ_API_KEY=your_free_groq_key   # optional — falls back gracefully if unset
    uvicorn main:app --reload --port 8000

Endpoints:
    POST /agent            -> run the agent end-to-end on a natural language request
    GET  /download/{name}  -> download a previously generated .docx
    GET  /health           -> liveness check
"""
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from models import AgentRequest, AgentResponse
from llm_client import LLMClient
from agent import Agent
from doc_generator import OUTPUT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("agent.api")

app = FastAPI(
    title="Autonomous Document Agent",
    description=(
        "Accepts a natural language request, plans its own task list, "
        "executes each step, and returns a generated .docx business document."
    ),
    version="1.0.0",
)

llm_client = LLMClient()
agent = Agent(llm_client=llm_client)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "llm_configured": llm_client.is_configured,
        "model": llm_client.model,
    }


@app.post("/agent", response_model=AgentResponse)
def run_agent(payload: AgentRequest):
    request_text = payload.request.strip()

    # ---- Request validation & guardrails ----
    if not request_text:
        raise HTTPException(status_code=400, detail="`request` must not be empty.")
    if len(request_text) < 8:
        raise HTTPException(
            status_code=400,
            detail="`request` is too short for the agent to plan a meaningful document.",
        )
    if len(request_text) > 4000:
        raise HTTPException(
            status_code=400,
            detail="`request` is too long (max 4000 characters).",
        )

    try:
        result = agent.run(request_text)
    except Exception as exc:
        # Should be extremely rare: even LLM failure is handled internally via
        # fallback. This is the last line of defense so the API never 500s
        # silently without a useful message.
        logger.exception("Agent run failed unexpectedly")
        raise HTTPException(status_code=500, detail=f"Agent failed to complete the request: {exc}")

    plan = result["plan"]
    filename = result["filename"]

    return AgentResponse(
        request=request_text,
        plan=plan,
        tasks=result["tasks"],
        assumptions=result["assumptions"],
        llm_mode=result["mode"],
        document_filename=filename,
        download_url=f"/download/{filename}",
        summary=result["summary"],
    )


@app.get("/download/{filename}")
def download(filename: str):
    # basic path traversal guard
    safe_name = os.path.basename(filename)
    filepath = os.path.join(OUTPUT_DIR, safe_name)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=safe_name,
    )
