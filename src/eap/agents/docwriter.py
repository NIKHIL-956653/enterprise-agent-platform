"""
Document-writer agent: a Jira-shaped ticket in, a reviewed technical document out.

Port of the GenAI Document Writing Assistant onto the platform. The input is the same
shape jira_fetch.py produced there, so a real Jira fetch (EAP-26) drops in without
changing this file. What is new is the graph: the original made one call; this one
outlines, drafts, has the model review its own draft, and revises up to a cap.
"""

from pydantic import BaseModel, Field

from eap.agents.base import AgentContext, AgentError, BaseAgent
from eap.agents.docwriter_graph import build_graph
from eap.agents.registry import register


class DocWriterInput(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=3, max_length=300)
    description: str = Field(min_length=10, max_length=20000)
    status: str = Field(default="To Do", max_length=50)
    labels: list[str] = Field(default_factory=list, max_length=20)


class DocWriterOutput(BaseModel):
    document: str
    outline: str
    # These two are the audit trail: a document that took two revisions and was never
    # approved deserves a human look before it goes to Confluence.
    approved: bool
    revisions: int
    # Empty when approved. Otherwise the reviewer's last objections - the thing a human
    # needs to read before deciding whether to publish anyway.
    critique: str


def _render_ticket(t: DocWriterInput) -> str:
    """One text rendering, built once, reused by every prompt in the graph."""
    labels = ", ".join(t.labels) or "none"
    return (
        f"ID: {t.ticket_id}\nTITLE: {t.title}\nSTATUS: {t.status}\nLABELS: {labels}\n"
        f"DESCRIPTION:\n{t.description}"
    )


@register
class DocWriterAgent(BaseAgent):
    name = "docwriter"
    display_name = "Document Writer"
    InputModel = DocWriterInput
    OutputModel = DocWriterOutput

    async def run(self, payload: DocWriterInput, ctx: AgentContext) -> DocWriterOutput:
        if ctx.llm is None:
            raise AgentError("no language model is available for this run")

        # First use of the per-tenant agent config row. A tenant that wants stricter
        # documents can raise the cap; nobody can set it below zero or above five.
        max_revisions = max(0, min(5, int(ctx.config.get("max_revisions", 2))))

        final = await build_graph(ctx.llm).ainvoke(
            {
                "ticket": _render_ticket(payload),
                "max_revisions": max_revisions,
                "outline": "",
                "draft": "",
                "critique": "",
                "revisions": 0,
                "approved": False,
            }
        )
        return DocWriterOutput(
            document=final["draft"],
            outline=final["outline"],
            approved=final["approved"],
            revisions=final["revisions"],
            critique=final["critique"],
        )
