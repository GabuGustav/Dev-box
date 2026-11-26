from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import docker
import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.getenv("DEVBOX_RUN_DOCKER_INTEGRATION") != "1",
    reason="Requires the Linux Docker integration environment.",
)


DNS_SERVER_SCRIPT = r"""
import socketserver
import struct


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        packet, sock = self.request
        offset = 12
        while offset < len(packet) and packet[offset] != 0:
            offset += packet[offset] + 1
        if offset + 5 > len(packet):
            return
        query_type = struct.unpack("!H", packet[offset + 1:offset + 3])[0]
        question = packet[12:offset + 5]
        if query_type == 1:
            answer_type = 1
            address = b"\x7f\x00\x00\x01"
        elif query_type == 28:
            answer_type = 28
            address = bytes.fromhex("00000000000000000000000000000001")
        else:
            answer_type = 0
            address = b""
        answer_count = 1 if address else 0
        header = struct.pack(
            "!HHHHHH",
            struct.unpack("!H", packet[:2])[0],
            0x8180,
            1,
            answer_count,
            0,
            0,
        )
        response = header + question
        if address:
            response += (
                b"\xc0\x0c"
                + struct.pack("!HHIH", answer_type, 1, 1, len(address))
                + address
            )
        sock.sendto(response, self.client_address)


class Server(socketserver.ThreadingUDPServer):
    allow_reuse_address = True


Server(("0.0.0.0", 53), Handler).serve_forever()
"""


PROBE_SCRIPT = r"""
import socket
import sys
import time

proxy_ip = sys.argv[1]
targets = (
    "github.com:443",
    "127.0.0.1:443",
    "[::1]:443",
    "[64:ff9b::7f00:1]:443",
    "[2002:7f00:1::]:443",
    "example.com:443",
    "github.com:22",
)

for target in targets:
    response = b""
    for attempt in range(40):
        try:
            connection = socket.create_connection((proxy_ip, 3128), timeout=2)
            break
        except OSError:
            if attempt == 39:
                raise
            time.sleep(0.25)
    request = (
        f"CONNECT {target} HTTP/1.1\r\n"
        f"Host: {target}\r\n"
        "Proxy-Connection: close\r\n\r\n"
    )
    connection.sendall(request.encode("ascii"))
    while b"\r\n" not in response:
        chunk = connection.recv(4096)
        if not chunk:
            break
        response += chunk
    connection.close()
    status = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    print(f"{target} {status}")
    if " 403 " not in status:
        raise SystemExit(f"Proxy did not deny {target}: {status}")
"""


def test_proxy_denies_private_dns_answers_and_special_destination_ranges(tmp_path):
    client = docker.from_env()
    suffix = uuid4().hex[:10]
    network = None
    dns_container = None
    proxy_container = None
    client_container = None
    proxy_image = f"devbox-egress-proxy:test-{suffix}"

    try:
        client.images.build(
            path=str(ROOT),
            dockerfile="egress-proxy.Dockerfile",
            tag=proxy_image,
            rm=True,
        )
        network = client.networks.create(
            name=f"devbox-egress-test-{suffix}",
            driver="bridge",
            internal=True,
        )
        dns_container = client.containers.run(
            "python:3.12-slim",
            command=["python3", "-c", DNS_SERVER_SCRIPT],
            name=f"devbox-egress-dns-{suffix}",
            network=network.name,
            detach=True,
            user="0:0",
            cap_drop=["ALL"],
            cap_add=["NET_BIND_SERVICE"],
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=4m"},
            security_opt=["no-new-privileges:true"],
            mem_limit="128m",
            pids_limit=16,
        )
        dns_container.reload()
        dns_ip = dns_container.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]
        config_path = tmp_path / "squid.conf"
        base_config = (ROOT / "egress-proxy" / "squid.conf").read_text(encoding="utf-8")
        config_path.write_text(
            f"dns_nameservers {dns_ip}\n" + base_config,
            encoding="utf-8",
        )
        proxy_container = client.containers.run(
            proxy_image,
            name=f"devbox-egress-proxy-{suffix}",
            network=network.name,
            detach=True,
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="256m",
            pids_limit=64,
            volumes={
                str(config_path): {
                    "bind": "/etc/squid/squid.conf",
                    "mode": "ro",
                }
            },
        )
        proxy_container.reload()
        proxy_ip = proxy_container.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]
        client_container = client.containers.run(
            "python:3.12-slim",
            command=["python3", "-c", PROBE_SCRIPT, proxy_ip],
            network=network.name,
            detach=True,
            user="10001:10001",
            cap_drop=["ALL"],
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=4m"},
            security_opt=["no-new-privileges:true"],
            mem_limit="128m",
            pids_limit=16,
        )
        result = client_container.wait(timeout=60)
        output = client_container.logs(stdout=True, stderr=True).decode(
            "utf-8", errors="replace"
        )
        assert result["StatusCode"] == 0, output
        assert "github.com:443" in output
        assert "[64:ff9b::7f00:1]:443" in output
        assert "[2002:7f00:1::]:443" in output
    finally:
        for container in (client_container, proxy_container, dns_container):
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.NotFound:
                    pass
        if network is not None:
            try:
                network.remove()
            except docker.errors.NotFound:
                pass
        try:
            client.images.remove(proxy_image, force=True)
        except docker.errors.ImageNotFound:
            pass
        client.close()
