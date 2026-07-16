"""
Pydantic models shared across the agent API.
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    request: str = Field(..., description="Natural language request from the user")


class TaskStep(BaseModel):
    step_number: int
    name: str
    description: str
    status: str = "pending"  # pending -> running -> done / failed / recovered


class AgentPlan(BaseModel):
    document_type: str
    title: str
    sections: List[str]
    assumptions: List[str] = []
    reasoning: str = ""
    tasks: List[TaskStep] = []


class AgentResponse(BaseModel):
    request: str
    plan: AgentPlan
    tasks: List[TaskStep]
    assumptions: List[str]
    llm_mode: str  # "llm" or "fallback" -> tells caller whether real LLM or rule-based fallback was used
    document_filename: str
    download_url: str
    summary: str
