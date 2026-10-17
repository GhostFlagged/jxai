# ai/router.py

SONNET = "t-claude-sonnet-5"
JEV = "t-jev-1.13"


class ModelRouter:
    """
    Single-model registry. Every generative role routes to t-claude-sonnet-5.

    One model on purpose: the old multi-model routing (cheap triage model ->
    Opus deep pass) cost more than it saved, because Sonnet had to re-derive the
    ranking the cheap model produced anyway. Jev stays — it is a deterministic
    classifier, not a generator, so it gates which candidates Sonnet reads.
    """

    ROUTES = {
        "js_triage": SONNET,
        "deep_audit": SONNET,
        "auth_triage": SONNET,
        "report_writer": SONNET,
        "classifier": JEV,
    }

    @classmethod
    def get(cls, role: str) -> str:
        if role not in cls.ROUTES:
            raise KeyError(f"Unknown AI role: {role}")
        return cls.ROUTES[role]
