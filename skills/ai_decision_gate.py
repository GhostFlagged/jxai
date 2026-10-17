# skills/ai_decision_gate.py
from .base import Skill
from ai.client import SinoxClient

class AiDecisionGateSkill(Skill):
    """Uses Jev to decide whether the pipeline should keep escalating."""
    name = "ai_decision_gate"

    def __init__(self):
        self.ai = SinoxClient()

    def run(self, ctx):
        findings = ctx.get("jev_findings", [])
        if not findings:
            return {"escalate": False, "gate_reason": "no_jeev_findings"}

        state = {
            "count": len(findings),
            "top_urls": [f["url"] for f in findings[:10]],
            "top_confidences": [f["probe"].get("jev_confidence", 0)
                                for f in findings[:10]],
        }
        questions = {
            "escalate": {
                "type": "noul",
                "instructions": "Should this be escalated to a Sonnet triage review?",
                "criteria": {
                    "true":  "At least one high-confidence unauthenticated candidate",
                    "false": "All candidates are likely false positives or low value",
                },
            }
        }
        try:
            ans = self.ai.decide(state=state, questions=questions)
            prob = ans["answers"]["escalate"]["noul"]
            return {"escalate": prob > 0.5, "gate_confidence": prob}
        except Exception:
            return {"escalate": True, "gate_confidence": 1.0}  # fail-open