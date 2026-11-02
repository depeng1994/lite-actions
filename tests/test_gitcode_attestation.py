"""Synthetic ephemeral RSA fixtures only; no live key or Action proof."""

import base64
import copy
import hashlib
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ci_core.gitcode_app import OpenSSLSigner
from ci_core.gitcode_attestation import (
    AttestationError,
    AttestationPolicy,
    VerifiedAttestation,
    create_envelope,
    verify_envelope,
)
from ci_core.gitcode_contract import canonical
from tests.test_gitcode_contract import fixture


@unittest.skipUnless(Path("/usr/bin/openssl").is_file(), "OpenSSL unavailable")
class AttestationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.key = cls.root / "synthetic-dedicated-key.pem"
        cls.public = cls.root / "synthetic-public.pem"
        subprocess.run(
            [
                "/usr/bin/openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(cls.key),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        cls.key.chmod(0o600)
        subprocess.run(
            [
                "/usr/bin/openssl",
                "pkey",
                "-in",
                str(cls.key),
                "-pubout",
                "-out",
                str(cls.public),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        cls.public.chmod(0o644)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        _, context = fixture()
        control = replace(context["policy"], github_repo_id=1210038933)
        self.intent = copy.deepcopy(context["intent"])
        self.intent["control_repo_id"] = control.github_repo_id
        self.intent["selectors"] = [{"test_id": "worker_os_blue"}]
        self.intent["bridge"].update(
            bridge_action_run="a" * 32,
            bridge_nonce="synthetic_nonce_123456789",
        )
        self.original_event = {
            "id": self.intent["bridge"]["bridge_event_id"],
            "created_at": 1000,
            "event_kind": "pull_request",
            "source_snapshot_id": self.intent["bridge"]["source_snapshot_id"],
            "source_snapshot": copy.deepcopy(self.intent["pr"]),
        }
        self.policy = AttestationPolicy(
            control=control,
            key_id="synthetic-key-v1",
            public_key_file=self.public,
            public_key_sha256=hashlib.sha256(self.public.read_bytes()).hexdigest(),
            issuer_repo_id="42",
            head_repo_id="42",
            workflow_path=".gitcode/workflows/ci-github-bridge.yml",
            workflow_source_sha="e" * 40,
            workflow_id="b" * 32,
            allowed_event_kinds=("pull_request",),
            allowed_platform_events=("PULL_REQUEST",),
            enabled=True,
        )
        self.arguments = dict(
            original_event=self.original_event,
            run_id="a" * 32,
            run_attempt=1,
            capture_job_id="capture-job-1",
            run_head_sha="f" * 40,
            run_head_branch="target-control",
            platform_event="PULL_REQUEST",
            workflow_id=self.policy.workflow_id,
            workflow_path=self.policy.workflow_path,
            workflow_source_sha=self.policy.workflow_source_sha,
            key_id=self.policy.key_id,
            issuer_repo_id="42",
            actor="depeng1994",
            issued_at=1020,
            signer=OpenSSLSigner(self.key),
        )

    def sign(self, **kwargs):
        return create_envelope(self.intent, **{**self.arguments, **kwargs})

    def test_valid_signature_preserves_original_identity_and_separate_heads(self):
        envelope = self.sign()
        result = verify_envelope(envelope, self.intent, self.policy, now=1050)
        self.assertIsInstance(result, VerifiedAttestation)
        self.assertEqual(result.original_event_at, 1000)
        self.assertEqual(result.gitcode_run_id, "a" * 32)
        self.assertEqual(result.workflow_id, "b" * 32)
        self.assertEqual(result.run_head_sha, "f" * 40)
        self.assertEqual(result.workflow_source_sha, "e" * 40)
        self.assertEqual(result.source_snapshot, self.intent["pr"])
        self.assertEqual(
            result.envelope_digest, hashlib.sha256(canonical(envelope)).hexdigest()
        )
        envelope["payload"]["original_event"]["source_snapshot"]["head_sha"] = "0" * 40
        self.assertNotEqual(result.source_snapshot["head_sha"], "0" * 40)

    def test_original_event_age_not_fresh_issued_at_controls_acceptance(self):
        envelope = self.sign(issued_at=1350, max_original_event_age_seconds=3600)
        with self.assertRaisesRegex(AttestationError, "ORIGINAL_EVENT_EXPIRED"):
            verify_envelope(envelope, self.intent, self.policy, now=1350)
        with self.assertRaisesRegex(AttestationError, "TOO_OLD_TO_SIGN"):
            self.sign(issued_at=1350)

    def test_disabled_policy_never_reads_public_key_or_verifies(self):
        envelope = self.sign()
        with patch("ci_core.gitcode_attestation._verify_signature") as verifier:
            with self.assertRaisesRegex(AttestationError, "DISABLED"):
                verify_envelope(
                    envelope, self.intent, replace(self.policy, enabled=False), now=1050
                )
            verifier.assert_not_called()

    def test_missing_original_metadata_and_boolean_times_fail_closed(self):
        for field in self.original_event:
            event = {k: v for k, v in self.original_event.items() if k != field}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.sign(original_event=event)
        with self.assertRaises(ValueError):
            self.sign(original_event={**self.original_event, "created_at": True})
        with self.assertRaises(ValueError):
            self.sign(run_attempt=True)
        with self.assertRaisesRegex(AttestationError, "PLATFORM_ID"):
            self.sign(run_id=123)

    def test_current_snapshot_cannot_replace_original_snapshot(self):
        event = copy.deepcopy(self.original_event)
        event["source_snapshot"]["base_sha"] = "d" * 40
        with self.assertRaisesRegex(AttestationError, "BINDING_MISMATCH"):
            self.sign(original_event=event)

    def test_intent_selector_control_and_nonce_tampering_is_detected(self):
        envelope = self.sign()
        for field, value in (
            ("selectors", [{"test_id": "dispatch_smoke_16p"}]),
            ("control_sha", "0" * 40),
        ):
            changed = copy.deepcopy(self.intent)
            changed[field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(AttestationError, "IDENTITY_MISMATCH"),
            ):
                verify_envelope(envelope, changed, self.policy, now=1050)
        envelope["payload"]["nonce"] = "tampered_nonce_123456789"
        with self.assertRaisesRegex(AttestationError, "BINDING_MISMATCH"):
            verify_envelope(envelope, self.intent, self.policy, now=1050)

    def test_validly_shaped_payload_mutation_still_fails_signature(self):
        envelope = self.sign()
        envelope["payload"]["action"]["head_sha"] = "d" * 40
        with self.assertRaisesRegex(AttestationError, "SIGNATURE_INVALID"):
            verify_envelope(envelope, self.intent, self.policy, now=1050)

    def test_key_workflow_and_api_event_pins_are_exact(self):
        envelope = self.sign()
        for kwargs in (
            {"key_id": "other-key"},
            {"workflow_source_sha": "0" * 40},
            {"workflow_id": "other-workflow"},
            {"allowed_platform_events": ("PUSH",)},
            {"public_key_sha256": "0" * 64},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(AttestationError):
                verify_envelope(
                    envelope, self.intent, replace(self.policy, **kwargs), now=1050
                )

    def test_exact_fork_head_is_allowed_not_arbitrary_fork(self):
        self.intent["pr"]["head_repo_id"] = "77"
        self.original_event["source_snapshot"]["head_repo_id"] = "77"
        envelope = self.sign()
        policy = replace(self.policy, head_repo_id="77")
        self.assertEqual(
            verify_envelope(envelope, self.intent, policy, now=1050).source_snapshot[
                "head_repo_id"
            ],
            "77",
        )
        with self.assertRaisesRegex(AttestationError, "RELEASE_MISMATCH"):
            verify_envelope(envelope, self.intent, self.policy, now=1050)

    def test_future_issuance_and_expired_deadline_fail(self):
        envelope = self.sign(issued_at=1100)
        with self.assertRaisesRegex(AttestationError, "EXPIRED_OR_FUTURE"):
            verify_envelope(envelope, self.intent, self.policy, now=1050)
        with self.assertRaisesRegex(AttestationError, "EXPIRED_OR_FUTURE"):
            verify_envelope(
                self.sign(), self.intent, self.policy, now=self.intent["deadline"]
            )

    def test_duplicate_json_keys_oversize_and_signature_encoding_fail(self):
        with self.assertRaises(ValueError):
            verify_envelope(
                b'{"schema":1,"schema":1}', self.intent, self.policy, now=1050
            )
        envelope = self.sign()
        envelope["signature"] = base64.b64encode(b"x" * 128).decode()
        with self.assertRaisesRegex(AttestationError, "SIGNATURE_ENCODING"):
            verify_envelope(envelope, self.intent, self.policy, now=1050)
        with self.assertRaises(ValueError):
            verify_envelope(b"x" * 65537, self.intent, self.policy, now=1050)

    def test_capture_source_key_is_stable_across_changed_payload_and_key_id(self):
        first = verify_envelope(self.sign(), self.intent, self.policy, now=1050)
        self.intent["selectors"] = [{"test_id": "dispatch_smoke_16p"}]
        self.intent["pr"]["head_sha"] = "d" * 40
        self.intent["bridge"].update(
            bridge_nonce="different_nonce_123456789", bridge_event_id="different-event"
        )
        self.original_event.update(
            id="different-event", source_snapshot=copy.deepcopy(self.intent["pr"])
        )
        envelope = self.sign(key_id="rotated-key-id")
        second = verify_envelope(
            envelope,
            self.intent,
            replace(self.policy, key_id="rotated-key-id"),
            now=1050,
        )
        self.assertNotEqual(first.envelope_digest, second.envelope_digest)
        self.assertEqual(first.source_key, second.source_key)
        self.assertEqual(first.capture_job_id, "capture-job-1")
        # A missing original CAPTURE job ID has no fallback to run/nonce/time.
        del envelope["payload"]["action"]["capture_job_id"]
        with self.assertRaises(ValueError):
            verify_envelope(
                envelope,
                self.intent,
                replace(self.policy, key_id="rotated-key-id"),
                now=1050,
            )

    def test_external_issuer_is_not_inferred_from_allowed_source_fork(self):
        with self.assertRaisesRegex(AttestationError, "TRUSTED_TARGET_ISSUER"):
            replace(self.policy, issuer_repo_id="77")
        external = replace(self.policy, issuer_repo_id="77", allow_external_issuer=True)
        envelope = self.sign(issuer_repo_id="77")
        self.assertEqual(
            verify_envelope(envelope, self.intent, external, now=1050).issuer_repo_id,
            "77",
        )
        with self.assertRaisesRegex(AttestationError, "PINNED_EMITTER"):
            verify_envelope(envelope, self.intent, self.policy, now=1050)

    def test_public_key_only_and_clean_subprocess_environment(self):
        envelope = self.sign()
        with self.assertRaisesRegex(AttestationError, "FINGERPRINT"):
            verify_envelope(
                envelope,
                self.intent,
                replace(
                    self.policy,
                    public_key_file=self.key,
                    public_key_sha256=hashlib.sha256(self.key.read_bytes()).hexdigest(),
                ),
                now=1050,
            )
        real_run = subprocess.run
        with (
            patch.dict(os.environ, SYNTHETIC_SECRET="must-not-propagate"),
            patch("ci_core.gitcode_attestation.subprocess.run", wraps=real_run) as run,
        ):
            verify_envelope(envelope, self.intent, self.policy, now=1050)
            self.assertNotIn("SYNTHETIC_SECRET", run.call_args.kwargs["env"])
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertNotIn(str(self.key), str(run.call_args.args))


if __name__ == "__main__":
    unittest.main()
