# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for cert utility helper functions."""

import subprocess
from pathlib import Path

import pytest

import certs


def test_gen_certs_runs_openssl_pipeline(monkeypatch, tmp_path):
    """gen_certs should execute the expected openssl flow and return generated file contents."""
    ssl_template = tmp_path / "ssl.conf.j2"
    ssl_template.write_text(
        "service={{ service_name }} ns={{ namespace }} webhook={{ webhook_server_service }}"
    )
    monkeypatch.setattr(certs, "SSL_CONFIG_FILE", str(ssl_template))

    calls = []

    def fake_check_call(cmd):
        calls.append(cmd)
        cmd = [str(token) for token in cmd]
        if "-out" in cmd:
            out_file = Path(cmd[cmd.index("-out") + 1])
            out_file.write_text(f"generated-{out_file.name}")

    monkeypatch.setattr(certs, "check_call", fake_check_call)

    out = certs.gen_certs(
        service_name="llmisvc-controller",
        namespace="kubeflow",
        webhook_service="llmisvc-webhook-server-service",
    )

    assert len(calls) == 5
    assert out["cert"] == "generated-cert.pem"
    assert out["key"] == "generated-server.key"
    assert out["ca"] == "generated-ca.crt"


def _self_signed_cert(tmp_path: Path, days: int) -> str:
    subprocess.check_call(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=test",
            "-days",
            str(days),
            "-keyout",
            str(tmp_path / "key.pem"),
            "-out",
            str(tmp_path / "cert.pem"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return (tmp_path / "cert.pem").read_text()


@pytest.mark.parametrize("days, expected", [(365, False), (10, True)])
def test_cert_expires_soon(tmp_path, days, expected):
    """Certificates expiring within the renewal window are reported as expiring."""
    assert certs.cert_expires_soon(_self_signed_cert(tmp_path, days)) is expected


def test_cert_expires_soon_on_invalid_cert():
    """An unparsable certificate is treated as expiring so it gets replaced."""
    assert certs.cert_expires_soon("not a certificate") is True
