"""
agent.py

The autonomous agent. Given a free-text request it:

  1. PLANS  - decides what kind of document is needed and what sections
              it should contain (multi-step planning).
  2. EXECUTES - "runs" each planned step, generating the content for every
                section one at a time, tracking task status as it goes.
  3. BUILDS  - hands the finished content to doc_generator to produce a
               polished .docx file.
  4. REPORTS - returns the plan, the task list (with statuses), any
               assumptions it had to make, and a short summary.

Two content-generation strategies are supported and are chosen
automatically at runtime:

  - "llm"      : uses LLMClient (Groq) to plan and write section content.
  - "fallback" : deterministic, rule-based planner + template writer.

This is the Retry & Fallback engineering improvement in action: the agent
always tries the LLM first, but never depends on it being reachable.
"""
from __future__ import annotations

import logging
import re
from typing import List, Tuple

from llm_client import LLMClient, LLMUnavailableError
from models import AgentPlan, TaskStep
from doc_generator import build_docx

logger = logging.getLogger("agent.core")


# ---------------------------------------------------------------------------
# Rule-based knowledge base used by the fallback planner.
# Each entry: keywords to look for -> (document_type, title_template, sections)
# ---------------------------------------------------------------------------
DOCUMENT_TEMPLATES = {
    "proposal": {
        "keywords": ["proposal", "pitch", "quote", "offer", "sponsorship"],
        "sections": [
            "Executive Summary",
            "Background & Problem Statement",
            "Proposed Solution",
            "Scope of Work",
            "Timeline",
            "Budget & Pricing",
            "Next Steps",
        ],
    },
    "meeting_minutes": {
        "keywords": ["meeting minutes", "minutes of meeting", "mom", "meeting notes"],
        "sections": [
            "Meeting Details",
            "Attendees",
            "Agenda",
            "Discussion Summary",
            "Decisions Made",
            "Action Items",
            "Next Meeting",
        ],
    },
    "project_plan": {
        "keywords": ["project plan", "roadmap", "implementation plan", "delivery plan"],
        "sections": [
            "Project Overview",
            "Objectives",
            "Scope",
            "Milestones & Timeline",
            "Resources & Roles",
            "Risks & Mitigation",
            "Success Criteria",
        ],
    },
    "business_report": {
        "keywords": ["business report", "quarterly report", "status report", "performance report"],
        "sections": [
            "Executive Summary",
            "Key Metrics",
            "Analysis",
            "Challenges",
            "Recommendations",
            "Conclusion",
        ],
    },
    "technical_design": {
        "keywords": ["technical design", "design document", "architecture", "system design", "tdd"],
        "sections": [
            "Overview",
            "Goals & Non-Goals",
            "System Architecture",
            "Data Model",
            "API Design",
            "Security Considerations",
            "Rollout Plan",
        ],
    },
    "sop": {
        "keywords": ["sop", "standard operating procedure", "process document", "procedure"],
        "sections": [
            "Purpose",
            "Scope",
            "Roles & Responsibilities",
            "Procedure Steps",
            "Exceptions & Escalation",
            "Revision History",
        ],
    },
    "product_spec": {
        "keywords": ["product spec", "prd", "product requirements", "feature spec"],
        "sections": [
            "Overview",
            "Problem Statement",
            "Goals & Success Metrics",
            "User Stories",
            "Functional Requirements",
            "Out of Scope",
            "Open Questions",
        ],
    },
}

DEFAULT_TEMPLATE_KEY = "project_plan"


def _score_templates(request_lower: str) -> str:
    """Pick the best-matching document template via simple keyword scoring."""
    best_key, best_score = DEFAULT_TEMPLATE_KEY, 0
    for key, spec in DOCUMENT_TEMPLATES.items():
        score = sum(1 for kw in spec["keywords"] if kw in request_lower)
        if score > best_score:
            best_key, best_score = key, score
    return best_key


def _derive_title(request: str, doc_type_label: str) -> str:
    # crude but effective: title-case a trimmed version of the request
    cleaned = re.sub(r"\s+", " ", request).strip()
    if len(cleaned) > 70:
        cleaned = cleaned[:70].rsplit(" ", 1)[0] + "..."
    return f"{doc_type_label}: {cleaned}" if cleaned else doc_type_label


def _detect_missing_info(request: str) -> List[str]:
    """
    Looks for common gaps in an ambiguous/underspecified request and returns
    the reasonable assumptions the agent will make to proceed autonomously.
    """
    assumptions = []
    lower = request.lower()

    if not re.search(r"\b(rs\.?|inr|\$|usd|budget of|rupees|dollars)\b", lower):
        assumptions.append(
            "No budget was specified — the document uses a placeholder budget "
            "range and flags it clearly for the user to update."
        )
    if not re.search(r"\b(by|before|deadline|within \d+|weeks|months|days|due)\b", lower):
        assumptions.append(
            "No deadline/timeline was given — a reasonable default timeline "
            "(4-6 weeks in phased milestones) was assumed."
        )
    if not re.search(r"\b(client|customer|company|for [A-Z][a-zA-Z]+|team)\b", request):
        assumptions.append(
            "No specific client/company/team name was provided — generic "
            "placeholders (e.g. '[Client Name]') were used and should be "
            "replaced before sending."
        )
    if "and" in lower and len(re.findall(r"\band\b", lower)) >= 2:
        assumptions.append(
            "The request combines multiple objectives — the agent prioritized "
            "them into a single coherent document rather than producing "
            "several conflicting drafts."
        )
    if re.search(r"\b(or|either)\b", lower):
        assumptions.append(
            "The request presented alternative/conflicting options — the "
            "agent picked the most business-appropriate interpretation and "
            "documented the alternative as a noted assumption."
        )
    return assumptions


class Agent:
    def __init__(self, llm_client: LLMClient, output_dir: str = "outputs"):
        self.llm = llm_client
        self.output_dir = output_dir

    # ------------------------------------------------------------------ #
    # STEP 1: PLANNING
    # ------------------------------------------------------------------ #
    def plan(self, request: str) -> Tuple[AgentPlan, str]:
        """Returns (plan, mode) where mode is 'llm' or 'fallback'."""
        try:
            plan = self._plan_with_llm(request)
            return plan, "llm"
        except LLMUnavailableError as exc:
            logger.info("Falling back to rule-based planner: %s", exc)
            plan = self._plan_with_rules(request)
            return plan, "fallback"

    def _plan_with_llm(self, request: str) -> AgentPlan:
        system_prompt = (
            "You are an autonomous business-document planning agent. "
            "Given a user's request, decide the single best business document "
            "type to produce and design its structure. "
            "Respond ONLY with a JSON object with keys: "
            "document_type (one of: proposal, meeting_minutes, project_plan, "
            "business_report, technical_design, sop, product_spec), "
            "title (string), sections (array of 5-8 section name strings), "
            "assumptions (array of strings describing any gaps you filled in), "
            "reasoning (one sentence on why you chose this structure)."
        )
        raw = self.llm.complete(system_prompt, request, json_mode=True)
        data = self.llm.safe_json_parse(raw)
        if not data or "sections" not in data:
            raise LLMUnavailableError("LLM returned an unparsable/incomplete plan.")

        return AgentPlan(
            document_type=data.get("document_type", DEFAULT_TEMPLATE_KEY),
            title=data.get("title") or _derive_title(request, "Document"),
            sections=data.get("sections") or DOCUMENT_TEMPLATES[DEFAULT_TEMPLATE_KEY]["sections"],
            assumptions=data.get("assumptions") or [],
            reasoning=data.get("reasoning", ""),
        )

    def _plan_with_rules(self, request: str) -> AgentPlan:
        lower = request.lower()
        key = _score_templates(lower)
        spec = DOCUMENT_TEMPLATES[key]
        label = key.replace("_", " ").title()
        return AgentPlan(
            document_type=key,
            title=_derive_title(request, label),
            sections=list(spec["sections"]),
            assumptions=_detect_missing_info(request),
            reasoning=(
                f"Rule-based planner matched keywords to the '{label}' template "
                f"(LLM unavailable, deterministic fallback used)."
            ),
        )

    # ------------------------------------------------------------------ #
    # STEP 2: EXECUTION
    # ------------------------------------------------------------------ #
    def execute(self, request: str, plan: AgentPlan, mode: str) -> Tuple[dict, List[TaskStep]]:
        """
        Runs one "task" per section, generating its content, and returns
        (section_content_map, task_list_with_final_statuses).
        """
        tasks: List[TaskStep] = []
        content: dict = {}

        for i, section in enumerate(plan.sections, start=1):
            step = TaskStep(
                step_number=i,
                name=f"Write section: {section}",
                description=f"Generate content for the '{section}' section of the {plan.document_type} document.",
                status="running",
            )
            try:
                if mode == "llm":
                    text = self._write_section_with_llm(request, plan, section)
                else:
                    text = self._write_section_with_rules(request, plan, section)
                step.status = "done"
            except LLMUnavailableError as exc:
                # graceful per-step recovery: an individual section falling
                # back doesn't take down the whole document
                logger.info("Section '%s' falling back: %s", section, exc)
                text = self._write_section_with_rules(request, plan, section)
                step.status = "recovered (used fallback writer)"

            content[section] = text
            tasks.append(step)

        return content, tasks

    def _write_section_with_llm(self, request: str, plan: AgentPlan, section: str) -> str:
        system_prompt = (
            f"You are drafting the '{section}' section of a {plan.document_type} "
            f"titled '{plan.title}'. Write 2-4 concise, professional paragraphs "
            "or a short bullet list where appropriate. Use realistic mock data "
            "(names, numbers, dates) where the user didn't supply real ones. "
            "Do not repeat the section title in your answer. Plain text only, "
            "no markdown headers."
        )
        return self.llm.complete(system_prompt, request, json_mode=False).strip()

    def _write_section_with_rules(self, request: str, plan: AgentPlan, section: str) -> str:
        """Deterministic template content, so the agent always produces a full document."""
        snippet = request.strip()
        if len(snippet) > 160:
            snippet = snippet[:160].rsplit(" ", 1)[0] + "..."

        generic = {
            "Executive Summary": (
                f"This document was generated in response to the request: \"{snippet}\". "
                "It summarizes the objective, approach, and next steps at a high level "
                "for stakeholders who need the key points without reading the full document."
            ),
            "Attendees": "1. [Name], Project Sponsor\n2. [Name], Project Lead\n3. [Name], Stakeholder",
            "Agenda": "1. Review previous action items\n2. Status update\n3. Open discussion\n4. Next steps",
            "Budget & Pricing": (
                "Estimated budget: INR 5,00,000 - 8,00,000 (placeholder — no figure was "
                "specified in the original request; update with actual pricing)."
            ),
            "Timeline": (
                "Phase 1 (Weeks 1-2): Discovery & requirements\n"
                "Phase 2 (Weeks 3-4): Execution\n"
                "Phase 3 (Weeks 5-6): Review & handover"
            ),
        }
        if section in generic:
            return generic[section]

        return (
            f"[Auto-generated content for '{section}'] Based on the request \"{snippet}\", "
            f"this section outlines the relevant details for the {plan.document_type.replace('_', ' ')}. "
            "Mock data has been used where specifics were not provided by the user; "
            "please review and replace placeholder values before distribution."
        )

    # ------------------------------------------------------------------ #
    # STEP 3 & 4: BUILD DOCUMENT + REPORT
    # ------------------------------------------------------------------ #
    def run(self, request: str) -> dict:
        plan, mode = self.plan(request)

        # Planning step is itself tracked as task #0 for a transparent task list
        planning_task = TaskStep(
            step_number=0,
            name="Analyze request & create execution plan",
            description=(
                f"Classified request as '{plan.document_type}' and defined "
                f"{len(plan.sections)} sections to produce."
            ),
            status="done" if mode == "llm" else "recovered (used rule-based planner)",
        )

        content, section_tasks = self.execute(request, plan, mode)
        all_tasks = [planning_task] + section_tasks

        build_task = TaskStep(
            step_number=len(all_tasks),
            name="Assemble Word document",
            description="Render the planned sections into a formatted .docx file.",
            status="running",
        )
        filename, filepath = build_docx(
            document_type=plan.document_type,
            title=plan.title,
            sections=content,
            assumptions=plan.assumptions,
        )
        build_task.status = "done"
        all_tasks.append(build_task)

        plan.tasks = all_tasks

        summary = (
            f"Generated a {plan.document_type.replace('_', ' ')} titled "
            f"'{plan.title}' with {len(plan.sections)} sections "
            f"({'LLM-authored' if mode == 'llm' else 'rule-based fallback content'}). "
            f"{len(plan.assumptions)} assumption(s) were made to fill gaps in the request."
        )

        return {
            "plan": plan,
            "tasks": all_tasks,
            "assumptions": plan.assumptions,
            "mode": mode,
            "filename": filename,
            "filepath": filepath,
            "summary": summary,
        }
