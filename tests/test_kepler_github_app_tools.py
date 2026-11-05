"""Offline tests for the Kepler GitHub App tooling (verify-app.py and store-app-secret.sh).

Throwaway RSA keys, a fake `aws` on PATH, no network, no GitHub, no AWS, no model calls.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "deploy" / "agentcore" / "kepler" / "github-app"
pytestmark = pytest.mark.skipif(
    not (TOOLS / "verify-app.py").exists() or shutil.which("bash") is None,
    reason="deploy/ or bash is not available",
)


def _load_verify():
    spec = importlib.util.spec_from_file_location("verify_app", TOOLS / "verify-app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


va = _load_verify()
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()


def _pad(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_jwt_is_rs256_short_lived_and_signed_by_the_key():
    now = 1_800_000_000
    header, claims, signature = va.make_jwt("12345", PEM, now=now).split(".")
    assert json.loads(_pad(header)) == {"alg": "RS256", "typ": "JWT"}
    decoded = json.loads(_pad(claims))
    assert decoded["iss"] == "12345"
    assert decoded["iat"] == now - 60 and decoded["exp"] == now + 540
    assert decoded["exp"] - decoded["iat"] <= 600  # GitHub's ten-minute ceiling
    KEY.public_key().verify(_pad(signature), f"{header}.{claims}".encode(),
                            padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(InvalidSignature):
        KEY.public_key().verify(_pad(signature), f"{header}.{claims}x".encode(),
                                padding.PKCS1v15(), hashes.SHA256())


def test_a_malformed_key_is_rejected():
    with pytest.raises(ValueError):
        va.make_jwt("1", "not a pem")


def test_expected_permissions_keep_the_reader_read_only_and_the_writer_narrow():
    assert all(level == "read" for level in va.EXPECTED["reader"].values())
    assert {k for k, v in va.EXPECTED["writer"].items() if v == "write"} == {"issues"}
    assert set(va.EXPECTED["writer"]) == {"issues", "metadata"}
    assert "pull_requests" not in va.EXPECTED["writer"] and "contents" not in va.EXPECTED["writer"]
    assert va.SECRET_NAME["reader"] != va.SECRET_NAME["writer"]


@pytest.fixture
def store(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured, log = tmp_path / "captured.json", tmp_path / "aws.log"
    fake = bindir / "aws"
    fake.write_text(
        f'#!/bin/bash\necho "$@" >> {log}\ncase "$*" in\n'
        f'  *describe-secret*) [ -f {tmp_path}/missing ] && exit 255; exit 0 ;;\n'
        f'  *put-secret-value*) for a in "$@"; do case "$a" in file://*) cp "${{a#file://}}" '
        f'{captured};; esac; done; echo VERSION; exit 0 ;;\nesac\nexit 1\n'
    )
    fake.chmod(0o755)
    keyfile = tmp_path / "app.pem"
    keyfile.write_text(PEM)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}

    def run(*args):
        return subprocess.run(["bash", str(TOOLS / "store-app-secret.sh"), *args],
                              capture_output=True, text=True, env=env, cwd=ROOT)

    return type("S", (), {"run": staticmethod(run), "key": keyfile, "captured": captured,
                          "log": log, "dir": tmp_path, "bin": bindir})


@pytest.mark.parametrize("args", [
    ["bogus", "1", "2", "KEY"], ["reader", "x", "2", "KEY"], ["reader", "1", "y", "KEY"],
    ["reader", "1", "2", "/nonexistent.pem"], ["reader", "1", "2", "KEY", "--bogus"],
])
def test_bad_arguments_are_rejected_before_any_aws_call(store, args):
    args = [str(store.key) if a == "KEY" else a for a in args]
    result = store.run(*args)
    assert result.returncode == 2 and not store.log.exists()


def test_a_file_that_is_not_a_private_key_is_rejected(store):
    notkey = store.dir / "notkey.txt"
    notkey.write_text("hello\n")
    assert store.run("reader", "1", "2", str(notkey)).returncode == 2 and not store.log.exists()


def test_it_refuses_when_the_secret_does_not_exist(store):
    (store.dir / "missing").write_text("")
    result = store.run("reader", "1", "2", str(store.key))
    assert result.returncode == 1 and "does not exist" in result.stdout
    assert not store.captured.exists()


def test_it_stores_the_exact_key_without_printing_or_passing_it_on_a_command_line(store):
    result = store.run("writer", "999", "888", str(store.key))
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert json.loads(store.captured.read_text()) == {
        "app_id": 999, "installation_id": 888, "private_key_pem": PEM}
    log = store.log.read_text()
    assert "viharbot/kepler/github-app-alert-writer" in log
    secret_line = PEM.strip().splitlines()[1]
    assert "BEGIN" not in out and secret_line not in out
    assert "BEGIN" not in log and secret_line not in log  # never in argv
    assert store.key.exists()  # kept unless --delete-pem


def test_delete_pem_removes_the_key_only_after_a_successful_store(store):
    assert store.run("reader", "1", "2", str(store.key), "--delete-pem").returncode == 0
    assert not store.key.exists()

    store.key.write_text(PEM)
    (store.bin / "aws").write_text(
        '#!/bin/bash\ncase "$*" in *describe-secret*) exit 0;; esac\nexit 1\n')
    result = store.run("reader", "1", "2", str(store.key), "--delete-pem")
    assert result.returncode == 1 and store.key.exists() and "NOT deleted" in result.stdout
