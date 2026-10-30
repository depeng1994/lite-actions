"""Existing-container selection is explicit and checked before pool activation."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from ci_core import v2_config

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
                dict(
                    Id=ident,
                    Image="sha256:expected",
                    ImageRef="actual:tag",
                    State=dict(Running=True),
                )
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
            result["binding"],
            dict(
                container_id=SECOND, image_id="sha256:expected", image_ref="actual:tag"
            ),
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

    def test_logical_environment_resolves_pinned_node_image_reference(self):
        node = dict(
            ssh="node",
            environments={
                "shared_environment": dict(
                    container_id=FIRST,
                    image_id="sha256:expected",
                    image_ref="node_image:tag",
                )
            },
        )
        responses = [
            SimpleNamespace(
                stdout=json.dumps(
                    dict(Image="sha256:expected", State=dict(Running=True))
                )
            ),
            SimpleNamespace(stdout="sha256:expected\n"),
            SimpleNamespace(stdout="0\n"),
        ]
        with patch.object(v2_config, "run", side_effect=responses) as remote:
            resolved = v2_config.execution_node(node, "shared_environment")
        self.assertEqual(resolved["ssh"], "node/" + FIRST)
        self.assertTrue(remote.call_args_list[1].args[1].endswith("node_image:tag"))

    def test_retagged_node_image_is_rejected(self):
        node = dict(
            ssh="node",
            environments={
                IMAGE: dict(
                    container_id=FIRST,
                    image_id="sha256:expected",
                    image_ref="actual:tag",
                )
            },
        )
        with patch.object(
            v2_config,
            "run",
            side_effect=[
                SimpleNamespace(
                    stdout=json.dumps(
                        dict(Image="sha256:expected", State=dict(Running=True))
                    )
                ),
                SimpleNamespace(stdout="sha256:different\n"),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "IMAGE_TAG_CHANGED"):
                v2_config.execution_node(node, IMAGE)
