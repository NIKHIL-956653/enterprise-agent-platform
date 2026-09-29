"""
Doc-writer agent.

The graph is tested directly, with no database, because it is pure orchestration: a fake
model that answers by prompt type is enough to prove the loop, the cap and the exit. One
HTTP test at the end proves the agent is wired into the platform like every other.
"""

import uuid

from sqlalchemy import text

from eap.agents import runner
from eap.agents.docwriter_graph import build_graph
from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password

PASSWORD = "correct-horse-battery-staple"


class ScriptedLLM:
    """Answers by prompt type; reviewer verdicts come from a script, in order."""

    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[str] = []  # "outline" | "draft" | "review", in order
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        self.prompts.append(prompt)
        if "bullet-point outline" in prompt:
            self.calls.append("outline")
            return "- Overview: the thing\n- Technical Details: the how"
        if "You are now the reviewer" in prompt:
            self.calls.append("review")
            return self.verdicts.pop(0) if self.verdicts else "REVISE\n1. still wrong"
        self.calls.append("draft")
        return f"# Draft {self.calls.count('draft')}\n\nBody."


def _state(max_revisions: int = 2) -> dict:
    return {
        "ticket": "ID: T-1\nTITLE: Add login\nDESCRIPTION:\nUsers must log in.",
        "max_revisions": max_revisions,
        "outline": "",
        "draft": "",
        "critique": "",
        "revisions": 0,
        "approved": False,
    }


async def test_approved_first_time_runs_each_node_once() -> None:
    llm = ScriptedLLM(["APPROVE"])

    final = await build_graph(llm).ainvoke(_state())

    assert llm.calls == ["outline", "draft", "review"]
    assert final["approved"] is True
    assert final["revisions"] == 0
    assert final["draft"] == "# Draft 1\n\nBody."


async def test_one_revision_feeds_critique_into_second_draft() -> None:
    llm = ScriptedLLM(["REVISE\n1. Acceptance criteria missing", "APPROVE"])

    final = await build_graph(llm).ainvoke(_state())

    assert llm.calls == ["outline", "draft", "review", "draft", "review"]
    assert final["approved"] is True
    assert final["revisions"] == 1
    assert final["draft"] == "# Draft 2\n\nBody."
    second_draft_prompt = llm.prompts[3]
    assert "<review>" in second_draft_prompt
    assert "Acceptance criteria missing" in second_draft_prompt


async def test_revision_cap_ends_the_loop_unapproved() -> None:
    llm = ScriptedLLM([])  # every review says REVISE, forever

    final = await build_graph(llm).ainvoke(_state(max_revisions=2))

    # 2 rejections allowed: draft, reject, draft, reject, stop. No unreviewed third draft.
    assert llm.calls.count("draft") == 2
    assert llm.calls.count("review") == 2
    assert final["approved"] is False
    assert final["revisions"] == 2
    assert "still wrong" in final["critique"]  # the last objection survives for a human


async def _seed_tenant_with_docwriter() -> dict[str, str]:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, :slug) RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (gen_random_uuid(), :tid, :email, :ph)"
            ),
            {"tid": tid, "email": email, "ph": hash_password(PASSWORD)},
        )
        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, 'docwriter', 'Document Writer')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return {"slug": slug, "email": email}


async def test_docwriter_runs_through_the_platform(client, monkeypatch) -> None:
    llm = ScriptedLLM(["APPROVE"])
    monkeypatch.setattr(runner, "_default_llm", lambda: llm)
    seed = await _seed_tenant_with_docwriter()
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed["email"], "password": PASSWORD},
    )
    token = r.json()["access_token"]

    r = await client.post(
        "/v1/agents/docwriter/run",
        json={
            "ticket_id": "DEMO-1",
            "title": "Add login rate limiting",
            "description": "Limit failed logins to 5 per minute per tenant.",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded", body
    assert body["output"]["approved"] is True
    assert body["output"]["revisions"] == 0
    assert body["output"]["document"].startswith("# Draft")
    # The ticket text reached the model fenced, after the instructions.
    assert "<ticket>" in llm.prompts[0]
    assert "Add login rate limiting" in llm.prompts[0]
