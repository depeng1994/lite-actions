"""Existing-container selection is explicit and checked before pool activation."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "onboarding", ROOT / "scripts/preflight_v2.py"
)
onboarding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(onboarding)
IMAGE = "image:tag"
FIRST = "a" * 64
SECOND = "b" * 64


class OnboardingTests(unittest.TestCase):
    def remote(self, host, command, **kw):
        if command.startswith("python3"):
            return SimpleNamespace(stdout="fingerprint\n")
        if command.startswith("docker image"):
            return SimpleNamespace(stdout="sha256:expected\n")
        if command.startswith("docker ps"):
            return SimpleNamespace(stdout=FIRST + "\n" + SECOND + "\n")
        ident = command.rsplit(" ", 1)[1]
        return SimpleNamespace(
            stdout=json.dumps(
                dict(Id=ident, Image="sha256:expected", State=dict(Running=True))
            )
        )

    def test_ambiguous_running_containers_require_explicit_selection(self):
        with patch.object(onboarding, "run", side_effect=self.remote):
            with self.assertRaisesRegex(ValueError, "found 2"):
                onboarding.probe_node(dict(ssh="node"), IMAGE)

    def test_explicit_existing_container_is_pinned_and_verified(self):
        with (
            patch.object(onboarding, "run", side_effect=self.remote),
            patch.object(onboarding, "execution_node") as verify,
        ):
            result = onboarding.probe_node(
                dict(ssh="node"), IMAGE, container_ref=SECOND
            )
        self.assertEqual(
            result["binding"], dict(container_id=SECOND, image_id="sha256:expected")
        )
        self.assertEqual(
            verify.call_args.args[0]["environments"][IMAGE]["container_id"], SECOND
        )

    def test_preconfigured_binding_does_not_require_unique_image_candidate(self):
        node = dict(
            ssh="node",
            environments={IMAGE: dict(container_id=FIRST, image_id="sha256:expected")},
        )
        with (
            patch.object(onboarding, "run", side_effect=self.remote) as remote,
            patch.object(onboarding, "execution_node"),
        ):
            self.assertEqual(
                onboarding.probe_node(node, IMAGE)["binding"]["container_id"], FIRST
            )
        self.assertFalse(
            any(c.args[1].startswith("docker ps") for c in remote.call_args_list)
        )
