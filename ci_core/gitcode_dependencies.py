"""Explicit non-runtime dependency exception for the reviewed smoke profile."""

from ci_core.gitcode_contract import canonical

PROFILE = "torchtitan-smoke-agent-metadata-v1"
OPTIONAL_GITLINKS = {
    "third_party/agent-skills/KPBot": "17bbb2b6be6b1b416e23dd8af5b9bc7f15f3c2bc"
}
SMOKE_CASES = frozenset({"worker_os_blue", "dispatch_smoke_8p", "dispatch_smoke_16p"})


class DependencyPolicyError(ValueError):
    pass


def optional_gitlinks(profile, pipeline, test_ids):
    if profile is None:
        return {}
    if profile != PROFILE:
        raise DependencyPolicyError("UNKNOWN_SOURCE_DEPENDENCY_PROFILE")
    if pipeline != "smoke" or not test_ids or set(test_ids) - SMOKE_CASES:
        raise DependencyPolicyError("OPTIONAL_AGENT_DEPENDENCY_SMOKE_ONLY")
    return dict(OPTIONAL_GITLINKS)


def validate_dependencies(profile, pipeline, test_ids, dependencies):
    expected = [
        {"path": path, "kind": "gitlink", "sha": sha, "materialized": False}
        for path, sha in optional_gitlinks(profile, pipeline, test_ids).items()
    ]
    if canonical(dependencies) != canonical(expected):
        raise DependencyPolicyError("SOURCE_DEPENDENCY_DECLARATION_MISMATCH")


def validate_profile_binding(profile, policy_version):
    bound = isinstance(policy_version, str) and policy_version.endswith(":" + PROFILE)
    if profile not in (None, PROFILE) or bound != (profile == PROFILE):
        raise DependencyPolicyError("SOURCE_DEPENDENCY_POLICY_VERSION_MISMATCH")
