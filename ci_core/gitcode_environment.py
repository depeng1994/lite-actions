"""Captured vLLM A3 container identity; no lookup-by-name during recovery."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ci_core.gitcode_contract import VLLM_BLUE_SMOKE_DOCKER, canonical


class EnvironmentError(ValueError):
    pass


@dataclass(frozen=True)
class ContainerBinding:
    host_id: str
    container_name: str
    container_id: str
    image_id: str
    image_ref: str

    def metadata(self):
        return asdict(self)

    @classmethod
    def from_node(cls, node):
        configured = node.get("environments", {}).get(VLLM_BLUE_SMOKE_DOCKER)
        if not isinstance(configured, dict):
            raise EnvironmentError("VLLM_CONTAINER_BINDING_MISSING")
        binding = cls(
            node["host_id"],
            VLLM_BLUE_SMOKE_DOCKER,
            configured.get("container_id"),
            configured.get("image_id"),
            configured.get("image_ref"),
        )
        if not isinstance(binding.container_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", binding.container_id
        ):
            raise EnvironmentError("VLLM_CONTAINER_ID_NOT_PINNED")
        if not isinstance(binding.image_id, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", binding.image_id
        ):
            raise EnvironmentError("VLLM_IMAGE_ID_NOT_PINNED")
        if (
            not isinstance(binding.image_ref, str)
            or not binding.image_ref
            or len(binding.image_ref) > 256
            or any(c.isspace() for c in binding.image_ref)
        ):
            raise EnvironmentError("VLLM_IMAGE_REF_INVALID")
        return binding

    def verify_inspection(self, inspection: dict, *, resolved_image_id: str):
        if (
            inspection.get("Id") != self.container_id
            or inspection.get("Name") != "/" + self.container_name
            or inspection.get("Image") != self.image_id
            or inspection.get("State", {}).get("Running") is not True
            or resolved_image_id != self.image_id
        ):
            raise EnvironmentError("VLLM_CONTAINER_IDENTITY_MISMATCH")
        return self

    def verify_recovery(self, captured: dict, inspection_by_id: dict):
        if canonical(captured) != canonical(self.metadata()):
            raise EnvironmentError("CAPTURED_CONTAINER_BINDING_CHANGED")
        # Recovery does not resolve the image tag or the container's current name
        # to a new object. ID and image must match the captured original object.
        if (
            inspection_by_id.get("Id") != self.container_id
            or inspection_by_id.get("Name") != "/" + self.container_name
            or inspection_by_id.get("Image") != self.image_id
        ):
            raise EnvironmentError("RECOVERY_CONTAINER_REPLACED_OR_MISSING")
        return self
