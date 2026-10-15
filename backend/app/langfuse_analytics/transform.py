"""Langfuse API records -> table rows. Pure functions (no I/O), unit-tested directly.

How the app's traces map to analytics (see app/observability.py, app/jobs.py, app/api/kbs.py, app/mcp_server.py):
- trace name = the operation: "KG Extraction", "KG Build", "Embedding", "Hybrid Ingestion", "Add Data" (knowledge base
  creation and ingestion jobs), "Chat", "RAG Chat", "Hybrid Chat", "multi_kb_<type>_chat", "MCP <tool>" (questions),
  "RAGAS Evaluation";
- knowledge base = trace metadata "knowledge_base" (set by trace_context / llm_context), else the root input's;
- tokens and cost = the trace's generations (LLM calls) and embeddings; Langfuse prices known models itself;
- what was asked: MCP tool traces and RAGAS evaluations carry it in the trace input ("question", "query" for
  search_documents, "cypher" for query_graph; app/mcp_server.py _trace_details); web chat traces carry only the
  operation and knowledge base, so their question is the "Question: ..." line of the first LLM prompt (app/chat.py
  builds every graph / RAG / hybrid prompt that way);
- source = where the operation came from: the web app, an MCP agent, a background job, or a RAGAS evaluation.
"""

import datetime as dt
import json
import re

from app.observability import WORKFLOW_NAMES

CATEGORIES = {
    "creation": "KB creation & ingestion",
    "rag": "RAG / chat",
    "evaluation": "Evaluation",
    "other": "Other",
}

_CREATION_OPS = ("kg_extraction", "kg_build", "add_data", "rag_ingest", "hybrid_ingest", "embedding")
_RAG_OPS = ("chat", "rag_chat", "hybrid_chat")
_CREATION = {*_CREATION_OPS, *(WORKFLOW_NAMES.get(op, op) for op in _CREATION_OPS)}
_RAG = {
    *_RAG_OPS,
    *(WORKFLOW_NAMES.get(op, op) for op in _RAG_OPS),
    "MCP ask_knowledge_base",
    "MCP search_documents",
    "MCP query_graph",
}
_EVALUATION = {"RAGAS Evaluation"}
_KEPT_METADATA = ("knowledge_base", "operation", "job_id", "source")  # never prompts, answers or SDK internals
_ROLES = {"system", "human", "user", "ai", "assistant", "tool"}
_HUMAN_ROLES = {"human", "user"}
_QUESTION = "Question: "
# what app/chat.py appends after the question (Cypher retry, answer prompt)
_QUESTION_ENDS = ("\n\nYour previous query:", "\n\nQuery used (only to understand the columns):", "\n\nResults (")
_TRIMMED = re.compile(r"\.\.\. \[[\d,]+ more characters\]$")  # app/observability.py _trim marker
QUESTION_MAX_CHARS = 5000  # MCP accepts Cypher up to 5,000 characters
# trace input key -> request kind (what the person or agent sent)
_REQUEST_KEYS = (("question", "question"), ("query", "search"), ("cypher", "cypher"))
SOURCES = {"web": "Web app", "mcp": "MCP agents", "job": "Background jobs", "evaluation": "Evaluation"}


def category_of(name: str | None) -> str:
    if not name:
        return "other"
    if name in _CREATION:
        return "creation"
    if name in _RAG or name.startswith("multi_kb_"):
        return "rag"
    if name in _EVALUATION:
        return "evaluation"
    return "other"


def source_of(name: str | None, category: str) -> str:
    if name and name.startswith("MCP "):
        return "mcp"
    if category == "evaluation":
        return "evaluation"
    if category == "creation":
        return "job"
    return "web"


def request_of(trace: dict) -> tuple[str | None, str | None]:
    """(text, kind) the trace input says was asked: an MCP question / search query / Cypher, a RAGAS question."""
    data = _as_dict(trace.get("input"))
    for key, kind in _REQUEST_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return _TRIMMED.sub("…", value.strip())[:QUESTION_MAX_CHARS], kind
    return None, None


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _text(value) -> str | None:
    """Metadata values may come back JSON-encoded ('"name"') depending on the Langfuse version."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            value = str(json.loads(value)).strip()
        except ValueError:
            value = value[1:-1].strip()
    return value or None


def kb_of(trace: dict) -> str | None:
    for source in (_as_dict(trace.get("metadata")), _as_dict(trace.get("input"))):
        for key in ("knowledge_base", "kb_name"):
            name = _text(source.get(key))
            if name:
                return name
    return None


def parse_time(value) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def trace_row(trace: dict, keep_text: bool = True) -> dict:
    metadata = _as_dict(trace.get("metadata"))
    kb_name = kb_of(trace)
    kept = {k: _text(metadata.get(k)) for k in _KEPT_METADATA if _text(metadata.get(k))}
    if kb_name:
        kept["knowledge_base"] = kb_name
    latency = trace.get("latency")
    cost = _num(trace.get("totalCost"))
    name = _text(trace.get("name"))
    category = category_of(name)
    question, kind = request_of(trace) if category in ("rag", "evaluation") else (None, None)
    return {
        "id": trace["id"],
        "name": name,
        "category": category,
        "source": source_of(name, category),
        "question": question if keep_text else None,
        "request_kind": kind,
        "kb_name": kb_name,
        "user_id": _text(trace.get("userId")),
        "session_id": _text(trace.get("sessionId")),
        "environment": _text(trace.get("environment")),
        "tags": [t for t in trace.get("tags") or [] if isinstance(t, str)],
        "metadata": kept,
        "trace_timestamp": parse_time(trace.get("timestamp")),
        "latency_ms": _num(latency) * 1000 if isinstance(latency, (int, float)) else None,  # API: seconds
        "langfuse_cost": cost,
        "total_cost": cost,
        "html_path": _text(trace.get("htmlPath")),
    }


def usage_of(observation: dict) -> tuple[int, int, int]:
    """(input, output, total) tokens from usageDetails, the older `usage` object or the top-level token fields."""
    sources = (observation.get("usageDetails") or {}, observation.get("usage") or {}, observation)

    def pick(*keys) -> int:
        for source in sources:
            for key in keys:
                value = source.get(key) if isinstance(source, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    return int(value)
        return 0

    tokens_in = pick("input", "input_tokens", "prompt_tokens", "promptTokens")
    tokens_out = pick("output", "output_tokens", "completion_tokens", "completionTokens")
    total = pick("total", "total_tokens", "totalTokens") or tokens_in + tokens_out
    return tokens_in, tokens_out, total


def cost_of(observation: dict, tokens_in: int, tokens_out: int, prices: dict) -> tuple[float, float, float, bool]:
    """(input, output, total) USD and whether it is an estimate from ANALYTICS_MODEL_PRICES (Langfuse had none)."""
    details = observation.get("costDetails") or {}
    cost_in = _num(details.get("input")) or _num(observation.get("calculatedInputCost"))
    cost_out = _num(details.get("output")) or _num(observation.get("calculatedOutputCost"))
    total = _num(details.get("total")) or _num(observation.get("calculatedTotalCost")) or cost_in + cost_out
    if total > 0:
        return cost_in, cost_out, total, False
    price = prices.get(observation.get("model") or "") or {}
    cost_in = tokens_in * _num(price.get("input")) / 1_000_000
    cost_out = tokens_out * _num(price.get("output")) / 1_000_000
    return cost_in, cost_out, cost_in + cost_out, cost_in + cost_out > 0


def _human_texts(value, depth: int = 0):
    """Texts of the user / human messages in a generation input: LangChain (role, text) pairs, chat-style
    {"role", "content"} dicts, JSON strings of either, or plain text. System prompts are skipped."""
    if depth > 6:
        return
    if isinstance(value, str):
        if value.lstrip()[:1] in ("[", "{"):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, (list, dict)):
                yield from _human_texts(parsed, depth + 1)
                return
        yield value
    elif isinstance(value, (list, tuple)):
        if len(value) == 2 and all(isinstance(v, str) for v in value) and value[0].lower() in _ROLES:
            if value[0].lower() in _HUMAN_ROLES:
                yield value[1]
            return
        for item in value:
            yield from _human_texts(item, depth + 1)
    elif isinstance(value, dict):
        role = str(value.get("role") or value.get("type") or "").lower()
        if role in _ROLES and isinstance(value.get("content"), str):
            if role in _HUMAN_ROLES:
                yield value["content"]
            return
        for item in value.values():
            yield from _human_texts(item, depth + 1)


def _question_in(text: str) -> str | None:
    pos = text.rfind("\n" + _QUESTION)  # the question is appended last, after history / passages
    if pos >= 0:
        start = pos + 1 + len(_QUESTION)
    elif text.startswith(_QUESTION):
        start = len(_QUESTION)
    else:
        return None
    question = text[start:]
    for end in _QUESTION_ENDS:
        question = question.split(end, 1)[0]
    question = _TRIMMED.sub("…", question.strip())
    return question[:QUESTION_MAX_CHARS] or None


def question_of(observation: dict) -> str | None:
    """The user's question from an LLM call's prompt, or None (not a chat prompt, or content not captured)."""
    if (_text(observation.get("type")) or "").upper() != "GENERATION":
        return None
    for text in _human_texts(observation.get("input")):
        question = _question_in(text)
        if question:
            return question
    return None


def observation_row(observation: dict, prices: dict | None = None) -> dict:
    tokens_in, tokens_out, total_tokens = usage_of(observation)
    cost_in, cost_out, cost, estimated = cost_of(observation, tokens_in, tokens_out, prices or {})
    start, end = parse_time(observation.get("startTime")), parse_time(observation.get("endTime"))
    obs_type = (_text(observation.get("type")) or "").upper() or None
    return {
        "id": observation["id"],
        "trace_id": observation["traceId"],
        "parent_observation_id": _text(observation.get("parentObservationId")),
        "type": obs_type,
        "name": _text(observation.get("name")),
        "model": _text(observation.get("model")),
        "level": _text(observation.get("level")),
        "status_message": (_text(observation.get("statusMessage")) or "")[:500] or None,
        "start_time": start,
        "end_time": end,
        "latency_ms": (end - start).total_seconds() * 1000 if start and end else None,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "total_tokens": total_tokens,
        "input_cost": cost_in,
        "output_cost": cost_out,
        "total_cost": cost,
        "cost_estimated": estimated,
    }
