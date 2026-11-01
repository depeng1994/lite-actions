"""Real-Git merge-source tests. No platform calls and no NPU execution."""

import gzip
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ci_core.gitcode_source import (
    MergeSourceBuilder,
    SourceError,
    extract_verified_source,
    require_ref,
    sha256_file,
)


class MergeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "fixture"
        self.repo.mkdir()
        self.git("init", "--initial-branch=base")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write("shared.txt", "common\n")
        self.commit("initial")
        self.initial = self.sha()
        self.git("branch", "head")
        self.version = subprocess.check_output(["git", "--version"], text=True).strip().removeprefix("git version ")
        self.counter = 0

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()

    def sha(self):
        return self.git("rev-parse", "HEAD")

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.sha()

    def builder(self, **kwargs):
        self.counter += 1
        builder = MergeSourceBuilder(self.root / f"build-{self.counter}", expected_git_version=self.version, **kwargs)
        # Local-object injection is test-only; production fetch_ref rejects file URLs.
        subprocess.run(["git", "--git-dir=" + str(builder.repo), "fetch", "--no-tags", str(self.repo),
                        "+refs/heads/*:refs/heads/*"], check=True, capture_output=True)
        return builder

    def branches(self):
        self.write("base-only.txt", "latest base\n")
        base = self.commit("base change")
        self.git("checkout", "head")
        self.write("head-only.txt", "PR change\n")
        head = self.commit("head change")
        return base, head

    def test_merges_both_sides_and_is_reproducible(self):
        base, head = self.branches()
        one = self.builder().build(head_sha=head, base_sha=base)
        two = self.builder().build(head_sha=head, base_sha=base)
        self.assertEqual(one.tested_merge_sha, two.tested_merge_sha)
        self.assertEqual(one.tested_tree_sha, two.tested_tree_sha)
        self.assertEqual(one.bundle_sha256, two.bundle_sha256)
        dest = extract_verified_source(one, self.root / "source")
        self.assertEqual((dest / "base-only.txt").read_text(), "latest base\n")
        self.assertEqual((dest / "head-only.txt").read_text(), "PR change\n")
        self.assertNotEqual(one.tested_merge_sha, head)
        self.assertNotEqual(one.tested_merge_sha, base)
        self.assertNotIn("bundle_path", one.metadata())

    def test_conflict_never_publishes_bundle(self):
        self.write("shared.txt", "base\n")
        base = self.commit("base conflict")
        self.git("checkout", "head")
        self.write("shared.txt", "head\n")
        head = self.commit("head conflict")
        builder = self.builder()
        with self.assertRaisesRegex(SourceError, "MERGE_CONFLICT"):
            builder.build(head_sha=head, base_sha=base)
        self.assertFalse((builder.root / "source.tar.gz").exists())

    def test_equal_head_base_has_one_parent_and_exact_tree(self):
        builder = self.builder()
        handle = builder.build(head_sha=self.initial, base_sha=self.initial)
        parents = builder._git("show", "-s", "--format=%P", handle.tested_merge_sha).decode().strip()
        self.assertEqual(parents, self.initial)
        self.assertEqual(handle.tested_tree_sha, self.git("rev-parse", "HEAD^{tree}"))

    def test_fast_forward_still_builds_explicit_merge_snapshot(self):
        self.write("added.txt", "new\n")
        head = self.commit("fast forward")
        builder = self.builder()
        handle = builder.build(head_sha=head, base_sha=self.initial)
        self.assertEqual(handle.tested_tree_sha, self.git("rev-parse", "HEAD^{tree}"))
        parents = builder._git("show", "-s", "--format=%P", handle.tested_merge_sha).decode().strip()
        self.assertEqual(parents, self.initial + " " + head)

    def test_export_attributes_cannot_hide_or_substitute_payload(self):
        self.write(".gitattributes", "payload.py export-ignore\nversion.txt export-subst\n")
        self.write("payload.py", "raise RuntimeError('must not execute')\n")
        self.write("version.txt", "$Format:%H$\n")
        head = self.commit("hostile export attributes")
        handle = self.builder().build(head_sha=head, base_sha=self.initial)
        dest = extract_verified_source(handle, self.root / "source")
        self.assertTrue((dest / "payload.py").is_file())
        self.assertEqual((dest / "version.txt").read_text(), "$Format:%H$\n")

    def test_pr_python_and_custom_merge_driver_never_execute(self):
        marker = self.root / "EXECUTED"
        self.write("sitecustomize.py", f"open({str(marker)!r},'w').write('bad')\n")
        self.write(".gitattributes", "shared.txt merge=evil\n")
        head = self.commit("untrusted source")
        self.git("config", "merge.evil.driver", f"touch {marker}")
        handle = self.builder().build(head_sha=head, base_sha=self.initial)
        extract_verified_source(handle, self.root / "source")
        self.assertFalse(marker.exists())

    def test_git_environment_does_not_inherit_control_secrets(self):
        os.environ["LITE_ACTIONS_TEST_SECRET"] = "do-not-forward"
        self.addCleanup(os.environ.pop, "LITE_ACTIONS_TEST_SECRET", None)
        builder = self.builder()
        self.assertNotIn("LITE_ACTIONS_TEST_SECRET", builder.env)
        self.assertNotIn("GIT_CONFIG_COUNT", builder.env)
        self.assertEqual(builder.env["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_unknown_version_and_remote_are_rejected(self):
        with self.assertRaisesRegex(SourceError, "GIT_VERSION_NOT_PINNED"):
            MergeSourceBuilder(self.root / "bad-version", expected_git_version="0.0.0")
        builder = self.builder()
        for url in ("file:///tmp/repo", "https://evil.invalid/a/b.git", "https://user:secret@gitcode.com/a/b.git"):
            with self.subTest(url=url), self.assertRaisesRegex(SourceError, "UNTRUSTED_GIT_REMOTE"):
                builder.fetch_ref(url=url, ref="head", expected_sha=self.initial, role="head")

    def test_ref_rejects_git_syntax_and_traversal(self):
        for ref in ("--upload-pack=bad", "a..b", "a//b", "a.lock", ".hidden", "x/", "a^{}", "a\\b", "a/../b"):
            with self.subTest(ref=ref), self.assertRaises(SourceError):
                require_ref(ref)
        self.assertEqual(require_ref("feature/change-1"), "feature/change-1")

    def test_lfs_and_symlinks_fail_closed(self):
        self.write("lfs.bin", "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 1\n")
        head = self.commit("lfs")
        with self.assertRaisesRegex(SourceError, "UNMATERIALIZED_LFS_OBJECT"):
            self.builder().build(head_sha=head, base_sha=self.initial)
        self.git("reset", "--hard", self.initial)
        (self.repo / "link").symlink_to("shared.txt")
        head = self.commit("symlink")
        with self.assertRaisesRegex(SourceError, "UNSUPPORTED_SOURCE_FILE_MODE"):
            self.builder().build(head_sha=head, base_sha=self.initial)

    def test_content_limits_and_cache_digest(self):
        with self.assertRaisesRegex(SourceError, "SOURCE_SIZE_LIMIT"):
            self.builder(max_file_bytes=1).build(head_sha=self.initial, base_sha=self.initial)
        handle = self.builder().build(head_sha=self.initial, base_sha=self.initial)
        Path(handle.bundle_path).write_bytes(b"corrupt")
        with self.assertRaisesRegex(SourceError, "SOURCE_BUNDLE_DIGEST_MISMATCH"):
            extract_verified_source(handle, self.root / "no-extract")
        self.assertFalse((self.root / "no-extract").exists())

    def malicious_handle(self, entries):
        handle = self.builder().build(head_sha=self.initial, base_sha=self.initial)
        bundle = Path(handle.bundle_path)
        with bundle.open("wb") as out, gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w|") as archive:
                for entry, content in entries:
                    archive.addfile(entry, io.BytesIO(content))
        return replace(handle, bundle_sha256=sha256_file(bundle))

    def test_archive_traversal_links_duplicate_and_bombs_are_rejected(self):
        for name in ("../escape", "/absolute", "a/../../escape", ".git/config"):
            entry = tarfile.TarInfo(name)
            handle = self.malicious_handle([(entry, b"")])
            with self.subTest(name=name), self.assertRaises(SourceError):
                extract_verified_source(handle, self.root / "bad")
            self.assertFalse((self.root / "bad").exists())
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        with self.assertRaisesRegex(SourceError, "UNSAFE_ARCHIVE_MEMBER"):
            extract_verified_source(self.malicious_handle([(link, b"")]), self.root / "linkbad")
        file = tarfile.TarInfo("file")
        with self.assertRaisesRegex(SourceError, "SOURCE_DUPLICATE"):
            extract_verified_source(self.malicious_handle([(file, b""), (file, b"")]), self.root / "duplicate")
        file.size = 4
        with self.assertRaisesRegex(SourceError, "SOURCE_SIZE_LIMIT"):
            extract_verified_source(self.malicious_handle([(file, b"data")]), self.root / "bomb", max_unpacked_bytes=1)

    def test_existing_destination_is_not_overwritten(self):
        handle = self.builder().build(head_sha=self.initial, base_sha=self.initial)
        dest = self.root / "occupied"
        dest.mkdir()
        (dest / "keep").write_text("keep")
        with self.assertRaisesRegex(SourceError, "SOURCE_DESTINATION_EXISTS"):
            extract_verified_source(handle, dest)
        self.assertEqual((dest / "keep").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
