import json
import subprocess
from types import SimpleNamespace

import pytest

from tools import run_integration


class DockerProbe:
    def __init__(self, endpoint="unix:///var/run/docker.sock", owner=True, create_failure=False,
                 test_timeout=False, loopback=True, cleanup_failure=False):
        self.endpoint = endpoint
        self.owner = owner
        self.create_failure = create_failure
        self.test_timeout = test_timeout
        self.loopback = loopback
        self.cleanup_failure = cleanup_failure
        self.containers = {}
        self.removed = []
        self.test_called = False
        self.password = None

    def __call__(self, args, **kwargs):
        def result(stdout="", code=0):
            return SimpleNamespace(stdout=stdout, stderr="", returncode=code)
        env = kwargs["env"]
        assert "HHMUR_DATABASE_URL" not in env
        assert not any(key.upper().startswith("PG") for key in env)
        if args[0] != "docker":
            self.test_called = True
            self.password = env["HHMUR_TEST_PASSWORD"]
            if self.test_timeout:
                raise subprocess.TimeoutExpired(args, timeout=120)
            return result("synthetic test failure: " + self.password, code=1)
        if args[1:3] == ["context", "inspect"]:
            return result(json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}]))
        if args[1] == "create":
            if self.create_failure:
                return result(code=1)
            name = args[args.index("--name") + 1]
            label = args[args.index("--label") + 1].split("=", 1)[1]
            assert args[args.index("--publish") + 1] == "127.0.0.1::5432"
            assert env["POSTGRES_PASSWORD"] not in args
            self.containers[name] = label
        if args[1] == "inspect":
            name = args[2]
            if name not in self.containers:
                return result(code=1)
            return result(json.dumps([{
                "Config": {"Labels": {run_integration.OWNER_LABEL: self.containers[name] if self.owner else "other-owner"}},
                "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1" if self.loopback else "0.0.0.0", "HostPort": "10000"}]}},
                "Image": "synthetic-image-id",
            }]))
        if args[1] == "rm":
            if self.cleanup_failure:
                return result(code=1)
            assert args[-1] in self.containers
            self.removed.append(args[-1])
        return result()


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("HHMUR_DATABASE_URL", "must-not-be-used")
    monkeypatch.setenv("PGSERVICE", "must-not-be-used")
    monkeypatch.setenv("PGOPTIONS", "must-not-be-used")
    def install(**kwargs):
        probe = DockerProbe(**kwargs)
        monkeypatch.setattr(run_integration.subprocess, "run", probe)
        return probe
    return install


@pytest.mark.parametrize("endpoint", ["ssh://remote-test-host", "npipe:////remote-test-host/pipe/docker_engine"])
def test_rejects_remote_context_without_creating_anything(setup, endpoint):
    probe = setup(endpoint=endpoint)
    assert run_integration.run() == 1
    assert not probe.containers and not probe.test_called


def test_rejects_docker_host_override(setup, monkeypatch):
    probe = setup()
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote-test-host")
    assert run_integration.run() == 1
    assert not probe.containers


def test_failure_is_propagated_redacted_and_cleaned(setup, capsys):
    probe = setup()
    assert run_integration.run() == 1
    assert probe.test_called and len(probe.removed) == 1
    output = capsys.readouterr()
    assert probe.password not in output.out + output.err
    assert "<redacted>" in output.out


def test_wrong_owner_is_never_removed(setup):
    probe = setup(owner=False)
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        run_integration.run()
    assert not probe.removed and not probe.test_called


def test_creation_failure_does_not_remove_absent_resources(setup):
    probe = setup(create_failure=True)
    assert run_integration.run() == 1
    assert not probe.removed and not probe.test_called


def test_test_timeout_still_cleans_owned_resources(setup):
    probe = setup(test_timeout=True)
    assert run_integration.run() == 1
    assert probe.test_called and len(probe.removed) == 1


def test_non_loopback_binding_rejected_before_tests(setup):
    probe = setup(loopback=False)
    assert run_integration.run() == 1
    assert not probe.test_called and len(probe.removed) == 1


def test_cleanup_failure_is_not_reported_as_success(setup, capsys):
    probe = setup(cleanup_failure=True)
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        run_integration.run()
    assert not probe.removed
    assert "anonymous volumes removed" not in capsys.readouterr().out
