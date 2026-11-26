from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section_between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_egress_proxy_is_the_only_member_of_the_internal_egress_network():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    proxy = section_between(compose, "  egress-proxy:\n", "  worker:\n")
    broker = section_between(compose, "  broker:\n", "  worker:\n")
    worker = section_between(compose, "  worker:\n", "  api:\n")
    api = section_between(compose, "  api:\n", "\nvolumes:")
    networks = compose.split("\nnetworks:\n", 1)[1]

    assert "internal: true" in networks
    assert "      - egress_internal\n      - egress_public" in proxy
    assert "    ports:" not in proxy
    assert "      - egress_internal" not in broker
    assert "      - egress_internal" not in worker
    assert "      - egress_internal" not in api


def test_egress_proxy_blocks_private_and_unapproved_destinations():
    policy = (ROOT / "egress-proxy" / "squid.conf").read_text(encoding="utf-8")
    lines = [line.strip() for line in policy.splitlines() if line.strip()]

    to_local = " ".join(
        line for line in lines if line.startswith("acl to_local dst ")
    )
    required_ranges = (
        "192.88.99.0/24",
        "::/96",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2002::/16",
        "3fff::/20",
    )
    assert all(address_range in to_local for address_range in required_ranges)
    assert "http_access deny to_local" in lines
    assert "http_access deny CONNECT !SSL_ports" in lines
    assert (
        "acl allowed_domains dstdomain .github.com .githubusercontent.com "
        ".gitlab.com .bitbucket.org .pypi.org .pythonhosted.org"
    ) in lines
    assert lines.index("http_access deny to_local") < lines.index(
        "http_access allow allowed_domains"
    )
    access_rules = [line for line in lines if line.startswith("http_access ")]
    assert access_rules[-1] == "http_access deny all"
    assert "http_access allow all" not in lines
