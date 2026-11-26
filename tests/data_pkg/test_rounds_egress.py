# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The harness egress guard. A compose service name that resolves
to a PUBLIC address (the host's resolver qualified ``opensearch`` with its
search domain, whose wildcard answered) is refused before any connection; so
is an undeclared name and a raw public address. Loopback, a declared name
that resolves privately, and the gateway are allowed. The install is tested
in a subprocess (an audit hook cannot be removed). No network is used: the
resolver is a fake and the refused connects never leave the process.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from legba.data.rounds.egress import EgressPolicy, is_private, vet_address, vet_name

SRC = Path(__file__).resolve().parents[2] / "src"


def _policy() -> EgressPolicy:
    return EgressPolicy(gateway_hosts=frozenset({"api.gateway.example"}),
                        container_names=frozenset({"opensearch", "postgres"}))


def test_a_container_name_resolving_public_is_refused_and_private_is_allowed():
    p = _policy()
    ok, why = vet_name(p, "opensearch", ["8.8.4.4"])
    assert not ok and why == "container_name_resolved_public:8.8.4.4"
    assert vet_name(p, "postgres", ["172.18.0.4"]) == (True, "container_name_private")
    assert vet_address(p, "172.18.0.4")[0] and not vet_address(p, "8.8.4.4")[0]


def test_undeclared_names_and_raw_public_addresses_are_refused():
    p = _policy()
    assert vet_name(p, "opensearch.example.org", ["10.0.0.5"]) == (False, "undeclared_host")
    assert vet_name(p, "198.51.100.9", ["198.51.100.9"])[0] is False
    assert vet_name(p, "127.0.0.1", ["127.0.0.1"])[0] and vet_name(p, "localhost", ["::1"])[0]
    assert vet_name(p, "localhost", ["198.51.100.9"])[0] is False


def test_the_gateway_is_allowed_at_the_addresses_it_resolved_to():
    p = _policy()
    assert vet_name(p, "api.gateway.example", ["198.51.100.20"]) == (True, "gateway_host")
    assert vet_address(p, "198.51.100.20") == (True, "vetted")
    assert vet_address(p, "198.51.100.21")[0] is False
    assert is_private("10.1.2.3") and not is_private("8.8.8.8")


def test_the_installed_guard_refuses_before_connecting():
    code = textwrap.dedent("""
        import socket
        from legba.data.rounds.egress import EgressPolicy, EgressRefused, install
        table = {"opensearch": "8.8.4.4", "postgres": "172.18.0.4", "api.gateway.example": "127.0.0.9"}
        def fake(host, port, *a, **k):
            ip = table.get(host, "198.51.100.1")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        pol = install(EgressPolicy(gateway_hosts=frozenset({"api.gateway.example"}),
                                   container_names=frozenset({"opensearch", "postgres"})), resolver=fake)
        out = []
        for host in ("opensearch", "nowhere.example", "postgres", "api.gateway.example"):
            try:
                socket.getaddrinfo(host, 9200)
                out.append(f"{host}:resolved")
            except EgressRefused as e:
                out.append(f"{host}:refused")
        s = socket.socket(); s.settimeout(0.2)
        try:
            s.connect(("198.51.100.77", 9))
            out.append("public:connected")
        except EgressRefused:
            out.append("public:refused")
        except OSError as e:
            out.append("public:oserror")
        s.close()
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        c = socket.socket(); c.settimeout(1)
        c.connect(srv.getsockname()); out.append("loopback:connected"); c.close(); srv.close()
        print(";".join(out)); print(len(pol.refused))
    """)
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                         env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"})
    assert res.returncode == 0, res.stderr
    first, count = res.stdout.strip().splitlines()
    assert first == ("opensearch:refused;nowhere.example:refused;postgres:resolved;"
                     "api.gateway.example:resolved;public:refused;loopback:connected")
    assert int(count) == 3


def test_the_harness_installs_the_guard_before_its_first_connection():
    text = (Path(__file__).resolve().parents[2] / "scripts" / "bakeoff" / "arm_round.py").read_text()
    for fn in ("def _run_main", "def _probe_main", "def _calibrate_main"):
        body = text.split(fn, 1)[1].split("\ndef ", 1)[0]
        assert "guard_egress()" in body, fn
        assert body.index("guard_egress()") < min(
            (body.index(k) for k in ("build_planes(", "build_port(") if k in body), default=10**9), fn
