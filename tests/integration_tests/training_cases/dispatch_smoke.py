# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""CPU-only acceptance profile for RFC 19, not the default release profile."""

from tests.integration_tests import OverrideDefinitions

SMOKE_DEFAULTS = {
    "worker": "blue",
    "target_pool": "a3_shared_blue",
    "docker": "cann_master_torchtitan_master_xyj",
}
SMOKE_PROFILES = {
    "dispatch_smoke": [
        "worker_os_blue",
        # "dispatch_smoke_8p",
        # "dispatch_smoke_16p",
        # "dispatch_smoke_8p",
    ],
}


def build_smoke_list() -> list[OverrideDefinitions]:
    return [
        OverrideDefinitions(
            test_name="worker_os_blue",
            test_descr="RFC 19 Blue CPU-only operating system probe",
            ci_kind="worker_probe",
            ci_overrides={"worker": "blue"},
            nnodes=0,
            ngpu=0,
            timeout=90,
            check_loss=False,
        )
    ]


def resolve_smoke_profile(profile: str = "dispatch_smoke") -> list[OverrideDefinitions]:
    if profile != "dispatch_smoke":
        raise ValueError("unexpected smoke profile")
    return build_smoke_list()
