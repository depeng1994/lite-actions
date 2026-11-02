"""Reviewed, exact optional dependency profile; no remote or source execution."""

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ci_core.gitcode_contract import spec_digest
from ci_core.gitcode_dependencies import (
    OPTIONAL_GITLINKS,
    PROFILE,
    DependencyPolicyError,
    optional_gitlinks,
    validate_dependencies,
    validate_profile_binding,
)
from tests.test_gitcode_pipeline import HostingAPI


class DependencyTests(unittest.TestCase):
    def rows(self):
        return [
            dict(path=path, sha=sha, kind="gitlink", materialized=False)
            for path, sha in OPTIONAL_GITLINKS.items()
        ]

    def test_profile_exact_and_smoke_only(self):
        self.assertEqual(optional_gitlinks(None, "smoke", ["worker_os_blue"]), {})
        self.assertEqual(
            optional_gitlinks(
                PROFILE,
                "smoke",
                ["worker_os_blue", "dispatch_smoke_8p", "dispatch_smoke_16p"],
            ),
            OPTIONAL_GITLINKS,
        )
        for profile, pipeline, cases in [
            ("unknown", "smoke", ["worker_os_blue"]),
            (PROFILE, "nightly", ["worker_os_blue"]),
            (PROFILE, "smoke", ["dsv4_flash_a3_8p_example"]),
            (PROFILE, "smoke", []),
        ]:
            with self.assertRaises(DependencyPolicyError):
                optional_gitlinks(profile, pipeline, cases)

    def test_declaration_cannot_be_omitted_changed_or_added(self):
        valid = self.rows()
        validate_dependencies(PROFILE, "smoke", ["worker_os_blue"], valid)
        candidates = [[], valid + valid]
        for key, value in [
            ("path", "other/submodule"),
            ("sha", "a" * 40),
            ("materialized", True),
        ]:
            changed = copy.deepcopy(valid)
            changed[0][key] = value
            candidates.append(changed)
        for rows in candidates:
            with self.assertRaises(DependencyPolicyError):
                validate_dependencies(PROFILE, "smoke", ["worker_os_blue"], rows)
        with self.assertRaises(DependencyPolicyError):
            validate_dependencies(None, "smoke", ["worker_os_blue"], valid)

    def test_policy_version_binds_profile_both_directions(self):
        validate_profile_binding(None, "base-v1")
        validate_profile_binding(PROFILE, "base-v1:" + PROFILE)
        for profile, version in [
            (None, "base-v1:" + PROFILE),
            (PROFILE, "base-v1"),
            ("other", "base-v1"),
        ]:
            with self.assertRaises(DependencyPolicyError):
                validate_profile_binding(profile, version)

    def test_real_producer_reader_manifest_and_distinct_spec(self):
        with tempfile.TemporaryDirectory() as root:
            api = HostingAPI(Path(root))
            api.intent["selectors"] = api.intent["selectors"][:1]
            api.policy = replace(
                api.policy, policy_version=api.policy.policy_version + ":" + PROFILE
            )
            for item in (
                api.producer,
                api.reader,
                api.producer.reader,
                api.producer.bridge,
            ):
                item.policy = api.policy
            for item in (api.producer, api.reader, api.producer.reader):
                item.source_dependency_profile = PROFILE
            build = api.build_source

            def source(*args, **kwargs):
                self.assertEqual(kwargs["optional_gitlinks"], OPTIONAL_GITLINKS)
                handle, identity = build(*args, **kwargs)
                return replace(handle, dependencies=tuple(self.rows())), identity

            api.producer.source_builder = source
            plan = api.produce()
            old = copy.deepcopy(plan)
            old["policy_version"] = "old-policy-no-dependencies"
            self.assertNotEqual(spec_digest(plan), spec_digest(old))
            api.job.update(status="completed", conclusion="success")
            loaded = api.reader.load(100, workspace=Path(root) / "read", now=1100)
            self.assertEqual(list(loaded.source.dependencies), self.rows())
            api.reader.source_dependency_profile = None
            with self.assertRaises(DependencyPolicyError):
                api.reader.load(100, workspace=Path(root) / "read-rejected", now=1100)
