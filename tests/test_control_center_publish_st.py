import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

PUBLISHER = Path(__file__).resolve().parents[1] / "control-center/deploy/publish-web.py"


def frontend_bundle(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
    return stream.getvalue()


class ControlCenterPublishST(unittest.TestCase):
    def test_bad_release_preserves_live_page_and_next_release_publishes(self):
        with tempfile.TemporaryDirectory() as temp:
            web = Path(temp)
            dist = web / "dist"
            (dist / "assets").mkdir(parents=True)
            old = b'<script src="/assets/index-old.js"></script>'
            new = b'<script src="/assets/index-new.js"></script>'
            index = dist / "index.html"
            index.write_bytes(old)
            (dist / "assets/index-old.js").write_bytes(b"old")

            def deploy(entries):
                return subprocess.run(
                    [sys.executable, str(PUBLISHER), "--dist-dir", str(dist)],
                    input=frontend_bundle(entries),
                    capture_output=True,
                    check=False,
                )

            for invalid in [
                [("assets/../../service", b"unexpected")],
                [("assets/index-old.js", b"collision")],
            ]:
                with self.subTest(invalid=invalid[0][0]):
                    result = deploy([("index.html", new), *invalid])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(index.read_bytes(), old)
                    self.assertEqual(
                        (dist / "assets/index-old.js").read_bytes(), b"old"
                    )
                    self.assertFalse((web / "service").exists())

            release = [("index.html", new), ("assets/index-new.js", b"new")]
            for _ in range(2):
                result = deploy(release)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(index.read_bytes(), new)
            self.assertEqual((web / ".index-before-web-publish.html").read_bytes(), old)
            self.assertEqual((dist / "assets/index-new.js").read_bytes(), b"new")
            self.assertEqual((dist / "assets/index-old.js").read_bytes(), b"old")
