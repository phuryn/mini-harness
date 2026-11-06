"""A scripted stand-in for the Anthropic client, so the loop's invariants can be tested offline.

Each script item is a BetaMessage (returned), an exception (raised), a Broken
stream, or a callable that takes the request kwargs and returns one of those.
"""
import copy
import itertools
from types import SimpleNamespace

from anthropic.types.beta import BetaMessage

_ids = itertools.count(1)


def usage(inp=1000, out=50, read=0, write=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write}


def reply(*blocks, stop="end_turn", model="claude-opus-5-5", u=None, transformations=None):
    data = {"id": f"msg_{next(_ids)}", "type": "message", "role": "assistant", "model": model,
            "content": list(blocks), "stop_reason": stop, "stop_sequence": None, "usage": u or usage()}
    if transformations is not None:
        data["input_transformations"] = transformations
    return BetaMessage.model_validate(data)


def text(t):
    return {"type": "text", "text": t}


def thinking(sig="sig"):
    return {"type": "thinking", "thinking": "", "signature": sig}


def tool(name, inp, tid=None):
    return {"type": "tool_use", "id": tid or f"toolu_{next(_ids)}", "name": name, "input": inp}


class Broken:
    """A stream that shows the reply's text, then raises `error` mid-stream, or with no error stops before the end
    (no stop reason). A plain reply's stream, like some providers', sends no message_stop."""
    def __init__(self, msg, error=None):
        self.msg, self.error = msg, error


class _Stream:
    response = SimpleNamespace(request=None)  # what an SDK error needs

    def __init__(self, msg, error=None, whole=True):
        self.msg, self.error, self.whole = msg, error, whole

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield SimpleNamespace(type="message_start")
        for b in self.msg.content:
            if b.type == "text":
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text))
        if self.error:
            raise self.error

    def get_final_message(self):
        return self.msg if self.whole else self.msg.model_copy(update={"stop_reason": None})


class _Messages:
    def __init__(self, script):
        self.script, self.requests, self.creates = list(script), [], []

    def _next(self, kw):
        if not self.script:
            raise AssertionError("fake client ran out of scripted replies")
        item = self.script.pop(0)
        item = item(kw) if callable(item) else item
        if isinstance(item, BaseException):
            raise item
        return item

    def stream(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        item = self._next(kw)
        return _Stream(item.msg, item.error, whole=False) if isinstance(item, Broken) else _Stream(item)

    def create(self, **kw):
        self.creates.append(copy.deepcopy(kw))
        return self._next(kw)


class FakeClient:
    def __init__(self, script):
        self.beta = SimpleNamespace(messages=_Messages(script))

    @property
    def requests(self):
        return self.beta.messages.requests


class QuietUI:
    def __init__(self):
        self.warnings, self.notes = [], []

    def text(self, s): pass
    def thinking(self, s): pass
    def end_message(self): pass
    def tool_call(self, name, args): pass
    def tool_result(self, out, err): pass
    def note(self, s): self.notes.append(s)
    def warn(self, s): self.warnings.append(s)
    def child(self): return self
