"""Minimal case definition shared by the Lite Actions self-smoke adapter."""

from dataclasses import dataclass, field


@dataclass
class OverrideDefinitions:
    test_name: str
    test_descr: str
    ci_kind: str
    nnodes: int
    ngpu: int
    timeout: int
    ci_overrides: dict[str, str] = field(default_factory=dict)
    env_vars: dict[str, str] | None = None
    check_loss: bool = True
