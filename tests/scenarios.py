"""External GitHub/model fixtures for workflow lifecycle tests; no NPU simulation claims."""

import io
import json
import tarfile
import zipfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from ci_core.contract import create_plan, normalize
from ci_core.executor import Execution
from ci_core.github import GitHubClient

DEFAULTS = dict(worker="blue", target_pool="pool_blue", docker="training")


def definition(name, *, kind="train", worker="blue", nodes=1):
    return SimpleNamespace(
        test_name=name,
        test_descr=name,
        nnodes=nodes,
        ngpu=2,
        timeout=90,
        ci_kind=kind,
        ci_overrides={"worker": worker},
    )


def write_workspace(root):
    config = root / "config"
    config.mkdir(parents=True)
    nodes = [
        dict(
            host_id=f"192.0.2.{i}:22",
            ssh=f"192.0.2.{i}:22",
            node_ip=f"192.0.2.{i}",
            device_ids=[0, 1, 2, 3],
            allocation_groups=[
                dict(devices=[0, 1], verified=True),
                dict(devices=[2, 3], verified=True),
            ],
            environments={
                "training": dict(
                    container_id="a" * 64,
                    image_id="sha256:" + "b" * 64,
                    image_ref="fixture:tag",
                )
            },
        )
        for i in [10, 11]
    ]
    pool = dict(
        owner="blue",
        enabled=True,
        nodes=nodes,
        process_indexing="card-die",
        max_jobs_per_host=2,
        verified_multinode_allocations=[
            [dict(host_id=n["host_id"], devices=[0, 1]) for n in nodes]
        ],
    )
    (config / "pools.json").write_text(json.dumps({"pool_blue": pool}))
    (config / "workers.json").write_text(
        json.dumps({w: dict(enabled=True) for w in ["blue", "yellow"]})
    )


class Service(GitHubClient):
    """In-memory REST boundary; artifact decoding and result posting use production code."""

    def __init__(self, definitions, root):
        super().__init__("disposable-fixture-token")
        self.definitions = definitions
        self.plans, self.runs, self.comments = {}, {}, []
        self.offline = False
        self.archive = root / "model.tar.gz"
        rows = [
            dict(
                test_id=d.test_name,
                label=d.test_descr,
                nnodes=d.nnodes,
                ngpu=d.ngpu,
                timeout=d.timeout,
                kind=d.ci_kind,
                ci_overrides=d.ci_overrides,
                env_vars=dict(
                    ASCEND_SET_ENV_PATH="/fixture/cann",
                    HF_ASSETS_PATH="/fixture/model",
                    CKPT_INIT_LOAD_PATH="/fixture/checkpoint",
                ),
            )
            for d in definitions
        ]
        inspector = (
            "import json,sys\nrows=" + repr(rows) + "\n"
            "name=sys.argv[sys.argv.index('--test-id')+1]\n"
            "print(json.dumps([r for r in rows if r['test_id']==name]))\n"
        )
        with tarfile.open(self.archive, "w:gz") as archive:
            for name in [
                "tests/__init__.py",
                "tests/integration_tests/__init__.py",
                "tests/integration_tests/tools/__init__.py",
                "tests/integration_tests/tools/lite_actions/__init__.py",
                "tests/integration_tests/tools/lite_actions/entrypoint.py",
            ]:
                data = inspector.encode() if name.endswith("entrypoint.py") else b""
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        self.add_run(20)

    def add_run(self, ident, *, attempt=1):
        run = dict(
            id=ident,
            run_attempt=attempt,
            head_sha="a" * 40,
            path=".github/workflows/nightly_fulltests.yml",
            created_at="2026-01-01T00:00:00Z",
            status="in_progress",
            event="workflow_dispatch",
            head_branch="master",
            actor=dict(login="maintainer"),
            name="Nightly",
            run_number=ident,
        )
        plan = create_plan(
            [normalize(d, DEFAULTS, i) for i, d in enumerate(self.definitions)],
            run_id=ident,
            attempt=attempt,
            sha=run["head_sha"],
            workflow="nightly_fulltests.yml",
            defaults=DEFAULTS,
        )
        self.runs[ident], self.plans[ident] = run, plan
        return run, plan

    def request(self, method, path, payload=None):
        if method == "GET" and path.startswith("/actions/runs?"):
            event = (
                "pull_request" if "event=pull_request" in path else "workflow_dispatch"
            )
            return dict(
                workflow_runs=[
                    r
                    for r in self.runs.values()
                    if r["status"]
                    == ("queued" if "status=queued" in path else "in_progress")
                    and r["event"] == event
                ]
            )
        if path.startswith("/actions/runs/"):
            ident = int(path.split("/")[3])
            run = self.runs[ident]
            if "/artifacts?" in path:
                return dict(
                    artifacts=[
                        dict(
                            name=f"lite-ci-request-{run['run_attempt']}",
                            id=ident,
                            expired=False,
                            size_in_bytes=1000,
                            workflow_run=dict(id=ident),
                        )
                    ]
                )
            return dict(run)
        if method == "GET" and path.startswith("/commits/"):
            return self.comments
        if method == "POST":
            if self.offline:
                raise OSError("fixture network outage")
            item = dict(
                id=len(self.comments) + 1,
                body=payload["body"],
                user=dict(login="reporter"),
            )
            self.comments.append(item)
            return item
        if method == "PATCH":
            item = self.comments[int(path.rsplit("/", 1)[1]) - 1]
            item.update(payload)
            return item
        raise AssertionError((method, path))

    def open(self, request, **kwargs):
        ident = request.full_url.split("/")[-2]
        raise HTTPError(
            request.full_url,
            302,
            "redirect",
            {"Location": f"https://fixture.invalid/{ident}"},
            None,
        )

    def download(self, request, **kwargs):
        if "Authorization" in request.headers:
            raise AssertionError("GitHub token forwarded to signed URL")
        ident = int(request.full_url.rsplit("/", 1)[1])
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("ci-request.json", json.dumps(self.plans[ident]))
        out.seek(0)
        return out

    def connected(self):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch("urllib.request.build_opener", return_value=self))
        stack.enter_context(patch("urllib.request.urlopen", side_effect=self.download))
        stack.enter_context(
            patch("ci_core.router.model_archive", return_value=self.archive)
        )
        stack.enter_context(
            patch(
                "ci_core.router.probe_worker",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="WORKER_HOSTNAME=fixture\nWORKER_ARCH=fixture\n",
                    stderr="",
                ),
            )
        )
        return stack

    def results(self):
        return [
            json.loads(c["body"].split("\n", 1)[1])
            for c in self.comments
            if c["body"].startswith("Lite-CI-V3 ")
        ]


def completed_execution(root, case, run, *, lines=None, rc=0, clean=True):
    log = root / (str(run["id"]) + "-" + case["name"] + ".log")
    log.write_text(
        f"CASE_RESULT {run['_ci_test_id']} {'PASS' if rc == 0 else 'FAIL'} {rc}\n"
    )
    return Execution(rc, lines or ["training completed"], log, clean)
