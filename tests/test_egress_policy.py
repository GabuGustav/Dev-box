import yaml
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_egress_proxy_is_the_only_member_of_the_internal_egress_network():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    internal = compose["networks"]["egress_internal"]
    proxy = compose["services"]["egress-proxy"]

    assert internal["internal"] is True
    assert proxy["networks"] == ["egress_internal", "egress_public"]
    assert "ports" not in proxy
    assert "egress_internal" not in compose["services"]["worker"].get("networks", [])
    assert "egress_internal" not in compose["services"]["api"].get("networks", [])


def test_egress_proxy_blocks_private_and_unapproved_destinations():
    policy = (ROOT / "egress-proxy" / "squid.conf").read_text(encoding="utf-8")
    lines = [line.strip() for line in policy.splitlines() if line.strip()]

    assert any(line.startswith("acl to_local dst ") for line in lines)
    assert "http_access deny to_local" in lines
    assert (
        "acl allowed_domains dstdomain .github.com .githubusercontent.com "
        ".gitlab.com .bitbucket.org .pypi.org .pythonhosted.org"
    ) in lines
    assert lines.index("http_access deny to_local") < lines.index(
        "http_access allow allowed_domains"
    )
    assert lines[-1] == "http_access deny all"
    assert "http_access allow all" not in lines
