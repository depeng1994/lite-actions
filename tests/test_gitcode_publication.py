"""No live GitCode mutation: immutable versioned publication fault fixtures."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from ci_core.gitcode_checks import CreationJournal
from ci_core.gitcode_contract import VerifiedRequest, canonical, source_identity
from ci_core.gitcode_publication import (
    GitCodeResultClient,
    PublicationError,
    VersionedGitCodePublisher,
)
from tests.test_gitcode_contract import fixture, receipt_for


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan, _ = fixture()
        self.verified = VerifiedRequest(canonical(self.plan))
        self.receipt = receipt_for(self.plan)
        self.results = [
            {
                "case_id": case["case_id"],
                "receipt": self.receipt,
                "identity": source_identity(self.plan),
                "status": "PASS",
                "exit_code": 0,
                "cleanup_verified": True,
                "summary": [],
            }
            for case in self.plan["cases"]
        ]
        self.client = Mock(repo_id=self.plan["pr"]["repo_id"])
        self.client.authenticated_actor.return_value = "depeng1994"
        self.comments = {}

        def create(number, body):
            self.comments["comment-1"] = {
                "id": 12,
                "discussion_id": "comment-1",
                "body": body,
                "user": {"login": "depeng1994"},
            }
            return "comment-1"

        self.client.create_comment.side_effect = create
        self.client.comment.side_effect = lambda ident: self.comments[ident]
        self.journal = CreationJournal(Path(self.tmp.name) / "published.sqlite3")
        self.publisher = VersionedGitCodePublisher(
            self.client, self.journal, actor="depeng1994"
        )

    def publish(self, current=lambda: True, results=None):
        return self.publisher.publish(
            self.verified,
            self.receipt,
            self.results if results is None else results,
            current=current,
        )

    def test_idempotent_fixed_comment_and_no_native_merge_permission(self):
        result = self.publish()
        self.publish()
        self.client.create_comment.assert_called_once()
        self.assertFalse(result["native_check"])
        self.assertEqual(result["outcome"]["merge_gate"], "NOT_PROVIDED")
        self.assertIn("historical", self.comments["comment-1"]["body"])

    def test_lost_create_response_never_reposts_after_restart(self):
        self.client.create_comment.side_effect = OSError("unknown")
        with self.assertRaises(OSError):
            self.publish()
        self.publisher = VersionedGitCodePublisher(
            self.client, self.journal, actor="depeng1994"
        )
        with self.assertRaisesRegex(PublicationError, "UNKNOWN_DO_NOT_REPOST"):
            self.publish()
        self.client.create_comment.assert_called_once()

    def test_lost_get_recovers_exact_returned_id(self):
        original = self.client.comment.side_effect
        self.client.comment.side_effect = OSError("unavailable")
        with self.assertRaises(OSError):
            self.publish()
        self.client.comment.side_effect = original
        self.publish()
        self.client.create_comment.assert_called_once()

    def test_changed_body_wrong_author_or_id_fail_closed(self):
        self.publish()
        for key, value in (
            ("body", "tampered"),
            ("user", {"login": "other"}),
            ("discussion_id", "different"),
        ):
            original = self.comments["comment-1"][key]
            self.comments["comment-1"][key] = value
            with self.assertRaisesRegex(PublicationError, "IDENTITY_MISMATCH"):
                self.publish()
            self.comments["comment-1"][key] = original

    def test_revocation_during_publication_returns_stale_not_historical_pass(self):
        values = iter((True, False))
        result = self.publish(current=lambda: next(values))
        self.assertEqual(result["outcome"]["test_result"], "STALE")
        result = self.publish(current=lambda: False)
        self.assertEqual(result["outcome"]["test_result"], "STALE")

    def test_incomplete_results_never_publish_even_if_stale(self):
        with self.assertRaisesRegex(PublicationError, "COMPLETE_CASE"):
            self.publish(current=lambda: False, results=[])
        self.client.create_comment.assert_not_called()

    def test_low_level_publication_disabled_by_default(self):
        client = GitCodeResultClient("owner/repo", "42", transport=lambda path: {})
        with self.assertRaisesRegex(PublicationError, "DISABLED"):
            client.create_comment(1, "test")


if __name__ == "__main__":
    unittest.main()
