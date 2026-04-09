"""Deployment mode: hosted (centralized) vs ephemeral (BYOK, no DB cache expectation)."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

DeployMode = Literal["hosted", "ephemeral"]


@lru_cache
def get_deploy_mode() -> DeployMode:
    raw = (os.getenv("TRADINGAGENTS_DEPLOY_MODE") or "hosted").strip().lower()
    if raw == "ephemeral":
        return "ephemeral"
    return "hosted"


def is_ephemeral_deploy() -> bool:
    return get_deploy_mode() == "ephemeral"


def is_hosted_deploy() -> bool:
    return get_deploy_mode() == "hosted"


def clear_deploy_mode_cache() -> None:
    """For tests only."""
    get_deploy_mode.cache_clear()
