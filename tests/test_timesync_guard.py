"""Unit tests for worker clock synchronization guard."""

from __future__ import annotations

import email.utils
import io
import subprocess
import unittest
import urllib.error
from email.message import Message
from unittest.mock import MagicMock

from scripts import timesync_guard


class _FakeResponse:
    def __init__(self, headers: dict[str, str]):
        msg = Message()
        for key, value in headers.items():
            msg[key] = value
        self.headers = msg

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TimesyncGuardTests(unittest.TestCase):
    def test_parse_http_date_utc_and_offset(self):
        epoch = timesync_guard.parse_http_date("Wed, 25 Nov 2026 04:09:07 GMT")
        self.assertEqual(epoch, 1795579747.0)
        self.assertAlmostEqual(
            timesync_guard.calculate_offset(epoch, 1795579740.5),
            6.5,
        )
        self.assertAlmostEqual(
            timesync_guard.calculate_offset(epoch, 1795579750.25),
            -3.25,
        )

    def test_parse_http_date_rejects_missing_or_invalid(self):
        with self.assertRaises(ValueError):
            timesync_guard.parse_http_date(None)
        with self.assertRaises(ValueError):
            timesync_guard.parse_http_date("   ")
        with self.assertRaises(ValueError):
            timesync_guard.parse_http_date("not-a-valid-http-date")

    def test_fetch_remote_time_supports_405_date_header(self):
        headers = Message()
        headers["Date"] = "Wed, 25 Nov 2026 04:09:07 GMT"
        err = urllib.error.HTTPError(
            "https://lite-actions.ooguy.com/api/v1/public/overview",
            405,
            "Method Not Allowed",
            headers,
            io.BytesIO(b""),
        )
        times = iter([1795579745.0, 1795579745.4])

        def _opener(_req, timeout=10.0):
            raise err

        remote, local_mid, rtt = timesync_guard.fetch_remote_time(
            "https://lite-actions.ooguy.com/api/v1/public/overview",
            method="HEAD",
            opener=_opener,
            time_fn=lambda: next(times),
        )
        self.assertEqual(remote, 1795579747.0)
        self.assertAlmostEqual(local_mid, 1795579745.2, places=5)
        self.assertAlmostEqual(rtt, 0.4, places=5)

    def test_skips_stepping_when_ntp_synchronized(self):
        remote_epoch = 1795579800.0
        date_hdr = email.utils.formatdate(remote_epoch, usegmt=True)
        commands: list[list[str]] = []

        def _runner(cmd, **kwargs):
            commands.append(list(cmd))
            if cmd[:2] == ["timedatectl", "show"]:
                return _completed(cmd, returncode=0, stdout="yes\n")
            raise AssertionError(f"Unexpected command: {cmd}")

        result = timesync_guard.evaluate_and_sync(
            "https://lite-actions.ooguy.com/api/v1/public/overview",
            threshold=2.0,
            apply=True,
            opener=lambda req, timeout=10.0: _FakeResponse({"Date": date_hdr}),
            runner=_runner,
            time_fn=lambda: 1795579740.0,
        )
        self.assertTrue(result.ntp_synchronized)
        self.assertEqual(result.offset_seconds, 60.0)
        self.assertEqual(result.action, "skipped_ntp_synced")
        self.assertIsNone(result.applied_epoch)
        self.assertEqual(
            commands,
            [["timedatectl", "show", "-p", "NTPSynchronized", "--value"]],
        )

    def test_steps_clock_when_unsynchronized_and_beyond_threshold(self):
        remote_epoch = 1795579800.0
        date_hdr = email.utils.formatdate(remote_epoch, usegmt=True)
        commands: list[list[str]] = []

        def _runner(cmd, **kwargs):
            commands.append(list(cmd))
            if cmd == ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]:
                return _completed(cmd, returncode=1, stdout="")
            if cmd == ["timedatectl"]:
                return _completed(
                    cmd,
                    returncode=0,
                    stdout=(
                        "               Local time: Wed 2026-11-25 12:09:00 CST\n"
                        "System clock synchronized: no\n"
                        "              NTP service: inactive\n"
                    ),
                )
            if cmd[:3] == ["date", "-u", "-s"]:
                return _completed(cmd, returncode=0, stdout="")
            raise AssertionError(f"Unexpected command: {cmd}")

        result = timesync_guard.evaluate_and_sync(
            "https://lite-actions.ooguy.com/api/v1/public/overview",
            threshold=2.0,
            apply=True,
            opener=lambda req, timeout=10.0: _FakeResponse({"Date": date_hdr}),
            runner=_runner,
            time_fn=lambda: 1795579740.0,
        )
        self.assertFalse(result.ntp_synchronized)
        self.assertEqual(result.offset_seconds, 60.0)
        self.assertEqual(result.action, "adjusted")
        self.assertEqual(result.applied_epoch, 1795579800)
        self.assertEqual(
            commands[-1],
            ["date", "-u", "-s", "@1795579800"],
        )

    def test_skips_stepping_within_threshold(self):
        remote_epoch = 1795579800.0
        date_hdr = email.utils.formatdate(remote_epoch, usegmt=True)
        runner = MagicMock(return_value=_completed(["timedatectl"], stdout="no\n"))

        result = timesync_guard.evaluate_and_sync(
            "https://lite-actions.ooguy.com/api/v1/public/overview",
            threshold=2.0,
            apply=True,
            opener=lambda req, timeout=10.0: _FakeResponse({"Date": date_hdr}),
            runner=runner,
            time_fn=lambda: 1795579798.5,
        )
        self.assertEqual(result.offset_seconds, 1.5)
        self.assertEqual(result.action, "ok")
        self.assertIsNone(result.applied_epoch)
        self.assertEqual(runner.call_count, 1)

    def test_dry_run_does_not_step_even_when_unsynchronized(self):
        remote_epoch = 1795579800.0
        date_hdr = email.utils.formatdate(remote_epoch, usegmt=True)
        commands: list[list[str]] = []

        def _runner(cmd, **kwargs):
            commands.append(list(cmd))
            return _completed(cmd, returncode=0, stdout="no\n")

        result = timesync_guard.evaluate_and_sync(
            "https://lite-actions.ooguy.com/api/v1/public/overview",
            threshold=2.0,
            apply=False,
            opener=lambda req, timeout=10.0: _FakeResponse({"Date": date_hdr}),
            runner=_runner,
            time_fn=lambda: 1795579700.0,
        )
        self.assertEqual(result.action, "dry_run")
        self.assertIsNone(result.applied_epoch)
        self.assertEqual(len(commands), 1)

    def test_force_step_overrides_ntp_synchronized_guard(self):
        remote_epoch = 1795579800.0
        date_hdr = email.utils.formatdate(remote_epoch, usegmt=True)
        commands: list[list[str]] = []

        def _runner(cmd, **kwargs):
            commands.append(list(cmd))
            if cmd[:2] == ["timedatectl", "show"]:
                return _completed(cmd, returncode=0, stdout="yes\n")
            if cmd[:3] == ["date", "-u", "-s"]:
                return _completed(cmd, returncode=0, stdout="")
            raise AssertionError(f"Unexpected command: {cmd}")

        result = timesync_guard.evaluate_and_sync(
            "https://lite-actions.ooguy.com/api/v1/public/overview",
            threshold=2.0,
            apply=True,
            force_step=True,
            opener=lambda req, timeout=10.0: _FakeResponse({"Date": date_hdr}),
            runner=_runner,
            time_fn=lambda: 1795579700.0,
        )
        self.assertTrue(result.ntp_synchronized)
        self.assertEqual(result.action, "adjusted")
        self.assertEqual(result.applied_epoch, 1795579800)
        self.assertEqual(commands[-1], ["date", "-u", "-s", "@1795579800"])

    def test_skips_when_ntp_status_unknown_unless_forced(self):
        remote_epoch = 1795579800.0
        date_hdr = email.utils.formatdate(remote_epoch, usegmt=True)
        commands: list[list[str]] = []

        def _runner(cmd, **kwargs):
            commands.append(list(cmd))
            raise FileNotFoundError("timedatectl not installed")

        result = timesync_guard.evaluate_and_sync(
            "https://lite-actions.ooguy.com/api/v1/public/overview",
            threshold=2.0,
            apply=True,
            opener=lambda req, timeout=10.0: _FakeResponse({"Date": date_hdr}),
            runner=_runner,
            time_fn=lambda: 1795579700.0,
        )
        self.assertIsNone(result.ntp_synchronized)
        self.assertEqual(result.action, "skipped_ntp_unknown")
        self.assertIsNone(result.applied_epoch)
        self.assertEqual(
            commands,
            [["timedatectl", "show", "-p", "NTPSynchronized", "--value"]],
        )

    def test_url_validation_rejects_embedded_credentials(self):
        with self.assertRaises(ValueError):
            timesync_guard.validate_url(
                "https://user:secret@lite-actions.ooguy.com/api/v1/public/overview"
            )
        self.assertEqual(
            timesync_guard.sanitize_url_for_log(
                "https://user:secret@lite-actions.ooguy.com/api/v1/public/overview"
            ),
            "https://lite-actions.ooguy.com/api/v1/public/overview",
        )


if __name__ == "__main__":
    unittest.main()
