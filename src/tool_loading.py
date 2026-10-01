# src/tool_loading.py
"""
The `load_tools` agent tool: lets the model pull a tool that wasn't selected
for this turn into its tool list, from the next round on, instead of the user
having to send a second message (docs/creator-plan.md, "all tools in one turn").

This module only validates and describes. The agent loop does the actual
loading: after a successful call it unions `result["loaded"]` into the turn's
selected tools, the same way a skill's requires_toolsets are unlocked. Tools
that are disabled for this run (privileges, admin settings, plan mode, public
users, tool policy) are refused here and filtered again by the loop.

MCP tools aren't covered yet; only Odysseus's own tools can be loaded.
"""
import json
import re
from typing import Dict, Iterable, List, Optional, Set

_MAX_PER_CALL = 15
_MAX_LISTED = 120


def _catalog() -> Dict[str, str]:
    """name -> one-line description, for every built-in tool."""
    # Through src.agent_tools, like the agent loop: importing src.tool_schemas
    # first is a circular import.
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    out: Dict[str, str] = {}
    for schema in FUNCTION_TOOL_SCHEMAS:
        fn = schema.get("function") or {}
        name = fn.get("name")
        if name:
            out[name] = str(fn.get("description") or "")
    for name, desc in BUILTIN_TOOL_DESCRIPTIONS.items():
        out[name] = str(desc or out.get(name, ""))
    try:
        from src.agent_tools import TOOL_TAGS
        for name in TOOL_TAGS:
            out.setdefault(name, "")
    except Exception:
        pass
    out.pop("load_tools", None)
    return out


def _first_sentence(text: str, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return cut if len(cut) <= limit else cut[: limit - 1] + "…"


def _usage(name: str) -> str:
    """How to call a tool: its prompt section if it has one (fence-style
    models need that), else its description."""
    try:
        from src.agent_loop import TOOL_SECTIONS
        section = TOOL_SECTIONS.get(name)
        if section:
            return section.strip()
    except Exception:
        pass
    return f"- `{name}` — {_catalog().get(name, '')}"


def _parse(content) -> dict:
    if isinstance(content, dict):
        return content
    raw = (content or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {"_invalid": True}
    # Bare "send_email, read_email" → names.
    return {"names": [n for n in re.split(r"[\s,]+", raw) if n]}


def do_load_tools(content, disabled_tools: Optional[Iterable[str]] = None,
                  tool_policy=None) -> dict:
    """Args (JSON): {"names": [...]} to load tools, {"search": "email"} to
    list matching ones, or {} to list everything that can be loaded."""
    args = _parse(content)
    if args.get("_invalid"):
        return {"error": 'Invalid arguments. Use {"names": ["tool_a"]} or {"search": "word"}.',
                "exit_code": 1}
    disabled: Set[str] = set(disabled_tools or ())
    catalog = _catalog()

    def allowed(name: str) -> bool:
        if name in disabled:
            return False
        try:
            if tool_policy is not None and tool_policy.blocks(name):
                return False
        except Exception:
            return False
        return True

    names = args.get("names")
    if isinstance(names, str):
        names = [names]
    if names:
        names = [str(n).strip() for n in names if str(n).strip()][:_MAX_PER_CALL]
        loaded: List[str] = []
        unknown: List[str] = []
        refused: List[str] = []
        for name in names:
            if name not in catalog:
                unknown.append(name)
            elif not allowed(name):
                refused.append(name)
            elif name not in loaded:
                loaded.append(name)
        lines = []
        if loaded:
            lines.append("Loaded — you can call these from your next step:")
            lines += [_usage(n) for n in loaded]
        if refused:
            lines.append("Not available in this run (switched off or not allowed): " + ", ".join(refused))
        if unknown:
            lines.append("No such tool: " + ", ".join(unknown)
                         + '. Use load_tools with {"search": "<word>"} to find tool names.')
        return {"output": "\n".join(lines), "loaded": loaded,
                "exit_code": 0 if loaded else 1}

    terms = [t for t in str(args.get("search") or "").lower().split() if t]
    rows = []
    for name in sorted(catalog):
        if not allowed(name):
            continue
        desc = catalog[name]
        hay = f"{name} {desc}".lower()
        if terms and not all(t in hay for t in terms):
            continue
        rows.append(f"- {name}: {_first_sentence(desc)}")
    if not rows:
        return {"output": "No loadable tools match." if terms else "No tools can be loaded.",
                "loaded": [], "exit_code": 0}
    head = (f"Tools matching {args.get('search')!r}" if terms else "Tools you can load") \
        + ' (call load_tools with {"names": [...]} to use them):'
    more = f"\n…and {len(rows) - _MAX_LISTED} more; narrow with search." if len(rows) > _MAX_LISTED else ""
    return {"output": head + "\n" + "\n".join(rows[:_MAX_LISTED]) + more,
            "loaded": [], "exit_code": 0}
