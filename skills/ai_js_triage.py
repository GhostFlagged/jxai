import json
from .base import Skill
from ai.client import SinoxClient
from ai.router import ModelRouter
from ai.prompts import JS_TRIAGE_SYSTEM


class AiJsTriageSkill(Skill):
    """Sonnet-5 ranks downloaded JS files by API-endpoint richness before the deep pass."""

    name = "ai_js_triage"

    def __init__(self):
        self.ai = SinoxClient()
        self.model = ModelRouter.get("js_triage")  # t-claude-sonnet-5

    def run(self, ctx):
        files = ctx.get("js_files", [])
        if not files:
            return {"ai_js_priority": []}

        payload = []
        for f in files[:200]:
            try:
                head = open(f["path"], encoding="utf-8", errors="ignore").read(1200)
            except Exception:
                head = ""
            payload.append({"id": f["path"], "url": f.get("url", ""), "size": f["size"], "head": head})

        try:
            result = self.ai.chat_json(
                self.model,
                [
                    {"role": "system", "content": JS_TRIAGE_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                max_tokens=4000,
            )
            model_order = [p for p in result.get("priority", []) if p]
        except Exception as e:
            ctx.setdefault("errors", []).append(f"ai_js_triage: {e}")
            model_order = []

        seen = set(model_order)
        known_paths = {f["path"] for f in files}
        model_order = [p for p in model_order if p in known_paths]
        ordered = model_order + [p for p in known_paths if p not in set(model_order)]
        return {"ai_js_priority": ordered}
