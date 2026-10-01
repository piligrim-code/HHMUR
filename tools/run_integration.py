"""Own a disposable loopback-only PostgreSQL instance for synthetic adapter tests."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
OWNER_LABEL = "hhmur.probe.owner"


def run():
    suffix = uuid.uuid4().hex[:12]
    name = "hhmur-probe-" + suffix
    password = secrets.token_urlsafe(32)
    # Never reuse the application's configured DSN or libpq service settings.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG") and k != "HHMUR_DATABASE_URL"}
    env.update(POSTGRES_USER="hhmur_probe", POSTGRES_PASSWORD=password,
               POSTGRES_DB="hhmur_probe_" + suffix, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               PYTHONIOENCODING="utf-8")
    created = False
    stage = "local Docker preflight"

    def docker(*args, check=True, timeout=60):
        result = subprocess.run(["docker", *args], env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("Docker operation failed: " + args[0])
        return result

    try:
        context = json.loads(docker("context", "inspect").stdout)[0]
        endpoint = context["Endpoints"]["docker"]["Host"]
        if env.get("DOCKER_HOST") or not endpoint.startswith(("npipe:////./pipe/", "unix://")):
            raise RuntimeError("A local Docker context without DOCKER_HOST is required")
        stage = "prepare PostgreSQL"
        if docker("image", "inspect", IMAGE, check=False).returncode:
            print("Pulling pinned PostgreSQL test image", flush=True)
            docker("pull", IMAGE, timeout=300)
        # Register the name before creation to cover timeout-after-create too.
        created = True
        docker("create", "--name", name, "--label", OWNER_LABEL + "=" + name,
               "--publish", "127.0.0.1::5432", "--memory", "512m", "--cpus", "1",
               "--env", "POSTGRES_USER", "--env", "POSTGRES_PASSWORD", "--env", "POSTGRES_DB", IMAGE)
        docker("start", name)
        stage = "PostgreSQL readiness"
        deadline = time.monotonic() + 90
        while True:
            try:
                result = docker("exec", name, "pg_isready", "-U", "hhmur_probe", "-d", env["POSTGRES_DB"],
                                check=False, timeout=10)
                if result.returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                pass
            obj = json.loads(docker("inspect", name).stdout)[0]
            if not obj["State"]["Running"]:
                print(json.dumps({"exit_code": obj["State"].get("ExitCode"), "oom_killed": obj["State"].get("OOMKilled")}), flush=True)
                raise RuntimeError("PostgreSQL exited before readiness")
            if time.monotonic() >= deadline:
                raise TimeoutError("PostgreSQL readiness deadline")
            time.sleep(1)
        obj = json.loads(docker("inspect", name).stdout)[0]
        if (obj["Config"]["Labels"] or {}).get(OWNER_LABEL) != name:
            raise RuntimeError("Test container ownership changed")
        bindings = obj["NetworkSettings"]["Ports"]["5432/tcp"]
        if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
            raise RuntimeError("Refusing non-loopback test service")
        env.update(HHMUR_INTEGRATION="1", HHMUR_TEST_PASSWORD=password,
                   HHMUR_TEST_DB=env["POSTGRES_DB"], HHMUR_TEST_PORT=bindings[0]["HostPort"])
        print(json.dumps({"image": obj["Image"], "scope": "synthetic PostgreSQL adapter tests; no external services"}), flush=True)
        stage = "integration tests"
        result = subprocess.run([sys.executable, "-m", "pytest", "tests/integration", "-q", "--tb=short"],
                                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        print((result.stdout + result.stderr).replace(password, "<redacted>"))
        return result.returncode
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, RuntimeError) as error:
        print("Integration failed at " + stage + " (" + type(error).__name__ + "); raw diagnostics omitted.", file=sys.stderr)
        return 1
    finally:
        if created:
            cleanup_ok = False
            try:
                result = docker("inspect", name, check=False, timeout=30)
                if result.returncode:
                    existing = docker("ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}", timeout=30)
                    cleanup_ok = not existing.stdout.strip()
                else:
                    labels = json.loads(result.stdout)[0]["Config"]["Labels"] or {}
                    if labels.get(OWNER_LABEL) == name:
                        docker("rm", "--force", "--volumes", name, timeout=30)
                        cleanup_ok = True
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, RuntimeError):
                pass
            if not cleanup_ok:
                print("Owned test-container cleanup incomplete: " + name, file=sys.stderr)
                raise RuntimeError("Test resource cleanup incomplete")
            print("Owned PostgreSQL container and anonymous volumes removed.", flush=True)


if __name__ == "__main__":
    raise SystemExit(run())
