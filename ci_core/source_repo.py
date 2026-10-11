"""Only the upstream organization is an authorized CI model source."""
import os

REPO = "torchtitan-npu/torchtitan-npu"
if os.environ.get("A3_CI_REPOSITORY", REPO) != REPO:
    raise ValueError("unsupported CI source repository")
