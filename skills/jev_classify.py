# skills/jev_classify.py
from .base import Skill
from ai.client import SinoxClient
from ai.router import ModelRouter

class JevClassifySkill(Skill):
    name = "jev_classify"

    def __init__(self):
        self.ai = SinoxClient()
        self.model = ModelRouter.get("classifier")   # t-jev-1.13

    def run(self, ctx):
        results = ctx.get("auth_results", [])
        interesting = []
        cap = int(ctx.get("config", {}).get("jev_classify", {}).get("max_probes", 200))
        seen = 0

        for r in results:
            for p in r.get("probes", []):
                if p.get("status") != 200:
                    continue
                seen += 1
                if seen > cap:
                    break
                # Jev takes a "state" (any string/object) and typed questions.
                state = (
                    f"URL: {r['url']}\n"
                    f"Probe: {p['probe']}\n"
                    f"Content-Type: {p.get('content_type','')}\n"
                    f"Length: {p.get('length',0)}\n"
                    f"Body snippet: {p.get('snippet','')[:400]}"
                )
                questions = {
                    "bucket": {
                        "type": "choice",
                        "instructions": "What kind of response is this?",
                        "criteria": {
                            "real_data":    "Contains actual application/user data",
                            "auth_error":   "Contains an auth/permission error message",
                            "generic_error":"Generic 404/500-style error page",
                            "static_page":  "HTML shell, SPA index, or landing page",
                            "redirect":     "Redirect or login wall",
                        },
                    },
                    "worth_manual_review": {
                        "type": "noul",   # yes/no with probability
                        "instructions": "Should a human researcher manually inspect this?",
                    },
                }
                try:
                    ans = self.ai.decide(state=state, questions=questions,
                                         model=self.model)
                    bucket = ans["answers"]["bucket"]
                    worth  = ans["answers"]["worth_manual_review"]["noul"]
                except Exception as e:
                    ctx.setdefault("errors", []).append(f"jev: {e}")
                    continue

                p["jev_bucket"] = bucket["choice"]
                p["jev_confidence"] = bucket["probabilities"][bucket["choice"]]
                p["jev_worth_review"] = worth

                if bucket["choice"] == "real_data" and worth > 0.6:
                    interesting.append({"url": r["url"], "probe": p})

        return {"jev_findings": interesting, "auth_results": results}