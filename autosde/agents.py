"""Agent configuration: prompts and per-agent settings, edited from the web UI or by hand.

Settings live in `prompts/agents.json` beside the prompt files, so they are versioned
with the code. Missing entries fall back to the defaults below.
"""

import json
import os
import re
from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"
CONFIG = "agents.json"  # inside PROMPTS

SPECIALISTS = {
    "python": [".py"],
    "javascript": [".js", ".mjs", ".cjs", ".jsx"],
    "typescript": [".ts", ".tsx"],
    "java": [".java"],
    "go": [".go"],
    "csharp": [".cs"],
    "general": [],  # catch-all for every other file; cannot be disabled
}
ROLES = ("coordinator", *SPECIALISTS, "aggregator", "chat")
SYSTEM_PROMPTS = {"coordinator": "coordinator.md", "aggregator": "aggregator.md",
                  "specialist": "specialist.md", "chat": "chat.md",
                  "chunk_summary": "chunk_summary.md"}
_EXT = re.compile(r"\.[A-Za-z0-9_+-]{1,16}")
_MODEL = re.compile(r"[A-Za-z0-9._:-]{1,128}")
LANGSMITH_ENDPOINTS = ("https://api.smith.langchain.com", "https://eu.api.smith.langchain.com")
_PROJECT = re.compile(r"[A-Za-z0-9 ._:-]{1,100}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")


CORE = ("coordinator", "aggregator", "chat")  # not file specialists


class ConfigError(ValueError):
    pass


def defaults():
    agents = {name: {"enabled": True, "extensions": list(ext), "model": None}
              for name, ext in SPECIALISTS.items()}
    agents["coordinator"] = {"enabled": True, "model": None}
    agents["aggregator"] = {"enabled": True, "model": None}
    agents["chat"] = {"enabled": True, "model": None}  # answers questions on project history
    for item in agents.values():
        item["trace"] = True
    return {"agents": agents, "langsmith": {"enabled": False, "project": "autosde",
                                            "endpoint": LANGSMITH_ENDPOINTS[0]}}


def validate(config):
    """Return a clean config or raise ConfigError with a readable message."""
    if not isinstance(config, dict) or not isinstance(config.get("agents"), dict):
        raise ConfigError("Config needs an `agents` object.")
    clean, seen = defaults(), {}
    for name, item in config["agents"].items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(item, dict):
            raise ConfigError(f"Invalid agent entry: {name!r}")
        entry = clean["agents"].setdefault(name, {"enabled": True, "extensions": [], "model": None,
                                                         "trace": True})
        if "knowledge" in item:
            ids = item["knowledge"]
            if not isinstance(ids, list) or len(ids) > 20 or any(type(i) is not int or i < 1 for i in ids):
                raise ConfigError(f"{name}: knowledge must be a list of knowledge base ids.")
            entry["knowledge"] = sorted(set(ids))
        if "trace" in item:
            if not isinstance(item["trace"], bool):
                raise ConfigError(f"{name}: trace must be true or false.")
            entry["trace"] = item["trace"]
        if "enabled" in item:
            if not isinstance(item["enabled"], bool):
                raise ConfigError(f"{name}: enabled must be true or false.")
            entry["enabled"] = item["enabled"]
        if "model" in item:
            model = item["model"] or None
            if model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
                raise ConfigError(f"{name}: invalid model id.")
            entry["model"] = model
        if "extensions" in item and name not in CORE:
            extensions = item["extensions"]
            if not isinstance(extensions, list) or len(extensions) > 50:
                raise ConfigError(f"{name}: extensions must be a list.")
            cleaned = []
            for ext in extensions:
                ext = str(ext).strip().lower()
                ext = ext if ext.startswith(".") else "." + ext
                if not _EXT.fullmatch(ext):
                    raise ConfigError(f"{name}: invalid extension {ext!r}.")
                cleaned.append(ext)
            entry["extensions"] = cleaned
    langsmith = config.get("langsmith", {})
    if not isinstance(langsmith, dict):
        raise ConfigError("langsmith must be an object.")
    if "enabled" in langsmith:
        if not isinstance(langsmith["enabled"], bool):
            raise ConfigError("langsmith.enabled must be true or false.")
        clean["langsmith"]["enabled"] = langsmith["enabled"]
    if "project" in langsmith:
        if not isinstance(langsmith["project"], str) or not _PROJECT.fullmatch(langsmith["project"]):
            raise ConfigError("LangSmith project: letters, digits, spaces and ._:- only.")
        clean["langsmith"]["project"] = langsmith["project"]
    if "endpoint" in langsmith:
        if langsmith["endpoint"] not in LANGSMITH_ENDPOINTS:
            raise ConfigError("Unknown LangSmith endpoint.")
        clean["langsmith"]["endpoint"] = langsmith["endpoint"]
    for name, entry in clean["agents"].items():
        for ext in entry.get("extensions", []):
            if ext in seen and seen[ext] != name:
                raise ConfigError(f"{ext} is claimed by both {seen[ext]} and {name}.")
            seen[ext] = name
    for fixed in ("general", *CORE):
        clean["agents"][fixed]["enabled"] = True  # the team cannot run without these
    return clean


# Where agent settings and prompts live: files next to this module (None, the local app) or
# a store with get_config/put_config/get_prompt/put_prompt (DynamoDB in AWS).
BACKEND = None
# Whether a LangSmith key is available beyond the environment (set by autosde.cloud).
LANGSMITH_KEY_AVAILABLE = None


def load_config(path=None):
    if BACKEND is not None and path is None:
        try:
            saved = BACKEND.get_config()
            return validate(saved) if saved else defaults()
        except (ValueError, ConfigError):
            return defaults()
    try:
        return validate(json.loads(Path(path or PROMPTS / CONFIG).read_text()))
    except FileNotFoundError:
        return defaults()
    except (ValueError, ConfigError):
        return defaults()  # a broken file never stops reviews


def save_config(config, path=None):
    clean = validate(config)
    if BACKEND is not None and path is None:
        BACKEND.put_config(clean)
    else:
        Path(path or PROMPTS / CONFIG).write_text(json.dumps(clean, indent=2) + "\n")
    return clean


def read_text(name):
    """A prompt file by its name under prompts/ (e.g. `specialist.md`, `focus/python.md`).
    In AWS the stored version wins; the bundled file is the default."""
    if BACKEND is not None:
        saved = BACKEND.get_prompt(name)
        if saved is not None:
            return saved
    path = PROMPTS / name
    return path.read_text() if path.exists() else ""


def specialists(config):
    """Enabled specialists in order, with their extensions."""
    return {name: item.get("extensions", []) for name, item in config["agents"].items()
            if name not in CORE and item.get("enabled")}


def prompt_file(kind, agent=None):
    """Path of an editable prompt: a system prompt or a specialist's focus."""
    if kind == "focus":
        if not isinstance(agent, str) or not _NAME.fullmatch(agent):
            raise ConfigError("Invalid agent name.")
        return PROMPTS / "focus" / f"{agent}.md"
    if kind not in SYSTEM_PROMPTS:
        raise ConfigError("Unknown prompt.")
    return PROMPTS / SYSTEM_PROMPTS[kind]


def read_prompt(kind, agent=None):
    return read_text(prompt_file(kind, agent).relative_to(PROMPTS).as_posix())


def write_prompt(kind, text, agent=None):
    if not isinstance(text, str) or len(text) > 20_000:
        raise ConfigError("Prompt must be text under 20,000 characters.")
    if kind != "focus" and not text.strip():
        raise ConfigError("System prompts cannot be empty.")
    if kind == "specialist" and "{agent}" not in text:
        raise ConfigError("The specialist prompt must keep the {agent} placeholder.")
    path = prompt_file(kind, agent)
    text = text if text.endswith("\n") else text + "\n"
    if BACKEND is not None:
        BACKEND.put_prompt(path.relative_to(PROMPTS).as_posix(), text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def snapshot():
    """Everything the Agents page edits, in one object."""
    config = load_config()
    return {
        "config": config,
        "langsmith_key": bool(os.environ.get("LANGSMITH_API_KEY"))
                         or bool(LANGSMITH_KEY_AVAILABLE and LANGSMITH_KEY_AVAILABLE()),
        "prompts": {kind: read_prompt(kind) for kind in SYSTEM_PROMPTS},
        "focus": {name: read_prompt("focus", name) for name in config["agents"]
                  if name not in CORE},
    }


def update(body):
    """Validate the whole edit first, then write; a bad field changes nothing."""
    config = validate(body.get("config", load_config()))
    prompts = body.get("prompts") or {}
    focus = body.get("focus") or {}
    if not isinstance(prompts, dict) or not isinstance(focus, dict):
        raise ConfigError("prompts and focus must be objects.")
    writes = [(kind, text, None) for kind, text in prompts.items()]
    writes += [("focus", text, name) for name, text in focus.items()]
    for kind, text, agent in writes:
        prompt_file(kind, agent)
        if not isinstance(text, str) or len(text) > 20_000:
            raise ConfigError("Prompt must be text under 20,000 characters.")
        if kind != "focus" and not text.strip():
            raise ConfigError("System prompts cannot be empty.")
        if kind == "specialist" and "{agent}" not in text:
            raise ConfigError("The specialist prompt must keep the {agent} placeholder.")
    for kind, text, agent in writes:
        write_prompt(kind, text, agent)
    save_config(config)
    return snapshot()
