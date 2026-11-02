"""Versioned GitCode PR evidence, never a native Check or merge approval.

No tester/reviewer approval APIs, comment scanning, redirects, or blind POST
retry. A lost create response remains UNKNOWN until explicitly reconciled;
the same execution cannot silently create another comment. Platform Check
binding and actual GitCode Actions Job outcome remain separate release gates.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

from ci_core.gitcode_api import API_ROOT, GitCodeAPIError, GitCodeClient
from ci_core.gitcode_checks import CreationJournal
from ci_core.gitcode_contract import (
    VerifiedRequest,
    aggregate_results,
    canonical,
    digest,
    load_json,
    positive,
)


class PublicationError(ValueError):
    pass


class GitCodeResultClient(GitCodeClient):
    def __init__(
        self, *args, allow_comment_publication=False, write_transport=None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        if type(allow_comment_publication) is not bool:
            raise PublicationError("INVALID_PUBLICATION_SWITCH")
        self.allow_comment_publication = allow_comment_publication
        self._write_transport = write_transport

    @staticmethod
    def comment_id(value):
        if type(value) is int:
            return str(positive(value))
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", value
        ):
            raise PublicationError("INVALID_GITCODE_COMMENT_ID")
        return value

    def comment(self, ident):
        return self._get(f"/repos/{self.repo}/pulls/comments/{self.comment_id(ident)}")

    def create_comment(self, number, body):
        if not self.allow_comment_publication:
            raise PublicationError("GITCODE_PUBLICATION_DISABLED")
        positive(number)
        if not isinstance(body, str) or not 1 <= len(body.encode()) <= 60000:
            raise PublicationError("GITCODE_COMMENT_BODY_LIMIT")
        self.repository()
        path = f"/repos/{self.repo}/pulls/{number}/comments"
        payload = {"body": body}
        if self._write_transport is not None:
            result = self._write_transport("POST", path, payload)
        else:
            request = urllib.request.Request(
                API_ROOT + path,
                method="POST",
                data=canonical(payload),
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "lite-actions-gitcode-v4",
                },
            )
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    result = load_json(response.read(256 * 1024 + 1))
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                raise GitCodeAPIError(
                    "GITCODE_COMMENT_HTTP_ERROR", status=code
                ) from None
            except (OSError, TimeoutError):
                raise GitCodeAPIError("GITCODE_COMMENT_OUTCOME_UNKNOWN") from None
        if not isinstance(result, dict) or result.get("body") != body:
            raise PublicationError("GITCODE_CREATED_COMMENT_IDENTITY_MISMATCH")
        return self.comment_id(result.get("id"))


class VersionedGitCodePublisher:
    def __init__(self, client, journal: CreationJournal, *, actor):
        self.client, self.journal, self.actor = client, journal, actor

    def publish(self, verified, receipt, results, *, current):
        if not isinstance(verified, VerifiedRequest):
            raise PublicationError("VERIFIED_REQUEST_REQUIRED")
        plan = verified.plan
        if plan["pr"]["repo_id"] != self.client.repo_id:
            raise PublicationError("PUBLICATION_REPOSITORY_MISMATCH")
        if self.client.authenticated_actor() != self.actor:
            raise PublicationError("PUBLICATION_OPERATOR_MISMATCH")
        if len(results) != len(plan["cases"]) or {
            r.get("case_id") for r in results
        } != {c["case_id"] for c in plan["cases"]}:
            raise PublicationError("COMPLETE_CASE_EVIDENCE_REQUIRED")
        fresh = current()
        if type(fresh) is not bool:
            raise PublicationError("FRESH_AUTHORITY_BOOLEAN_REQUIRED")
        outcome = aggregate_results(plan, receipt, results, current=fresh)
        if outcome["test_result"] == "PENDING":
            raise PublicationError("COMPLETE_CASE_EVIDENCE_REQUIRED")
        key = digest({"plan": plan["plan_digest"], "receipt": receipt})
        owns, remembered = self.journal.begin(key)
        if remembered is None:
            if not owns:
                raise PublicationError("COMMENT_CREATE_UNKNOWN_DO_NOT_REPOST")
            envelope = {
                "protocol": "lite-actions-gitcode-versioned-result-v1",
                "publication_id": key,
                "historical_execution_evidence": True,
                "not_current_merge_authorization": True,
                "outcome": outcome,
                "github_run": plan["github"],
                "plan_digest": plan["plan_digest"],
            }
            body = (
                "TorchTitan GitCode Forward — versioned execution evidence\n"
                "Not a native Check, tester approval, or atomic merge permission.\n"
                "This immutable record is historical; Head/Base and authorization may change.\n\n"
                "~~~json\n" + canonical(envelope).decode() + "\n~~~"
            )
            ident = self.client.create_comment(plan["pr"]["number"], body)
            remembered = {"id": ident, "body": body, "publication_id": key}
            # Persist returned ID before a verifying GET can fail.
            self.journal.bind(key, remembered)
        if remembered["publication_id"] != key:
            raise PublicationError("COMMENT_JOURNAL_IDENTITY_CHANGED")
        comment = self.client.comment(remembered["id"])
        actual_ids = {str(comment.get("id")), str(comment.get("discussion_id"))}
        if (
            remembered["id"] not in actual_ids
            or comment.get("body") != remembered["body"]
            or (comment.get("user") or {}).get("login") != self.actor
        ):
            raise PublicationError("PUBLISHED_COMMENT_IDENTITY_MISMATCH")
        # Recheck after network publication. Never return cached historical PASS
        # as current after an observed revocation or authority outage.
        fresh = current()
        if type(fresh) is not bool:
            raise PublicationError("FRESH_AUTHORITY_BOOLEAN_REQUIRED")
        return {
            "comment_id": remembered["id"],
            "outcome": aggregate_results(plan, receipt, results, current=fresh),
            "native_check": False,
        }
