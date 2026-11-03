"""Service authentication (ADR-006 A2). Offline and deterministic: throwaway keys, injected clock.

These tests prove authentication properties only. They do not imply delegation-grant authorization,
which is A3 (issue #95).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from viharbot import service_auth as sa

AUD = "fact-check"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def raw_public(key: Ed25519PrivateKey) -> str:
    return sa.b64url_encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


def raw_seed(key: Ed25519PrivateKey) -> str:
    return sa.b64url_encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )


def registry_text(*entries: tuple[str, str, Ed25519PrivateKey, list[str]]) -> str:
    blocks = []
    for kid, sub, key, caps in entries:
        capabilities = ", ".join(f'"{c}"' for c in caps)
        blocks.append(
            f'[[keys]]\nkid = "{kid}"\nsub = "{sub}"\npublic_key = "{raw_public(key)}"\n'
            f"capabilities = [{capabilities}]\n"
        )
    return "\n".join(blocks)


@pytest.fixture
def world(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "registry.toml"
    path.write_text(registry_text(("web-1", "web", key, ["specialist.process"])))
    clock = Clock()
    registry = sa.KeyRegistry(path, clock)
    registry.load_initial()
    auth = sa.ServiceAuthenticator(audience=AUD, registry=registry, clock=clock)
    key_file = tmp_path / "signing.key"
    key_file.write_text(raw_seed(key))
    return SimpleNamespace(
        key=key, path=path, clock=clock, registry=registry, auth=auth, key_file=str(key_file)
    )


def mint(world, **overrides):
    args = dict(
        kid="web-1", sub="web", audience=AUD, capability="specialist.process",
        engagement="eng-1", classification="internal", method="POST", target="/process",
        body=b'{"question":"q"}', key_path=world.key_file, now=world.clock.now,
    )
    args.update(overrides)
    return sa.mint_assertion(**args)


def verify(world, token, *, method="POST", target="/process", body=b'{"question":"q"}'):
    path, query = sa.split_target(target)
    return world.auth.verify(token, method=method, raw_path=path, query=query, body=body)


def denied(world, token, status, reason, **kwargs):
    with pytest.raises(sa.AuthError) as info:
        verify(world, token, **kwargs)
    assert (info.value.status, info.value.reason) == (status, reason)


def forge(key, header, claims):
    """Build a token with arbitrary header and claims, signed with ``key`` (EdDSA signature)."""
    def seg(value):
        return sa.b64url_encode(json.dumps(value).encode())

    signing_input = f"{seg(header)}.{seg(claims)}"
    return f"{signing_input}.{sa.b64url_encode(key.sign(signing_input.encode()))}"


def good_header(**extra):
    return {"alg": "EdDSA", "typ": sa.ASSERTION_TYP, "kid": "web-1", **extra}


def good_claims(world, **overrides):
    now = int(world.clock.now)
    claims = {
        "iss": "web", "sub": "web", "aud": AUD, "exp": now + 30, "iat": now, "jti": "j-1",
        "cap": "specialist.process", "eng": "e", "cls": "c", "mth": "POST", "tgt": "/process",
        "req_sha256": hashlib.sha256(b'{"question":"q"}').hexdigest(),
    }
    claims.update(overrides)
    return claims


# --- happy path and token shape ---------------------------------------------------------------


def test_valid_assertion_is_accepted_and_identity_comes_from_the_registry(world):
    caller = verify(world, mint(world))
    assert (caller.sub, caller.kid, caller.capability) == ("web", "web-1", "specialist.process")


def test_no_token_and_malformed_tokens_are_refused(world):
    denied(world, None, 401, "missing_token")
    denied(world, "", 401, "missing_token")
    for bad in ("abc", "a.b", "a.b.c.d", "!!!.###.$$$", "x" * (sa.MAX_TOKEN_BYTES + 1)):
        denied(world, bad, 401, "malformed_token")
    not_object = sa.b64url_encode(b"[1]")
    denied(world, f"{not_object}.{not_object}.AAAA", 401, "malformed_token")


def test_unknown_kid_and_missing_kid(world):
    other = Ed25519PrivateKey.generate()
    denied(world, forge(other, good_header(kid="nope"), good_claims(world)), 401, "unknown_kid")
    header = good_header()
    del header["kid"]
    denied(world, forge(world.key, header, good_claims(world)), 401, "missing_kid")


def test_wrong_signature_key_is_refused(world):
    token = forge(Ed25519PrivateKey.generate(), good_header(), good_claims(world))
    denied(world, token, 401, "bad_signature")


def test_wrong_typ_is_refused(world):
    for typ in ("viharbot-grant+jwt", "JWT", None):
        header = good_header(typ=typ)
        denied(world, forge(world.key, header, good_claims(world)), 401, "wrong_token_type")


def test_alg_none_and_hs256_are_refused(world):
    claims = good_claims(world)

    def seg(value):
        return sa.b64url_encode(json.dumps(value).encode())

    unsigned = f"{seg(good_header(alg='none'))}.{seg(claims)}."
    denied(world, unsigned, 401, "algorithm_not_allowed")
    public = world.key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    signing_input = f"{seg(good_header(alg='HS256'))}.{seg(claims)}"
    mac = hmac.new(public, signing_input.encode(), hashlib.sha256).digest()
    denied(world, f"{signing_input}.{sa.b64url_encode(mac)}", 401, "algorithm_not_allowed")
    for alg in ("RS256", "ES256", "eddsa", ""):
        token = forge(world.key, good_header(alg=alg), claims)
        denied(world, token, 401, "algorithm_not_allowed")


def test_key_bearing_headers_are_ignored(world):
    attacker = Ed25519PrivateKey.generate()
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": raw_public(attacker)}
    header = good_header(jku="https://evil.test/k", x5u="https://evil.test/c", x5c=["AAAA"], jwk=jwk)
    # Signed by the attacker: refused, because only the registry key for the kid is used.
    denied(world, forge(attacker, header, good_claims(world)), 401, "bad_signature")
    # Signed by the registered key: accepted, the extra headers change nothing.
    assert verify(world, forge(world.key, header, good_claims(world))).sub == "web"


def test_sub_and_iss_must_match_the_registry_binding(world):
    denied(world, forge(world.key, good_header(), good_claims(world, iss="cipher")), 401,
           "subject_mismatch")
    denied(world, forge(world.key, good_header(), good_claims(world, sub="cipher")), 401,
           "subject_mismatch")


def test_capability_must_be_in_the_registry_entry(world):
    token = mint(world, capability="other.capability")
    denied(world, token, 403, "capability_not_permitted")


def test_required_claims_and_types(world):
    for name in sa.REQUIRED_CLAIMS:
        claims = good_claims(world)
        del claims[name]
        denied(world, forge(world.key, good_header(), claims), 401, "missing_claim")
    for name, value in (("exp", "soon"), ("iat", 1.5), ("exp", True), ("aud", ["fact-check"]),
                        ("jti", ""), ("jti", "x" * 200), ("cap", 3)):
        token = forge(world.key, good_header(), good_claims(world, **{name: value}))
        denied(world, token, 401, "malformed_claim")


def test_dlg_is_optional_and_ignored_in_a2(world):
    for dlg in (None, "garbage", {"gid": "x"}):
        claims = good_claims(world, jti=f"j-{dlg!r}")
        if dlg is not None:
            claims["dlg"] = dlg
        assert verify(world, forge(world.key, good_header(), claims)).sub == "web"


# --- time -------------------------------------------------------------------------------------


def test_expired_and_not_yet_valid_with_injected_clock(world):
    token = mint(world)
    world.clock.now = T0 + 60 + sa.CLOCK_SKEW_SECONDS  # exactly at exp + skew: still valid
    assert verify(world, token).sub == "web"
    world.clock.now += 1
    denied(world, mint(world, now=T0), 401, "expired")
    future = mint(world, now=T0 + 1000)
    denied(world, future, 401, "not_yet_valid")
    world.clock.now = T0 + 1000 - sa.CLOCK_SKEW_SECONDS  # within skew of iat
    assert verify(world, future).sub == "web"


def test_lifetime_over_60_seconds_or_not_positive_is_refused(world):
    now = int(T0)
    denied(world, forge(world.key, good_header(), good_claims(world, exp=now + 61)), 401,
           "invalid_lifetime")
    denied(world, forge(world.key, good_header(), good_claims(world, exp=now)), 401,
           "invalid_lifetime")
    with pytest.raises(ValueError):
        mint(world, lifetime=61)
    with pytest.raises(ValueError):
        mint(world, lifetime=0)


# --- audience and binding ---------------------------------------------------------------------


def test_wrong_audience(world):
    denied(world, mint(world, audience="synthesis"), 401, "wrong_audience")


def test_tampered_body_method_and_target(world):
    token = mint(world)
    denied(world, token, 401, "binding_mismatch", body=b'{"question":"other"}')
    denied(world, mint(world, jti="a"), 401, "binding_mismatch", method="GET")
    denied(world, mint(world, jti="b"), 401, "binding_mismatch", target="/other")
    denied(world, mint(world, target="/process?x=1", jti="c"), 401, "binding_mismatch")


def test_empty_body_and_query_are_bound(world):
    token = mint(world, method="GET", target="/artifacts/a?v=1", body=b"")
    assert verify(world, token, method="GET", target="/artifacts/a?v=1", body=b"").sub == "web"
    other = mint(world, method="GET", target="/artifacts/a?v=1", body=b"", jti="2")
    denied(world, other, 401, "binding_mismatch", method="GET", target="/artifacts/b?v=1", body=b"")
    third = mint(world, method="GET", target="/artifacts/a?v=1", body=b"", jti="3")
    denied(world, third, 401, "binding_mismatch", method="GET", target="/artifacts/a?v=2", body=b"")


@pytest.mark.parametrize("target,expected", [
    ("/files/report.bin", "/files/report.bin"),
    ("/x?b=2&a=1&a=0", "/x?b=2&a=1&a=0"),
    ("/x?a=0&a=1", "/x?a=0&a=1"),
    ("/x?q=a%20b", "/x?q=a%20b"),
    ("/x?q=a+b", "/x?q=a+b"),
    ("/x?a", "/x?a"),
    ("/x?a=", "/x?a="),
    ("/x?q=%2f", "/x?q=%2F"),
    ("/", "/"),
    ("/a/b/", "/a/b/"),
])
def test_canonical_target_vectors(target, expected):
    path, query = sa.split_target(target)
    assert sa.canonical_target(path, query) == expected


def test_adr_vectors_that_must_differ_do_differ():
    def tgt(value):
        return sa.canonical_target(*sa.split_target(value))

    assert tgt("/x?a=0&a=1") != tgt("/x?a=1&a=0")
    assert tgt("/x?q=a%20b") != tgt("/x?q=a+b")
    assert tgt("/x?a") != tgt("/x?a=")
    assert tgt("/x?q=a+b") != tgt("/x?q=a%2Bb")


@pytest.mark.parametrize("target", [
    "/a//b", "/a/../b", "/a/./b", "/a/%2e%2e/b", "/a/%2E%2E/b", "/a/b%2Fc", "/a/b%2fc",
    "/a/b%5Cc", "/a/b%00c", "/x?q=%7e", "/a/%7e", "/a/%41", "/a%zz", "/a%2", "/x?q=%zz",
    "/x?", "/x?a=1&&b=2", "/x?a=1&", "/x?a=1;b=2", "/x?q=café", "/a b", "/a\x7fb",
    "relative", "/a#frag", "/x?q=%41",
])
def test_rejected_targets(target):
    path, query = sa.split_target(target)
    with pytest.raises(sa.AuthError) as info:
        sa.canonical_target(path, query)
    assert info.value.status == 400


def test_issuer_never_produces_a_rejected_target(world):
    with pytest.raises(sa.AuthError):
        mint(world, target="/a/../b")


def test_rejected_target_is_400_after_authentication(world):
    token = mint_forged_target(world, "/a//b")
    denied(world, token, 400, "target_empty_segment", target="/a//b")


def mint_forged_target(world, target):
    return forge(world.key, good_header(), good_claims(
        world, tgt=target, req_sha256=hashlib.sha256(b'{"question":"q"}').hexdigest()))


# --- replay -----------------------------------------------------------------------------------


def test_replayed_jti_refused_and_evicted_after_ttl(world):
    token = mint(world, jti="once")
    assert verify(world, token).sub == "web"
    denied(world, token, 401, "replayed_token")
    # Past exp + skew the token is rejected as expired and its cache entry no longer blocks the jti.
    world.clock.now = T0 + 60 + sa.CLOCK_SKEW_SECONDS + 1
    fresh = mint(world, jti="once", now=world.clock.now)
    assert verify(world, fresh).sub == "web"


def test_replay_cache_is_bounded_and_fails_closed():
    cache = sa.ReplayCache(max_entries=3)
    for i in range(3):
        cache.check_and_add("web", f"j{i}", T0 + 30, T0)
    with pytest.raises(sa.AuthError) as info:
        cache.check_and_add("web", "j3", T0 + 30, T0)
    assert info.value.reason == "replay_cache_full"
    cache.check_and_add("web", "j3", T0 + 130, T0 + 100)  # expired entries were purged


def test_failed_verification_does_not_consume_a_jti(world):
    bad = forge(Ed25519PrivateKey.generate(), good_header(), good_claims(world, jti="shared"))
    denied(world, bad, 401, "bad_signature")
    good = forge(world.key, good_header(), good_claims(world, jti="shared"))
    assert verify(world, good).sub == "web"


# --- key registry -----------------------------------------------------------------------------


def test_key_rotation_overlap_and_retirement(world):
    new_key = Ed25519PrivateKey.generate()
    world.path.write_text(registry_text(
        ("web-1", "web", world.key, ["specialist.process"]),
        ("web-2", "web", new_key, ["specialist.process"]),
    ))
    world.clock.now += sa.REGISTRY_REFRESH_SECONDS
    new_token = forge(new_key, good_header(kid="web-2"), good_claims(world, jti="n"))
    old_token = forge(world.key, good_header(), good_claims(world, jti="o"))
    assert verify(world, new_token).sub == "web"
    assert verify(world, old_token).sub == "web"  # both valid during overlap
    world.path.write_text(registry_text(("web-2", "web", new_key, ["specialist.process"])))
    world.clock.now += sa.REGISTRY_REFRESH_SECONDS
    retired = forge(world.key, good_header(), good_claims(world, jti="o2"))
    denied(world, retired, 401, "unknown_kid")
    assert verify(world, forge(new_key, good_header(kid="web-2"),
                               good_claims(world, jti="n2"))).sub == "web"


def test_refresh_failure_keeps_last_known_keys_for_15_minutes_then_fails_closed(world):
    world.path.write_text("not = [valid")
    world.clock.now += sa.REGISTRY_REFRESH_SECONDS
    assert verify(world, forge(world.key, good_header(), good_claims(world, jti="a"))).sub == "web"
    world.clock.now = T0 + sa.REGISTRY_GRACE_SECONDS  # exactly at the limit: still allowed
    assert verify(world, forge(world.key, good_header(), good_claims(world, jti="b"))).sub == "web"
    world.clock.now = T0 + sa.REGISTRY_GRACE_SECONDS + 1
    denied(world, forge(world.key, good_header(), good_claims(world, jti="c")), 503,
           "key_registry_unavailable")
    # Recovery: a good file restores service.
    world.path.write_text(registry_text(("web-1", "web", world.key, ["specialist.process"])))
    world.clock.now += sa.REGISTRY_REFRESH_SECONDS
    assert verify(world, forge(world.key, good_header(), good_claims(world, jti="d"))).sub == "web"


def test_missing_registry_file_counts_as_failed_refresh(world):
    world.path.unlink()
    world.clock.now = T0 + sa.REGISTRY_GRACE_SECONDS + sa.REGISTRY_REFRESH_SECONDS
    denied(world, mint(world), 503, "key_registry_unavailable")


def test_unknown_kid_reload_is_rate_limited(world, monkeypatch):
    loads = []
    original = world.registry._load

    def counting_load():
        loads.append(world.clock.now)
        return original()

    monkeypatch.setattr(world.registry, "_load", counting_load)
    stranger = forge(Ed25519PrivateKey.generate(), good_header(kid="ghost"), good_claims(world))
    world.clock.now = T0 + 11
    denied(world, stranger, 401, "unknown_kid")
    denied(world, stranger, 401, "unknown_kid")
    world.clock.now += 5
    denied(world, stranger, 401, "unknown_kid")
    assert len(loads) == 1
    world.clock.now += 6  # now 11 s after the last reload
    denied(world, stranger, 401, "unknown_kid")
    assert len(loads) == 2


def test_unknown_kid_reload_picks_up_a_newly_added_key(world):
    new_key = Ed25519PrivateKey.generate()
    world.path.write_text(registry_text(
        ("web-1", "web", world.key, ["specialist.process"]),
        ("web-3", "web", new_key, ["specialist.process"]),
    ))
    world.clock.now = T0 + 11
    token = forge(new_key, good_header(kid="web-3"),
                  good_claims(world, jti="z"))
    assert verify(world, token).sub == "web"


def test_unknown_kid_reload_failure_past_grace_fails_closed(world):
    world.path.write_text("broken = [")
    world.clock.now = T0 + sa.REGISTRY_GRACE_SECONDS + 1
    stranger = forge(Ed25519PrivateKey.generate(), good_header(kid="ghost"), good_claims(world))
    denied(world, stranger, 503, "key_registry_unavailable")


@pytest.mark.parametrize("text", [
    "", "keys = []", "[[keys]]\nkid = 'a'\nsub = 'b'\npublic_key = 'AAAA'\ncapabilities = []",
    "[[keys]]\nkid = ''\nsub = 'b'\npublic_key = 'AAAA'\ncapabilities = []",
    "[[keys]]\nkid = 'a'\nsub = 'b'\npublic_key = 'AAAA'\ncapabilities = 'x'",
])
def test_registry_without_usable_keys_is_rejected(tmp_path, text):
    path = tmp_path / "r.toml"
    path.write_text(text)
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.KeyRegistry(path, Clock()).load_initial()


def test_duplicate_kid_in_registry_is_rejected():
    key = Ed25519PrivateKey.generate()
    text = registry_text(("a", "x", key, []), ("a", "y", key, []))
    with pytest.raises(ValueError):
        sa.parse_registry(text)


def test_missing_registry_file_refuses_startup(tmp_path):
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.KeyRegistry(tmp_path / "absent.toml", Clock()).load_initial()


# --- environment configuration ----------------------------------------------------------------


def test_environment_off_by_default_and_misconfiguration_refuses():
    assert sa.authenticator_from_environment(AUD, {}) is None
    assert sa.authenticator_from_environment(AUD, {sa.ENABLE_ENV: "off"}) is None
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.authenticator_from_environment(AUD, {sa.ENABLE_ENV: "maybe"})
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.authenticator_from_environment(AUD, {sa.ENABLE_ENV: "on"})  # no registry path
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.authenticator_from_environment(
            AUD, {sa.ENABLE_ENV: "on", sa.REGISTRY_ENV: "/nonexistent/registry.toml"}
        )


def test_environment_on_builds_an_authenticator(world):
    auth = sa.authenticator_from_environment(
        AUD, {sa.ENABLE_ENV: "1", sa.REGISTRY_ENV: str(world.path)}, world.clock
    )
    assert auth is not None and auth.audience == AUD


# --- signing side -----------------------------------------------------------------------------


def test_signing_key_from_env_pem_and_errors(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(sa.SIGNING_KEY_ENV, raw_seed(key))
    assert sa.load_signing_key().sign(b"m") == key.sign(b"m")
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pem_file = tmp_path / "k.pem"
    pem_file.write_bytes(pem)
    assert sa.load_signing_key(str(pem_file)).sign(b"m") == key.sign(b"m")
    monkeypatch.delenv(sa.SIGNING_KEY_ENV)
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.load_signing_key()
    monkeypatch.setenv(sa.SIGNING_KEY_ENV, "not a key !!")
    with pytest.raises(sa.ServiceAuthConfigError) as info:
        sa.load_signing_key()
    assert "not a key" not in str(info.value)
    from cryptography.hazmat.primitives.asymmetric import rsa
    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    rsa_file = tmp_path / "rsa.pem"
    rsa_file.write_bytes(rsa_pem)
    with pytest.raises(sa.ServiceAuthConfigError):
        sa.load_signing_key(str(rsa_file))


def test_a_base64url_seed_starting_with_a_dash_is_not_mistaken_for_pem(monkeypatch):
    seed = b"\xfb" + bytes(31)  # base64url of this starts with "-"
    text = sa.b64url_encode(seed)
    assert text.startswith("-")
    monkeypatch.setenv(sa.SIGNING_KEY_ENV, text)
    assert sa.load_signing_key().sign(b"m") == Ed25519PrivateKey.from_private_bytes(seed).sign(b"m")


def test_mint_uses_wall_clock_and_random_jti_by_default(world):
    token = sa.mint_assertion(
        kid="web-1", sub="web", audience=AUD, capability="c", engagement="e",
        classification="k", method="get", target="/x", key_path=world.key_file,
    )
    claims = sa._json_object(token.split(".")[1])
    assert claims["mth"] == "GET" and len(claims["jti"]) == 32
    assert claims["exp"] - claims["iat"] == 60 and "dlg" not in claims


# --- FastAPI dependency -----------------------------------------------------------------------


def make_app(world):
    app = FastAPI()
    dependency = sa.require_assertion(world.auth)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/process")
    async def process(request: Request, caller: sa.Caller = Depends(dependency)):
        return {"sub": caller.sub, "body": (await request.body()).decode()}

    @app.get("/artifacts/{name}")
    def artifact(name: str, caller: sa.Caller = Depends(dependency)):
        return {"name": name, "sub": caller.sub}

    @app.get("/files/{rest:path}")
    def files(rest: str, caller: sa.Caller = Depends(dependency)):
        return {"rest": rest}

    return app


def post(client, world, body=b'{"question":"q"}', **headers):
    return client.post("/process", content=body, headers=headers)


def test_health_is_open_and_other_routes_need_a_token(world):
    client = TestClient(make_app(world))
    assert client.get("/health").status_code == 200
    assert post(client, world).status_code == 401
    assert client.get("/artifacts/a").status_code == 401


def test_dependency_accepts_a_valid_request_and_body_still_reaches_the_handler(world):
    client = TestClient(make_app(world))
    token = mint(world)
    response = post(client, world, **{sa.ASSERTION_HEADER: token})
    assert response.status_code == 200
    assert response.json() == {"sub": "web", "body": '{"question":"q"}'}


def test_dependency_binds_query_route_and_replay(world):
    client = TestClient(make_app(world))
    token = mint(world, method="GET", target="/artifacts/a?v=1&v=2", body=b"")
    ok = client.get("/artifacts/a?v=1&v=2", headers={sa.ASSERTION_HEADER: token})
    assert ok.status_code == 200
    again = client.get("/artifacts/a?v=1&v=2", headers={sa.ASSERTION_HEADER: token})
    assert again.status_code == 401
    other = mint(world, method="GET", target="/artifacts/a?v=1&v=2", body=b"", jti="2")
    assert client.get("/artifacts/b?v=1&v=2", headers={sa.ASSERTION_HEADER: other}).status_code == 401


def test_dependency_rejects_bad_target_with_400_and_wrong_capability_with_403(world):
    client = TestClient(make_app(world))
    forged = mint_forged_target(world, "/files/a//b")
    bad = client.get("/files/a//b", headers={sa.ASSERTION_HEADER: forged})
    assert bad.status_code == 400
    wrong_cap = mint(world, capability="nope")
    assert post(client, world, **{sa.ASSERTION_HEADER: wrong_cap}).status_code == 403


def test_identity_in_body_or_headers_is_ignored(world):
    client = TestClient(make_app(world))
    body = b'{"question":"q","sub":"web","agent":"web"}'
    spoof = {"X-Agent": "web", "X-Viharbot-Sub": "web", "Authorization": "Bearer web"}
    assert post(client, world, body=body, **spoof).status_code == 401
    token = mint(world, body=body)
    response = post(client, world, body=body, **spoof, **{sa.ASSERTION_HEADER: token})
    assert response.status_code == 200 and response.json()["sub"] == "web"
    # The identity is the registry's, even when the body names a different one.
    other = mint(world, body=b'{"sub":"cipher"}', jti="x")
    response = post(client, world, body=b'{"sub":"cipher"}', **{sa.ASSERTION_HEADER: other})
    assert response.json()["sub"] == "web"


def test_missing_raw_path_fails_closed(world):
    dependency = sa.require_assertion(world.auth)
    scope = {"type": "http", "method": "POST", "headers": [], "query_string": b""}
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope, receive)
    import asyncio
    with pytest.raises(Exception) as info:
        asyncio.run(dependency(request))
    assert getattr(info.value, "status_code", None) == 401


# --- log hygiene ------------------------------------------------------------------------------


def test_logs_record_decisions_without_token_key_or_body(world, caplog):
    from viharbot.observability import JsonFormatter

    client = TestClient(make_app(world))
    secret_body = b'{"question":"sensitive-prompt-text"}'
    token = mint(world, body=secret_body)
    with caplog.at_level(logging.INFO, logger="service_auth"):
        post(client, world, body=secret_body, **{sa.ASSERTION_HEADER: token},
             **{"X-Request-ID": "req-123"})
        post(client, world, body=secret_body, **{sa.ASSERTION_HEADER: token})
        post(client, world, body=secret_body)
    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records
             if r.name == "service_auth"]
    assert [(entry["decision"], entry["reason"]) for entry in lines] == [
        ("allow", "verified"), ("deny", "replayed_token"), ("deny", "missing_token")]
    assert lines[0]["caller"] == "web" and lines[0]["request_id"] == "req-123"
    assert lines[0]["target"] == "/process" and lines[2]["caller"] is None
    text = json.dumps(lines) + caplog.text
    for forbidden in (token, token.split(".")[2], raw_seed(world.key), raw_public(world.key),
                      "sensitive-prompt-text"):
        assert forbidden not in text


def test_target_in_logs_excludes_the_query_string(world, caplog):
    client = TestClient(make_app(world))
    token = mint(world, method="GET", target="/artifacts/a?secret=hunter2", body=b"")
    with caplog.at_level(logging.INFO, logger="service_auth"):
        client.get("/artifacts/a?secret=hunter2", headers={sa.ASSERTION_HEADER: token})
    ours = [r for r in caplog.records if r.name == "service_auth"]
    assert ours and all("hunter2" not in str(r.__dict__) for r in ours)


# --- specialist app wiring --------------------------------------------------------------------


def load_specialist(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_KIND", "fact-check")
    monkeypatch.setenv("FACT_CHECK_MODEL", "svc/alias")
    for name in (sa.ENABLE_ENV, sa.REGISTRY_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    module = importlib.reload(importlib.import_module("viharbot.specialist_agent"))

    def no_inference(**_kwargs):
        raise RuntimeError("inference is disabled in these tests")

    monkeypatch.setattr(module, "build_agent", no_inference)  # never reach a real model
    return module


def test_specialist_behavior_is_unchanged_when_the_setting_is_off(monkeypatch, tmp_path):
    module = load_specialist(monkeypatch, tmp_path)
    assert module._authenticator is None and module._auth_dependencies == []
    client = TestClient(module.app)
    assert client.get("/health").json() == {"status": "ok", "agent": "fact-check"}
    assert client.get("/model").json()["agent"] == "fact-check"
    # No token: the request is not refused for authentication (it fails later at the stubbed model).
    assert client.post("/process", json={}).status_code not in (401, 403)
    assert client.post("/invocations", json={"input": {}}).status_code not in (401, 403)


def test_specialist_with_auth_on_refuses_to_start_without_keys(monkeypatch, tmp_path):
    with pytest.raises(sa.ServiceAuthConfigError):
        load_specialist(monkeypatch, tmp_path, **{sa.ENABLE_ENV: "on"})
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    with pytest.raises(sa.ServiceAuthConfigError):
        load_specialist(monkeypatch, tmp_path, **{sa.ENABLE_ENV: "on", sa.REGISTRY_ENV: str(empty)})
    # Leave a working module behind for other tests.
    load_specialist(monkeypatch, tmp_path)


def test_specialist_with_auth_on_protects_process_and_invocations_not_health(
    monkeypatch, tmp_path
):
    key = Ed25519PrivateKey.generate()
    registry = tmp_path / "registry.toml"
    registry.write_text(registry_text(("web-1", "web", key, ["specialist.process"])))
    key_file = tmp_path / "k"
    key_file.write_text(raw_seed(key))
    module = load_specialist(
        monkeypatch, tmp_path, **{sa.ENABLE_ENV: "on", sa.REGISTRY_ENV: str(registry)}
    )
    try:
        client = TestClient(module.app)
        assert client.get("/health").status_code == 200
        assert client.get("/ping").status_code == 200
        assert client.post("/process", json={}).status_code == 401
        assert client.post("/invocations", json={"input": {}}).status_code == 401
        body = b"{}"
        token = sa.mint_assertion(
            kid="web-1", sub="web", audience="fact-check", capability="specialist.process",
            engagement="e", classification="c", method="POST", target="/process", body=body,
            key_path=str(key_file),
        )
        # Authenticated: it now reaches the handler, which fails at the stubbed model (502).
        response = client.post(
            "/process", content=body,
            headers={sa.ASSERTION_HEADER: token, "Content-Type": "application/json"},
        )
        assert response.status_code == 502
        wrong = sa.mint_assertion(
            kid="web-1", sub="web", audience="synthesis", capability="specialist.process",
            engagement="e", classification="c", method="POST", target="/process", body=body,
            key_path=str(key_file),
        )
        assert client.post("/process", content=body, headers={sa.ASSERTION_HEADER: wrong,
                           "Content-Type": "application/json"}).status_code == 401
    finally:
        load_specialist(monkeypatch, tmp_path)


# --- no key material committed ----------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}


def test_no_private_key_material_is_committed():
    pem = re.compile(r"-----BEGIN [A-Z ]*" + "PRIVATE" + r" KEY-----")
    assignment = re.compile(re.escape(sa.SIGNING_KEY_ENV) + r"\s*[:=]\s*['\"]?[A-Za-z0-9_-]{32,}")
    offenders = []
    for current, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(current) / name
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if pem.search(text) or assignment.search(text):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []

