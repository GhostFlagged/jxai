import json
from .base import Skill
from ai.client import SinoxClient
from ai.router import ModelRouter
from ai.prompts import AUTH_TRIAGE_SYSTEM

class AiTriageResultsSkill(Skill):
    """Sonnet-5 triages Jev-surfaced probe results. No longer Opus-gated."""

    name = "ai_triage_results"

    def __init__(self):
        self.ai = SinoxClient()
        self.model = ModelRouter.get("auth_triage")  # t-claude-sonnet-5

    def run(self, ctx):
        candidates = ctx.get("jev_findings", [])
        if not candidates:
            return {"ai_findings": []}

        findings = []
        cap = int(ctx.get("config", {}).get("ai_triage_results", {}).get("max_candidates", 25))
        for c in candidates[:cap]:
            url = c["url"]
            p = c["probe"]
            user = (
                f"Endpoint: {url}\n"
                f"Probe: {p['probe']}\n"
                f"HTTP status: {p['status']}\n"
                f"Content-Type: {p.get('content_type','')}\n"
                f"Response snippet:\n\"\"\"\n{p.get('snippet','')[:1800]}\n\"\"\"\n\n"
                "Is this an unauthenticated access finding? "
                "Return JSON with keys: verdict (\"confirmed\"|\"likely\"|\"false_positive\"), "
                "confidence (0-1), reasoning (1-2 sentences), "
                "next_step (one concrete test to run)."
            )

            try:
                result = self.ai.chat_json(
                    self.model,
                    [{"role": "system", "content": AUTH_TRIAGE_SYSTEM},
                     {"role": "user",   "content": user}],
                    max_tokens=1500,
                )
                if not isinstance(result, dict):
                    result = {"verdict": str(result)}
                result["url"] = url
                findings.append(result)
            except Exception as e:
                ctx.setdefault("errors", []).append(f"ai_triage: {e}")

        return {"ai_findings": findings}
