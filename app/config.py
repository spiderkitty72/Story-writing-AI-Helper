"""Loading of settings, agents and workflows from YAML files."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", Path(__file__).resolve().parent.parent / "config"))
CONFIG_FILES = ("settings.yaml", "agents.yaml", "workflows.yaml")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


@dataclass
class ChunkSettings:
    chunk_tokens: int = 1200
    overlap_tokens: int = 300
    knowledge_tokens: int = 600
    chars_per_token: int = 4


@dataclass
class Agent:
    name: str
    description: str = ""
    mode: str = "edit"  # edit | report
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    system: str = ""
    task: str = "{instructions}"
    # Size of each piece of text sent to this agent (tokens). Small local
    # models do much better with small pieces. Falls back to settings.yaml.
    chunk_tokens: int | None = None
    # Allowed answer length compared with the input, e.g. [0.85, 1.2]. An
    # answer outside this range is retried once, then the original is kept.
    length_ratio: list[float] | None = None
    # Require the answer to have the same number of paragraphs (proofreading).
    keep_paragraphs: bool = False


@dataclass
class WorkflowStep:
    agent: str
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    instructions: str = ""


@dataclass
class Workflow:
    name: str
    description: str = ""
    steps: list[WorkflowStep] = field(default_factory=list)


@dataclass
class Config:
    data_dir: Path
    default_provider: str
    providers: dict[str, dict[str, Any]]
    chunking: ChunkSettings
    agents: dict[str, Agent]
    workflows: dict[str, Workflow]


class ConfigError(ValueError):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return data


def parse_config(settings: dict, agents: dict, workflows: dict, base_dir: Path | None = None) -> Config:
    settings = _expand_env(settings)
    base_dir = base_dir or CONFIG_DIR.parent

    data_dir = Path(settings.get("data_dir") or "./data")
    if not data_dir.is_absolute():
        data_dir = (base_dir / data_dir).resolve()

    providers = settings.get("providers") or {}
    if not isinstance(providers, dict):
        raise ConfigError("providers must be a mapping")
    for name, p in providers.items():
        if not isinstance(p, dict) or "type" not in p:
            raise ConfigError(f"provider '{name}' needs a 'type'")

    chunk_raw = settings.get("chunking") or {}
    chunking = ChunkSettings(**{k: int(v) for k, v in chunk_raw.items() if k in ChunkSettings.__dataclass_fields__})

    agent_map: dict[str, Agent] = {}
    for name, a in (agents.get("agents") or {}).items():
        if not isinstance(a, dict):
            raise ConfigError(f"agent '{name}' must be a mapping")
        unknown = set(a) - set(Agent.__dataclass_fields__)
        if unknown:
            raise ConfigError(f"agent '{name}' has unknown keys: {', '.join(sorted(unknown))}")
        agent = Agent(name=name, **a)
        if agent.mode not in ("edit", "report"):
            raise ConfigError(f"agent '{name}' mode must be 'edit' or 'report'")
        if agent.length_ratio is not None and (
            not isinstance(agent.length_ratio, list) or len(agent.length_ratio) != 2
            or not all(isinstance(x, (int, float)) for x in agent.length_ratio)
        ):
            raise ConfigError(f"agent '{name}' length_ratio must be a list like [0.8, 1.3]")
        if agent.provider and agent.provider not in providers:
            raise ConfigError(f"agent '{name}' uses unknown provider '{agent.provider}'")
        agent_map[name] = agent

    wf_map: dict[str, Workflow] = {}
    for name, w in (workflows.get("workflows") or {}).items():
        steps = []
        for s in (w or {}).get("steps") or []:
            if isinstance(s, str):
                s = {"agent": s}
            step = WorkflowStep(**s)
            if step.agent not in agent_map:
                raise ConfigError(f"workflow '{name}' uses unknown agent '{step.agent}'")
            steps.append(step)
        if not steps:
            raise ConfigError(f"workflow '{name}' has no steps")
        wf_map[name] = Workflow(name=name, description=(w or {}).get("description", ""), steps=steps)

    default_provider = settings.get("default_provider") or next(iter(providers), "echo")
    if providers and default_provider not in providers:
        raise ConfigError(f"default_provider '{default_provider}' is not defined")

    return Config(
        data_dir=data_dir,
        default_provider=default_provider,
        providers=providers or {"echo": {"type": "echo"}},
        chunking=chunking,
        agents=agent_map,
        workflows=wf_map,
    )


def load_config(config_dir: Path | None = None) -> Config:
    d = config_dir or CONFIG_DIR
    return parse_config(
        _read_yaml(d / "settings.yaml"),
        _read_yaml(d / "agents.yaml"),
        _read_yaml(d / "workflows.yaml"),
        base_dir=d.parent,
    )


def read_raw(name: str, config_dir: Path | None = None) -> str:
    if name not in CONFIG_FILES:
        raise ConfigError("unknown config file")
    p = (config_dir or CONFIG_DIR) / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_raw(name: str, text: str, config_dir: Path | None = None) -> Config:
    """Validate the new file together with the others, then save it."""
    if name not in CONFIG_FILES:
        raise ConfigError("unknown config file")
    d = config_dir or CONFIG_DIR
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML error: {e}") from e
    if not isinstance(parsed, dict):
        raise ConfigError("file must contain a mapping at the top level")
    parts = {n: _read_yaml(d / n) for n in CONFIG_FILES}
    parts[name] = parsed
    cfg = parse_config(parts["settings.yaml"], parts["agents.yaml"], parts["workflows.yaml"], base_dir=d.parent)
    (d / name).write_text(text, encoding="utf-8")
    return cfg
