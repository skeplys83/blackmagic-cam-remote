"""HTTPS for the dashboard (BCR_TLS): your own certificate, or a self-signed one kept in the data dir."""
import os
import re
import shutil
import socket
import ssl
import subprocess

from . import config


def tls_context():
    """HTTPS context from BCR_TLS_CERT/BCR_TLS_KEY, or a self-signed certificate kept in the data dir."""
    cert, key = os.environ.get("BCR_TLS_CERT"), os.environ.get("BCR_TLS_KEY")
    if not cert:
        d = config.DATA / "tls"
        cert, key = str(d / "cert.pem"), str(d / "key.pem")
        if not os.path.exists(cert):
            if not shutil.which("openssl"):
                raise RuntimeError("BCR_TLS=1 needs openssl to create a certificate (or set BCR_TLS_CERT/BCR_TLS_KEY)")
            d.mkdir(parents=True, exist_ok=True)
            names = ["DNS:localhost", "IP:127.0.0.1", "DNS:" + socket.gethostname()]
            for h in filter(None, (x.strip() for x in os.environ.get("BCR_TLS_HOSTS", "").split(","))):
                names.append(("IP:" if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", h) else "DNS:") + h)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                            "-nodes", "-days", "3650", "-subj", "/CN=Blackmagic Cam Remote", "-keyout", key, "-out", cert,
                            "-addext", "subjectAltName=" + ",".join(names)], check=True, capture_output=True)
            os.chmod(key, 0o600)
            print("Created a self-signed certificate:", cert, flush=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx
