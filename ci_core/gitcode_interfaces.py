"""Static interface checks for the approved TorchTitan merge-source catalog.

A SHA proves content identity, not that required interfaces exist. Only parse
text/AST here; never load model modules, run hooks, invoke shell recipes, or let
a proposed file choose devices, containers, timeouts, or trusted entrypoints.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

from ci_core.gitcode_source import SourceHandle, extract_verified_source

TORCHTITAN_CONTROL_REPO_ID = 1210038933
SMOKE_PAYLOAD = ".github/scripts/lite_actions/gitcode/smoke_payload.py"
TRAIN_PATHS = {
    "dsv4_flash_a3_8p_example": "examples/deepseek_v4/debug/deepseek_v4_flash_8p_cpt_4k_a3.sh",
    "dsv4_flash_a3_8p_adamw": "examples/deepseek_v4/debug/deepseek_v4_flash_8p_cpt_4k_a3.sh",
    "dsv4_flash_a3_16p_example": "examples/deepseek_v4/deepseek_v4_flash_cpt_4k_a3.sh",
}
SMOKE_CASES = {"dispatch_smoke_8p", "dispatch_smoke_16p"}
CASES = {"worker_os_blue"} | SMOKE_CASES | set(TRAIN_PATHS)
RESERVED_ROOT_NAMES = {"sitecustomize.py", "usercustomize.py", "_lite_gitcode_train.py", "_lite_gitcode_train"}


class SourceInterfaceError(ValueError):
    pass


def required_paths(test_ids: list[str]) -> set[str]:
    if not test_ids or len(test_ids) != len(set(test_ids)) or set(test_ids) - CASES:
        raise SourceInterfaceError("UNAPPROVED_TORCHTITAN_SOURCE_CASE")
    paths = set()
    if set(test_ids) & SMOKE_CASES:
        paths.add(SMOKE_PAYLOAD)
    for test_id in set(test_ids) & set(TRAIN_PATHS):
        paths.update({TRAIN_PATHS[test_id], "torchtitan_npu/train.py", "torchtitan_npu/models/deepseek_v4/__init__.py"})
        paths.add("scripts/run_train_multinodes.sh" if test_id == "dsv4_flash_a3_16p_example" else "scripts/run_train.sh")
    return paths


def inspect_directory(root: Path, test_ids: list[str]) -> None:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise SourceInterfaceError("VERIFIED_SOURCE_DIRECTORY_REQUIRED")
    root = root.resolve()
    if any((root / name).exists() or (root / name).is_symlink() for name in RESERVED_ROOT_NAMES):
        raise SourceInterfaceError("SOURCE_SHADOWS_TRUSTED_BOOTSTRAP")
    required = required_paths(test_ids)
    for name in required:
        relative = PurePosixPath(name)
        path = root.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root) or path.stat().st_size > 10 * 1024**2:
            raise SourceInterfaceError("SOURCE_INTERFACE_MISSING_OR_UNSAFE: " + name)
    if SMOKE_PAYLOAD in required:
        payload = root / SMOKE_PAYLOAD
        if payload.stat().st_size > 65536:
            raise SourceInterfaceError("SMOKE_PAYLOAD_INTERFACE_SIZE_LIMIT")
        try:
            tree = ast.parse(payload.read_bytes())
        except (SyntaxError, ValueError, UnicodeError, RecursionError):
            raise SourceInterfaceError("SMOKE_PAYLOAD_SYNTAX_INVALID") from None
        versions = [node.value for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "PAYLOAD_API_VERSION" for target in node.targets)]
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "all_reduce"]
        if (len(versions) != 1 or not isinstance(versions[0], ast.Constant)
            or type(versions[0].value) is not int or versions[0].value != 1
            or len(functions) != 1):
            raise SourceInterfaceError("SMOKE_PAYLOAD_API_1_REQUIRED")


def inspect_bundle(handle: SourceHandle, test_ids: list[str], destination: Path) -> None:
    extract_verified_source(handle, destination)
    inspect_directory(destination, test_ids)
