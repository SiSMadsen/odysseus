from pathlib import Path

def patch(path, old, new):
    p = Path(path)
    s = p.read_text()
    if s.count(old) != 1:
        raise SystemExit(f"STOP: expected text not found exactly once in {path}")
    p.write_text(s.replace(old, new))
    print("patched", path)

patch("src/ai_interaction.py",
'''                # Anthropic: match against hardcoded model list
                matched = None
                for am in ANTHROPIC_MODELS:
                    if model_name.lower() in am.lower() or am.lower() in model_name.lower():
                        matched = am
                        break
''',
'''                # Anthropic: match against the endpoint's stored models first,
                # then the built-in list. Exact matches win over partial ones.
                stored = _json_list(getattr(ep, "cached_models", None))
                candidates = stored + [m for m in ANTHROPIC_MODELS if m not in stored]
                want = model_name.lower()
                matched = next((m for m in candidates if m.lower() == want), None)
                if not matched:
                    matched = next(
                        (m for m in candidates if want in m.lower() or m.lower() in want),
                        None,
                    )
''')

patch("src/agent_tools/model_interaction_tools.py",
'''                model_ids = list(ANTHROPIC_MODELS)
''',
'''                try:
                    model_ids = json.loads(ep.cached_models or "[]") or list(ANTHROPIC_MODELS)
                except Exception:
                    model_ids = list(ANTHROPIC_MODELS)
''')
