"""Package entry point: delegate to the maintained monolithic server.

The repository keeps `gemini_web2api.py` as the canonical implementation used by
`launch.py`. The older package modules are retained for source compatibility, but
running `python -m gemini_web2api` or the console script must not silently use a
stale server without the agent-focused planner/retry/validation logic.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_monolith():
    root = Path(__file__).resolve().parent.parent
    script = root / "gemini_web2api.py"
    spec = importlib.util.spec_from_file_location("gemini_for_agents_monolith", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load canonical server from {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    module = _load_monolith()
    return module.main()


if __name__ == "__main__":
    main()
