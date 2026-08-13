"""Lua script registry for the atomic rate-limit algorithms (Phase 4)."""

import importlib.resources

from sentinel.redis import ScriptLoader

TOKEN_BUCKET_SCRIPT = "token_bucket"
SLIDING_WINDOW_SCRIPT = "sliding_window"
SCRIPT_NAMES = (TOKEN_BUCKET_SCRIPT, SLIDING_WINDOW_SCRIPT)


def script_source(name: str) -> str:
    if name not in SCRIPT_NAMES:
        raise ValueError(f"unknown script {name!r}; expected one of {', '.join(SCRIPT_NAMES)}")
    package_root = importlib.resources.files("sentinel")
    return package_root.joinpath("lua", f"{name}.lua").read_text(encoding="utf-8")


async def load_scripts(loader: ScriptLoader) -> dict[str, str]:
    return {name: await loader.load(name, script_source(name)) for name in SCRIPT_NAMES}
