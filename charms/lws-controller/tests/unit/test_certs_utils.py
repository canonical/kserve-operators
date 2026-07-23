# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for cert utility helper functions."""

from pathlib import Path

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
        service_name="lws-controller",
        namespace="kubeflow",
        webhook_service="lws-webhook-service",
    )

    assert len(calls) == 5
    assert out["cert"] == "generated-cert.pem"
    assert out["key"] == "generated-server.key"
    assert out["ca"] == "generated-ca.crt"
