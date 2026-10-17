# ai/client.py
import re, json, os, requests
from anthropic import Anthropic
from openai import OpenAI

SinoxBase = "https://sinoxapi.com"
SinoxV1 = "https://sinoxapi.com/v1"
API_KEY = os.environ.get("SINOX_API_KEY", "")


def _strip_fences(s: str) -> str:
    t = s.strip()
    if t.startswith("```"):
        if "\n" in t:
            t = t.split("\n", 1)[1]
        t = t.removesuffix("```").strip()
    return t


def _repair_json(s: str):
    """Best-effort parse of model JSON that may be truncated or wrapped in prose.

    A large audit response occasionally gets cut mid-string, which makes the whole
    object unparseable and silently drops an entire evidence chunk. Rather than lose
    it, progressively trim from the last well-formed element and close the open
    brackets/strings so we salvage the earliest complete records.
    """
    s = s.strip()
    # 1) direct
    try:
        return json.loads(s)
    except Exception:
        pass

    # 2) narrow to the outermost {...} or [...] run
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start == -1:
            continue
        end = s.rfind(closer)
        if end > start:
            frag = s[start:end + 1]
            try:
                return json.loads(frag)
            except Exception:
                pass

    # 3) repair: walk the string tracking depth + whether we're inside a string,
    #    recording the last position where the structure was "safe" to cut.
    start = s.find("{")
    if start == -1:
        start = s.find("[")
    if start == -1:
        return None
    body = s[start:]
    stack, in_str, esc = [], False, False
    for i, ch in enumerate(body):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                candidate = body[:i + 1]
                try:
                    return json.loads(candidate)
                except Exception:
                    pass
    # unterminated: truncate to the last comma/close that keeps depth sane, then close
    cut = body.rstrip()
    if in_str:
        # drop the dangling partial string back to its opening quote
        q = cut.rfind('"')
        cut = cut[:q] if q != -1 else cut
    cut = cut.rstrip().rstrip(",")
    closers = "".join("}" if c == "{" else "]" for c in reversed(stack))
    for _ in range(3):
        try:
            return json.loads(cut + closers)
        except Exception:
            # remove a trailing partial element and retry
            for sep in (",", "}", "]", '"'):
                j = cut.rfind(sep)
                if j > 0:
                    cut = cut[:j]
                    break
            else:
                return None
    return None


class SinoxClient:
    """
    One client, three surfaces:
      - chat()      -> OpenAI-compatible /v1/chat/completions  (default entry; claude models via this route work)
      - messages()  -> Anthropic /v1/messages                  (explicit for invoke/skill compatibility)
      - decide()    -> Jev /v1/decisions                       (t-jev-1.13 only)
    All auth via SINOX_API_KEY.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or API_KEY or os.environ.get("SINOX_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("SINOX_API_KEY is not set (env or .env)")
        self.anthropic = Anthropic(api_key=self.api_key, base_url=SinoxBase)
        self.openai = OpenAI(api_key=self.api_key, base_url=SinoxV1)

    def chat(self, model: str, messages: list, **kw) -> str:
        kw.setdefault("max_tokens", 4000)
        resp = self.openai.chat.completions.create(model=model, messages=messages, **kw)
        return resp.choices[0].message.content

    def chat_json(self, model: str, messages: list, **kw) -> dict:
        kw.setdefault("response_format", {"type": "json_object"})
        raw = self.chat(model, messages, **kw)
        fragment = _strip_fences(raw)
        try:
            return json.loads(fragment)
        except Exception:
            pass
        repaired = _repair_json(fragment)
        if repaired is not None:
            return repaired
        m = re.search(r"\{[\s\S]*\}", fragment)
        if m:
            return json.loads(m.group(0))
        m2 = re.search(r"\[[\s\S]*\]", fragment)
        if m2:
            return json.loads(m2.group(0))
        raise ValueError("model did not return parseable JSON")

    def messages(self, model: str, system: str, user: str, **kw) -> str:
        kw.setdefault("max_tokens", 4000)
        msg = self.anthropic.messages.create(
            model=model, system=system, messages=[{"role": "user", "content": user}], **kw
        )
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""

    def decide(self, state: str | dict, questions: dict, model: str = "t-jev-1.13") -> dict:
        r = requests.post(
            f"{SinoxV1}/decisions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": model, "state": state, "questions": questions},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
