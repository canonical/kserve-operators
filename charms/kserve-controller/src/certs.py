# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.


import tempfile
from pathlib import Path
from subprocess import check_call

from jinja2 import Template

SSL_CONFIG_FILE = "src/templates/ssl.conf.j2"


def gen_certs(service_name: str, namespace: str, webhook_service: str):
    """Generate certificates."""

    template = Template(Path(SSL_CONFIG_FILE).read_text())
    ssl_conf = template.render(
        service_name=str(service_name),
        namespace=str(namespace),
        webhook_server_service=str(webhook_service),
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        (tmp_path / "ssl.conf").write_text(ssl_conf)

        # execute OpenSSL commands
        check_call(["openssl", "genrsa", "-out", tmp_path / "ca.key", "2048"])
        check_call(["openssl", "genrsa", "-out", tmp_path / "server.key", "2048"])
        check_call(
            [
                "openssl",
                "req",
                "-x509",
                "-new",
                "-sha256",
                "-nodes",
                "-days",
                "3650",
                "-key",
                tmp_path / "ca.key",
                "-subj",
                "/CN=127.0.0.1",
                "-out",
                tmp_path / "ca.crt",
            ]
        )
        check_call(
            [
                "openssl",
                "req",
                "-new",
                "-sha256",
                "-key",
                tmp_path / "server.key",
                "-out",
                tmp_path / "server.csr",
                "-config",
                tmp_path / "ssl.conf",
            ]
        )
        check_call(
            [
                "openssl",
                "x509",
                "-req",
                "-sha256",
                "-in",
                tmp_path / "server.csr",
                "-CA",
                tmp_path / "ca.crt",
                "-CAkey",
                tmp_path / "ca.key",
                "-CAcreateserial",
                "-out",
                tmp_path / "cert.pem",
                "-days",
                "365",
                "-extensions",
                "v3_ext",
                "-extfile",
                tmp_path / "ssl.conf",
            ]
        )

        ret_certs = {
            "cert": (tmp_path / "cert.pem").read_text(),
            "key": (tmp_path / "server.key").read_text(),
            "ca": (tmp_path / "ca.crt").read_text(),
        }

        # cleanup temporary files
        for file in tmp_path.glob("cert-gen-*"):
            file.unlink()

    return ret_certs
