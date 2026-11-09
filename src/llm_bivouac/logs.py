"""Per-deployment logging: every line says which instance it is about.

The instance is the first 8 characters of the deployment ID, the same suffix as
the OVH app's name, so `docker compose logs proxy | grep <instance>` follows one
deployment from its start to its teardown.
"""

from __future__ import annotations

import logging
from typing import Any

_base = logging.getLogger("llm_bivouac.instance")


class InstanceLogger(logging.LoggerAdapter):
    def process(self, msg: Any, kwargs: Any) -> tuple[str, Any]:
        return f"[{self.extra['instance']}] {msg}", kwargs


def short_id(deployment_id: str) -> str:
    return deployment_id[:8]


def instance_log(deployment_id: str) -> InstanceLogger:
    return InstanceLogger(_base, {"instance": short_id(deployment_id)})
