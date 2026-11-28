"""The outbound firewall for the portal's containers (D101). The VM is out of
reach: these run infra/poc/egress-firewall.sh with a fake docker and a fake
iptables (which keeps its chains in a JSON file), evaluate the generated rules
against packets, read harden-host.sh and the other scripts, and check the
"Outbound firewall" health check."""

from __future__ import annotations

import datetime
import ipaddress
import json
import shlex
import shutil
import subprocess  # nosec B404 - runs the repository's own scripts
from pathlib import Path

import pytest
from django.utils import timezone

POC = Path("infra/poc")
SCRIPT = POC / "egress-firewall.sh"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(
    BASH is None or shutil.which("flock") is None or shutil.which("sha256sum") is None,
    reason="bash, flock and sha256sum needed",
)
DEFAULT_ID = "a1" * 32
EDGE_ID = "b2" * 32
DEFAULT_BR = "br-" + DEFAULT_ID[:12]
EDGE_BR = "br-" + EDGE_ID[:12]

FAKE_IPTABLES = r"""#!/usr/bin/env python3
import json, os, shlex, sys
name = os.path.basename(sys.argv[0])
fam = "6" if name.startswith("ip6") else "4"
path = os.path.join(os.environ["FAKE_IPT_STATE"], "v" + fam + ".json")
with open(os.path.join(os.environ["FAKE_IPT_STATE"], "calls.log"), "a") as log:
    log.write(name + " " + " ".join(sys.argv[1:]) + "\n")
if fam == "6" and os.environ.get("FAKE_NO_IP6"):
    sys.exit(3)
try:
    state = json.load(open(path))
except FileNotFoundError:
    state = {"FORWARD": ["-A FORWARD -j DOCKER-USER"], "DOCKER-USER": []}
    if fam == "6":
        state = {"FORWARD": []}
def save():
    json.dump(state, open(path, "w"))
args = [a for a in sys.argv[1:] if a != "-w"]
if name.endswith("-restore"):
    assert args == ["--noflush"], args
    text = sys.stdin.read().splitlines()
    assert text[0] == "*filter" and text[-1] == "COMMIT", text
    for line in text[1:-1]:
        if line.startswith(":"):
            state[line[1:].split()[0]] = []
        elif line.startswith("-A "):
            chain = line.split()[1]
            if chain not in state:
                sys.exit("no chain " + chain)
            state[chain].append(line)
        else:
            sys.exit("bad line " + line)
    save(); sys.exit(0)
op = args[0]
if op == "-n" and args[1] == "-L":
    sys.exit(0 if args[2] in state else 1)
chain = args[1]
rest = " ".join(args[2:])
if op == "-S":
    if chain not in state: sys.exit(1)
    print(("-P %s ACCEPT" % chain) if chain == "FORWARD" else ("-N " + chain))
    for rule in state[chain]: print(rule)
    sys.exit(0)
if op == "-N":
    if chain in state: sys.exit(1)
    state[chain] = []; save(); sys.exit(0)
if chain not in state: sys.exit(1)
if op == "-C":
    sys.exit(0 if ("-A %s %s" % (chain, rest)) in state[chain] else 1)
if op == "-I":
    rule = "-A %s %s" % (chain, " ".join(args[3:]))
    state[chain].insert(int(args[2]) - 1, rule); save(); sys.exit(0)
if op == "-A":
    state[chain].append("-A %s %s" % (chain, rest)); save(); sys.exit(0)
if op == "-D":
    rule = "-A %s %s" % (chain, rest)
    if rule not in state[chain]: sys.exit(1)
    state[chain].remove(rule); save(); sys.exit(0)
if op == "-F":
    state[chain] = []; save(); sys.exit(0)
if op == "-X":
    del state[chain]; save(); sys.exit(0)
sys.exit("unsupported " + " ".join(args))
"""

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
if os.environ.get("FAKE_DOCKER_DOWN"):
    sys.exit("Cannot connect to the Docker daemon")
nets = json.loads(os.environ.get("FAKE_NETS", "{}"))
args = sys.argv[1:]
if args[:2] == ["network", "ls"]:
    label = next(a for a in args if a.startswith("label=com.docker.compose.project="))
    project = label.split("=", 2)[2]
    assert project in {"oak-creek-portal", "hoa-restore-drill"}, args
    print("\n".join(n for n in nets if n.startswith(project + "_")))
elif args[:2] == ["network", "inspect"]:
    driver, nid, opt = nets[args[-1]]
    print(f"{driver} {nid} {opt}")
else:
    sys.exit("unsupported")
"""


class Fw:
    """A fake VM: docker networks, iptables state and the host's resolvers."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "ipt"
        self.bin.mkdir()
        self.state.mkdir()
        for name in ("iptables", "ip6tables", "iptables-restore", "ip6tables-restore"):
            (self.bin / name).write_text(FAKE_IPTABLES)
            (self.bin / name).chmod(0o755)
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        (self.bin / "systemctl").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "systemctl").chmod(0o755)
        self.resolv = tmp_path / "resolv.conf"
        self.resolv.write_text("nameserver 127.0.0.53\nnameserver 10.20.0.1\noptions edns0\n")
        self.local_conf = tmp_path / "local.conf"
        self.nets = {
            "oak-creek-portal_default": ["bridge", DEFAULT_ID, ""],
            "oak-creek-portal_edge": ["bridge", EDGE_ID, ""],
        }
        self.extra_env: dict[str, str] = {}

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOA_EGRESS_CONF": str(POC / "egress-firewall.conf"),
            "HOA_EGRESS_LOCAL_CONF": str(self.local_conf),
            "HOA_EGRESS_LOCK": str(self.tmp / "lock"),
            "HOA_EGRESS_STATE_DIR": str(self.tmp / "saved"),
            "HOA_EGRESS_RESOLV": f"{self.tmp}/missing.conf {self.resolv}",
            "FAKE_IPT_STATE": str(self.state),
            "FAKE_NETS": json.dumps(self.nets),
            **self.extra_env,
        }
        return subprocess.run(  # noqa: S603  # nosec B603
            [BASH, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60
        )

    def chains(self, family: str = "4") -> dict:
        path = self.state / f"v{family}.json"
        return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def fw(tmp_path):
    return Fw(tmp_path)


def _rules(fw: Fw) -> list[str]:
    result = fw.run("show")
    assert result.returncode == 0, result.stderr
    return result.stdout.split("# IPv6")[0].splitlines()


# --- A tiny evaluator of the generated rules ------------------------------------------------


def _verdict(chains: dict, packet: dict) -> str:
    """Walks DOCKER-USER for a packet; RETURN from it means Docker's own rules
    decide (allowed), DROP means dropped."""

    def walk(chain: str) -> str | None:
        for rule in chains.get(chain, []):
            tokens = shlex.split(rule)[2:]
            target, ok = None, True
            for tok, val in zip(tokens[::2], tokens[1::2], strict=False):
                if tok == "-i":
                    ok &= packet["in"] == val
                elif tok == "-o":
                    ok &= packet.get("out", "eth0") == val
                elif tok == "-d":
                    ok &= ipaddress.ip_address(packet["dst"]) in ipaddress.ip_network(val)
                elif tok == "-p":
                    ok &= packet.get("proto", "tcp") == val
                elif tok == "--dport":
                    ok &= packet.get("port") == int(val)
                elif tok == "--dports":
                    ok &= packet.get("port") in {int(p) for p in val.split(",")}
                elif tok == "--ctstate":
                    ok &= packet.get("state", "NEW") in val.split(",")
                elif tok == "-j":
                    target = val
                    break
                # Anything else (-m <module>, --comment, --limit) does not filter here.
            if not ok:
                continue
            if target in {"RETURN"}:
                return "RETURN"
            if target == "DROP":
                return "DROP"
            if target == "LOG":
                continue
            result = walk(target)
            if result is not None:
                return result
        return None

    return walk("DOCKER-USER") or "RETURN"


def _applied(fw: Fw) -> dict:
    result = fw.run("apply")
    assert result.returncode == 0, result.stderr
    return fw.chains()


ALLOWED = [
    {"in": DEFAULT_BR, "dst": "198.51.99.10", "port": 443},  # Stripe, SMTP2Go API, R2...
    {"in": DEFAULT_BR, "dst": "203.0.114.5", "port": 587},
    {"in": DEFAULT_BR, "dst": "203.0.114.5", "port": 465},
    {"in": DEFAULT_BR, "dst": "203.0.114.5", "port": 2525},
    {"in": DEFAULT_BR, "dst": "203.0.114.5", "port": 993},
    {"in": EDGE_BR, "dst": "198.41.192.7", "port": 7844},
    {"in": EDGE_BR, "dst": "198.41.192.7", "port": 7844, "proto": "udp"},
    {"in": EDGE_BR, "dst": "198.51.99.10", "port": 443},  # web's traffic may leave by edge
    # Within one portal network: Postgres, the file store, ClamAV, cloudflared -> web.
    {"in": DEFAULT_BR, "out": DEFAULT_BR, "dst": "172.18.0.5", "port": 5432},
    {"in": DEFAULT_BR, "out": DEFAULT_BR, "dst": "172.18.0.7", "port": 3310},
    {"in": EDGE_BR, "out": EDGE_BR, "dst": "172.30.50.130", "port": 8000},
    # DNS to the host's resolver (private, still allowed on 53 only).
    {"in": DEFAULT_BR, "dst": "10.20.0.1", "port": 53, "proto": "udp"},
    {"in": DEFAULT_BR, "dst": "10.20.0.1", "port": 53},
    # Replies, and other Docker networks on the host, are not this firewall's business.
    {"in": DEFAULT_BR, "dst": "192.168.1.20", "port": 50000, "state": "ESTABLISHED"},
    {"in": "docker0", "dst": "192.168.1.20", "port": 25},
]

DROPPED = [
    # Private and special ranges on allowed ports (DNS rebinding / SSRF).
    {"in": DEFAULT_BR, "dst": "192.168.1.20", "port": 443},
    {"in": DEFAULT_BR, "dst": "192.168.1.20", "port": 587},
    {"in": DEFAULT_BR, "dst": "10.0.0.2", "port": 993},
    {"in": DEFAULT_BR, "dst": "10.20.0.1", "port": 443},  # the resolver, but not DNS
    {"in": DEFAULT_BR, "dst": "172.17.0.1", "port": 443},
    {"in": DEFAULT_BR, "dst": "100.64.3.3", "port": 443},
    {"in": DEFAULT_BR, "dst": "169.254.169.254", "port": 443},  # cloud metadata
    {"in": DEFAULT_BR, "dst": "169.254.169.254", "port": 80},
    {"in": DEFAULT_BR, "dst": "127.0.0.1", "port": 443},
    {"in": EDGE_BR, "dst": "192.168.1.8", "port": 7844},
    {"in": EDGE_BR, "dst": "192.168.1.8", "port": 8006},  # Proxmox
    # Between the portal's two networks (default -> edge): not one network.
    {"in": DEFAULT_BR, "out": EDGE_BR, "dst": "172.30.50.10", "port": 443},
    # Ports that are not on the list.
    {"in": DEFAULT_BR, "dst": "203.0.114.5", "port": 25},  # no direct-to-MX
    {"in": EDGE_BR, "dst": "203.0.114.5", "port": 25},
    {"in": DEFAULT_BR, "dst": "198.51.99.10", "port": 80},
    {"in": DEFAULT_BR, "dst": "198.51.99.10", "port": 22},
    {"in": DEFAULT_BR, "dst": "198.51.99.10", "port": 443, "proto": "udp"},
    {"in": DEFAULT_BR, "dst": "198.41.192.7", "port": 7844},  # tunnel port: edge only
    {"in": DEFAULT_BR, "dst": "1.1.1.1", "port": 53, "proto": "udp"},  # other resolvers
]


@needs_bash
@pytest.mark.parametrize("packet", ALLOWED)
def test_allowed_traffic(fw, packet):
    assert _verdict(_applied(fw), packet) == "RETURN"


@needs_bash
@pytest.mark.parametrize("packet", DROPPED)
def test_dropped_traffic(fw, packet):
    assert _verdict(_applied(fw), packet) == "DROP"


@needs_bash
def test_the_generated_rules_contain_each_allow_and_deny(fw):
    rules = _rules(fw)
    body = "\n".join(rules)
    assert rules[0] == "*filter" and rules[-1] == "COMMIT"
    assert ":HOA-EGRESS - [0:0]" in rules and ":HOA-EGRESS-DROP - [0:0]" in rules
    assert "--ctstate RELATED,ESTABLISHED -j RETURN" in rules[3]
    for br in (DEFAULT_BR, EDGE_BR):
        assert f"-A HOA-EGRESS -i {br} -o {br} -j RETURN" in rules
        assert f"-A HOA-EGRESS -i {br} -d 10.20.0.1/32 -p udp -m udp --dport 53 -j RETURN" in rules
        assert f"-A HOA-EGRESS -i {br} -d 10.20.0.1/32 -p tcp -m tcp --dport 53 -j RETURN" in rules
        for cidr in (
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
            "169.254.0.0/16",
            "127.0.0.0/8",
        ):
            assert f"-A HOA-EGRESS -i {br} -d {cidr} -j HOA-EGRESS-DROP" in rules
        allow = f"-A HOA-EGRESS -i {br} -p tcp -m multiport --dports 443,587,465,2525,993 -j RETURN"
        assert allow in rules
        # Private ranges are dropped before any port is allowed (even 443).
        last_drop = max(
            i for i, r in enumerate(rules) if r.startswith(f"-A HOA-EGRESS -i {br} -d 1")
        )
        assert last_drop < rules.index(allow)
        assert rules.index(allow) < rules.index(f"-A HOA-EGRESS -i {br} -j HOA-EGRESS-DROP")
    assert f"-A HOA-EGRESS -i {EDGE_BR} -p udp -m multiport --dports 7844 -j RETURN" in rules
    assert f"-A HOA-EGRESS -i {DEFAULT_BR} -p udp -m multiport --dports 7844" not in body
    assert "127.0.0.53" not in body  # the stub resolver is the host's own, not a target
    assert "--dports 25" not in body and ",25," not in body
    assert rules[-3].startswith("-A HOA-EGRESS-DROP -m limit --limit 6/minute")
    assert '--log-prefix "hoa-egress-drop: "' in rules[-3]
    assert rules[-2] == "-A HOA-EGRESS-DROP -j DROP"


@needs_bash
def test_ipv6_from_the_portal_networks_never_leaves_them(fw):
    _applied(fw)
    v6 = fw.chains("6")
    assert v6["FORWARD"][0] == "-A FORWARD -j HOA-EGRESS"  # no DOCKER-USER for IPv6 here
    rules = v6["HOA-EGRESS"]
    for br in (DEFAULT_BR, EDGE_BR):
        assert f"-A HOA-EGRESS -i {br} -o {br} -j RETURN" in rules
        assert f"-A HOA-EGRESS -i {br} -j HOA-EGRESS-DROP" in rules
    assert not any("--dports" in r or " -d " in r for r in rules)  # no IPv6 allow list


@needs_bash
def test_reapplying_never_duplicates_and_follows_a_recreated_network(fw):
    first = _applied(fw)
    assert first["DOCKER-USER"] == ["-A DOCKER-USER -j HOA-EGRESS"]
    again = fw.run("apply")
    assert again.returncode == 0 and "already on and current" in again.stdout
    assert fw.chains() == first
    assert fw.run("apply", "--quiet").stdout == ""
    assert fw.run("status").stdout.strip() == "active 2"
    # `docker compose down && up` makes a new bridge: stale until re-applied.
    fw.nets["oak-creek-portal_default"][1] = "c3" * 32
    status = fw.run("status")
    assert status.stdout.strip() == "stale 2" and status.returncode != 0
    after = _applied(fw)
    assert after["DOCKER-USER"] == ["-A DOCKER-USER -j HOA-EGRESS"]
    assert not any(DEFAULT_BR in r for r in after["HOA-EGRESS"])
    assert any("br-c3c3c3c3c3c3" in r for r in after["HOA-EGRESS"])
    assert len(after["HOA-EGRESS"]) == len(first["HOA-EGRESS"])
    assert fw.chains("6")["FORWARD"].count("-A FORWARD -j HOA-EGRESS") == 1


@needs_bash
def test_the_jump_is_moved_back_to_the_first_rule(fw):
    _applied(fw)
    state = fw.chains()
    state["DOCKER-USER"] = [
        "-A DOCKER-USER -j ACCEPT",
        "-A DOCKER-USER -j HOA-EGRESS",
        "-A DOCKER-USER -j HOA-EGRESS",
    ]
    (fw.state / "v4.json").write_text(json.dumps(state))
    assert fw.run("status").stdout.startswith("missing")
    after = _applied(fw)
    assert after["DOCKER-USER"] == ["-A DOCKER-USER -j HOA-EGRESS", "-A DOCKER-USER -j ACCEPT"]


@needs_bash
def test_status_states(fw):
    assert fw.run("status").stdout.strip() == "missing 2"
    _applied(fw)
    assert fw.run("status").returncode == 0
    fw.nets = {}
    _applied(fw)
    result = fw.run("status")
    assert result.stdout.strip() == "no-networks 0" and result.returncode != 0
    assert not any("-i " in r for r in fw.chains()["HOA-EGRESS"])


@needs_bash
def test_ipv4_only_hosts_and_networks_of_other_drivers(fw):
    fw.extra_env["FAKE_NO_IP6"] = "1"
    fw.nets["oak-creek-portal_hostnet"] = ["host", "d4" * 32, ""]
    fw.nets["oak-creek-portal_named"] = ["bridge", "e5" * 32, "hoa-named"]
    chains = _applied(fw)
    assert any("-i hoa-named " in r for r in chains["HOA-EGRESS"])
    assert not any("d4d4" in r for r in chains["HOA-EGRESS"])
    assert fw.run("status").stdout.strip() == "active 3"


@needs_bash
def test_docker_down_changes_nothing(fw):
    before = _applied(fw)
    fw.nets["oak-creek-portal_default"][1] = "c3" * 32
    fw.extra_env["FAKE_DOCKER_DOWN"] = "1"
    result = fw.run("apply")
    assert result.returncode != 0 and "nothing changed" in result.stderr
    assert fw.chains() == before


@needs_bash
def test_dns_falls_back_to_dockers_default_resolvers(fw):
    fw.resolv.write_text("nameserver 127.0.0.53\n")
    body = "\n".join(_rules(fw))
    assert "-d 8.8.8.8/32" in body and "-d 8.8.4.4/32" in body
    fw.local_conf.write_text("DNS_SERVERS=192.168.7.1\n")
    body = "\n".join(_rules(fw))
    assert "-d 192.168.7.1/32 -p udp -m udp --dport 53" in body and "8.8.8.8" not in body


@needs_bash
@pytest.mark.parametrize(
    ("local", "message"),
    [
        ("APP_TCP_PORTS=443,25\n", "port 25 stays blocked"),
        ("TUNNEL_PORTS=25\n", "port 25 stays blocked"),
        ("BLOCKED_RANGES=10.0.0.0/8 172.16.0.0/12 192.168.0.0/16\n", "must include 100.64.0.0/10"),
        (
            "BLOCKED_RANGES=10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 127.0.0.0/8\n",
            "169.254.0.0/16",
        ),
        ("PROJECT=x; rm -rf /\n", "PROJECT must be"),
        ("APP_TCP_PORTS=443 -j ACCEPT\n", "APP_TCP_PORTS must be"),
        ("DNS_SERVERS=1.1.1.1 -j ACCEPT\n", "not an IPv4 address"),
        ("EXTRA=1\n", "unknown setting EXTRA"),
        ("echo hi\n", "not a KEY=value line"),
        ("LOG_RATE=lots\n", "LOG_RATE"),
    ],
)
def test_settings_are_checked_and_a_bad_one_changes_nothing(fw, local, message):
    before = _applied(fw)
    fw.local_conf.write_text(local)
    result = fw.run("apply")
    assert result.returncode != 0 and message in result.stderr
    assert fw.chains() == before


@needs_bash
def test_remove_takes_every_rule_out(fw):
    _applied(fw)
    result = fw.run("remove")
    assert result.returncode == 0 and "Re-apply host security" in result.stdout
    chains = fw.chains()
    assert "HOA-EGRESS" not in chains and chains["DOCKER-USER"] == []
    assert "HOA-EGRESS" not in fw.chains("6") and fw.chains("6")["FORWARD"] == []


# --- Review fixes (0.46.0): rules restored before Docker at boot; the drill's copy -----------


@needs_bash
def test_apply_saves_its_rules_and_restore_loads_them_before_docker_starts(fw):
    """EXP-001: after a reboot iptables is empty and Docker restarts the
    containers as it starts; the boot unit loads the saved rules first."""
    applied = _applied(fw)
    saved = fw.tmp / "saved" / "egress-firewall.v4.rules"
    assert saved.exists() and oct(saved.stat().st_mode & 0o777) == "0o600"
    assert saved.read_text().splitlines()[0] == "*filter"
    first_mtime = saved.stat().st_mtime_ns
    assert fw.run("apply", "--quiet").returncode == 0
    assert saved.stat().st_mtime_ns == first_mtime  # unchanged: not rewritten each minute
    # A reboot: no chains at all, Docker not started yet (it would answer nothing).
    for name in ("v4.json", "v6.json"):
        (fw.state / name).write_text(json.dumps({"FORWARD": []}))
    fw.extra_env["FAKE_DOCKER_DOWN"] = "1"
    result = fw.run("restore")
    assert result.returncode == 0, result.stderr
    after = fw.chains()
    assert after["HOA-EGRESS"] == applied["HOA-EGRESS"]
    assert after["DOCKER-USER"] == ["-A DOCKER-USER -j HOA-EGRESS"]
    assert after["FORWARD"] == ["-A FORWARD -j DOCKER-USER"]
    for packet in DROPPED:
        assert _verdict(after, packet) == "DROP", packet
    assert fw.chains("6")["FORWARD"] == ["-A FORWARD -j HOA-EGRESS"]
    # Docker up again, same bridges: apply finds the rules current.
    del fw.extra_env["FAKE_DOCKER_DOWN"]
    assert fw.run("status").stdout.strip() == "active 2"


@needs_bash
def test_restore_without_saved_rules_or_with_a_foreign_file_loads_nothing(fw):
    result = fw.run("restore")
    assert result.returncode == 0 and "No saved" in result.stdout
    assert not (fw.state / "v4.json").exists()
    (fw.tmp / "saved").mkdir()
    (fw.tmp / "saved" / "egress-firewall.v4.rules").write_text(
        "*filter\n:HOA-EGRESS - [0:0]\n-A FORWARD -j ACCEPT\nCOMMIT\n"
    )
    result = fw.run("restore")
    assert result.returncode != 0 and "not a saved rules file" in result.stderr
    assert not (fw.state / "v4.json").exists()


@needs_bash
def test_remove_also_forgets_the_saved_rules(fw):
    _applied(fw)
    fw.run("remove")
    assert not (fw.tmp / "saved" / "egress-firewall.v4.rules").exists()


@needs_bash
def test_the_restore_drills_network_is_filtered_too(fw):
    """EXP-003: the drill's copy (project hoa-restore-drill) gets the same
    rules; only the portal's own edge network has the tunnel port."""
    drill_id = "f6" * 32
    drill_br = "br-" + drill_id[:12]
    fw.nets["hoa-restore-drill_default"] = ["bridge", drill_id, ""]
    chains = _applied(fw)
    assert fw.run("status").stdout.strip() == "active 3"
    assert _verdict(chains, {"in": drill_br, "dst": "192.168.1.20", "port": 443}) == "DROP"
    assert _verdict(chains, {"in": drill_br, "dst": "198.41.192.7", "port": 7844}) == "DROP"
    assert _verdict(chains, {"in": drill_br, "dst": "198.51.99.10", "port": 443}) == "RETURN"
    conf = (POC / "egress-firewall.conf").read_text()
    assert "\nEXTRA_PROJECTS=hoa-restore-drill\n" in conf
    fw.local_conf.write_text("EXTRA_PROJECTS=bad;name\n")
    result = fw.run("apply")
    assert result.returncode != 0 and "EXTRA_PROJECTS" in result.stderr
    drill = (POC / "restore-drill.sh").read_text()
    created = drill.index('"${SCRATCH[@]}" up --no-start postgres seaweedfs')
    refreshed = drill.index("egress_firewall_refresh", created)
    assert refreshed < drill.index('"${SCRATCH[@]}" up -d postgres seaweedfs')
    cleanup = drill[drill.index("cleanup() {") : drill.index("trap cleanup EXIT")]
    assert cleanup.index("down -v") < cleanup.index("egress_firewall_refresh")


def test_the_boot_unit_restores_before_docker_and_start_portal_applies_before_starting():
    boot = _unit("hoa-egress-firewall-boot.service")
    assert "Before=docker.service" in boot and "After=ufw.service" in boot
    assert "ExecStart=/usr/local/sbin/hoa-egress-firewall restore" in boot
    assert "WantedBy=multi-user.target" in boot
    assert "hoa-egress-firewall-boot.service" in _harden().split("systemctl enable hoa-egress")[1]
    start = (POC / "start-portal.sh").read_text()
    before = start.index('egress_firewall_before_start oak-creek-portal "${COMPOSE[@]}"')
    assert start.index("down --remove-orphans") < before
    assert before < start.index('"${COMPOSE[@]}" up -d --remove-orphans --wait')
    lib = (POC / "lib.sh").read_text()
    helper = lib[lib.index("egress_firewall_before_start() {") :]
    helper = helper[: helper.index("\n}\n")]
    assert '[ -x "$EGRESS_FIREWALL_BIN" ] || return 0' in helper
    assert '"$@" up --no-start --remove-orphans' in helper
    assert helper.index("--no-start") < helper.index("egress_firewall_refresh")


@needs_bash
def test_before_start_creates_then_applies_only_when_the_networks_are_new(tmp_path):
    calls = tmp_path / "calls"
    for name, body in (
        (
            "docker",
            f'#!/bin/sh\necho "docker $*" >>{calls}\n'
            'if [ "$1" = network ]; then printf "%s" "$FAKE_IDS"; fi\n',
        ),
        ("compose", f'#!/bin/sh\necho "compose $*" >>{calls}\n'),
        ("hoa-egress-firewall", f'#!/bin/sh\necho "firewall $*" >>{calls}\n'),
    ):
        (tmp_path / name).write_text(body)
        (tmp_path / name).chmod(0o755)
    script = "source infra/poc/lib.sh; egress_firewall_before_start oak-creek-portal compose -f x"
    for ids, expected in (
        ("", ["compose -f x up --no-start --remove-orphans", "firewall apply --quiet"]),
        ("abc123\n", []),
    ):
        calls.write_text("")
        result = subprocess.run(  # noqa: S603  # nosec B603
            [BASH, "-c", script],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{tmp_path}:/usr/bin:/bin",
                "HOA_EGRESS_BIN": str(tmp_path / "hoa-egress-firewall"),
                "FAKE_IDS": ids,
            },
        )
        assert result.returncode == 0, result.stderr
        lines = [x for x in calls.read_text().splitlines() if not x.startswith("docker ")]
        assert lines == expected, ids


# --- harden-host.sh installs and applies it; the other scripts follow ------------------------


def _harden() -> str:
    return (POC / "harden-host.sh").read_text()


def _unit(name: str) -> str:
    text = _harden()
    start = text.index(f"/etc/systemd/system/{name} <<'UNIT'\n") + len(
        f"/etc/systemd/system/{name} <<'UNIT'\n"
    )
    return text[start : text.index("\nUNIT\n", start)]


def test_harden_host_installs_root_copies_and_applies():
    text = _harden()
    src = '"$APP_DIR/infra/poc/egress-firewall'
    assert f'install -m 0755 {src}.sh" /usr/local/sbin/hoa-egress-firewall' in text
    assert f'install -m 0644 {src}.conf" /etc/hoa-portal/egress-firewall.conf' in text
    assert "systemctl enable hoa-egress-firewall.service" in text
    assert "systemctl enable --now hoa-egress-firewall.timer" in text
    install = text.index("/usr/local/sbin/hoa-egress-firewall\n")
    assert install < text.index("/usr/local/sbin/hoa-egress-firewall apply |")
    assert "-w /etc/hoa-portal -p wa -k hoa_firewall" in text  # auditd watches its settings
    # The re-apply server task is harden-host.sh itself.
    tasks = (POC / "host-tasks.sh").read_text()
    assert (
        'harden_host)\n      TASK_MINUTES=25; TASK_CMD=(bash "$APP_DIR/infra/poc/harden-host.sh")'
        in tasks
    )


def test_systemd_unit_runs_after_every_docker_start_and_the_timer_every_minute():
    service = _unit("hoa-egress-firewall.service")
    assert "After=docker.service" in service
    assert "WantedBy=docker.service" in service
    assert "Type=oneshot" in service
    assert "ExecStart=/usr/local/sbin/hoa-egress-firewall apply --quiet" in service
    assert "RemainAfterExit" not in service  # the timer must be able to run it again
    timer = _unit("hoa-egress-firewall.timer")
    assert "OnBootSec=1min" in timer and "OnUnitActiveSec=1min" in timer
    assert "WantedBy=timers.target" in timer


def test_scripts_refresh_after_compose_and_the_agent_and_install_check_it():
    assert "egress_firewall_refresh" in (POC / "start-portal.sh").read_text()
    assert "egress_firewall_refresh" in (POC / "restart-portal.sh").read_text()
    agent = (POC / "portal-agent.sh").read_text()
    assert 'read -r EGRESS_STATE EGRESS_NETS <<<"$(egress_firewall_state)"' in agent
    assert "jsonb_build_object('egress', :'es', 'egress_networks', :'en')" in agent
    install = (POC / "install.sh").read_text()
    checks = install[install.index("run_checks() {") :]
    assert "egress_firewall_state" in checks and "Outbound firewall: on for" in checks
    assert "unit_enabled hoa-egress-firewall.timer" in install  # a re-run applies it


def test_never_applied_by_local_development():
    for name in ("docker-compose.yml", "docker-compose.tunnel.yml", ".env.example", "Dockerfile"):
        path = Path(name)
        if path.exists():
            assert "egress" not in path.read_text().lower(), name
    lib = (POC / "lib.sh").read_text()
    assert '[ -x "$EGRESS_FIREWALL_BIN" ] || return 0' in lib


@needs_bash
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("active 2", "active 2"),
        ("stale 2", "stale 2"),
        ("no-networks 0", "no-networks 0"),
        ("active 2'; DROP TABLE x", "unknown 0"),
        ("", "unknown 0"),
    ],
)
def test_the_agent_reports_only_the_expected_shape(tmp_path, output, expected):
    fake = tmp_path / "hoa-egress-firewall"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"{output}\"\nexit 1\n")
    fake.chmod(0o755)
    result = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", "set -euo pipefail; source infra/poc/lib.sh; egress_firewall_state"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOA_EGRESS_BIN": str(fake)},
    )
    assert result.stdout.strip() == expected
    missing = subprocess.run(  # noqa: S603  # nosec B603
        [BASH, "-c", "source infra/poc/lib.sh; egress_firewall_state; egress_firewall_refresh"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOA_EGRESS_BIN": str(tmp_path / "none")},
    )
    assert missing.stdout.strip() == "absent 0" and missing.returncode == 0


SHELLCHECK = shutil.which("shellcheck")


@pytest.mark.skipif(SHELLCHECK is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    subprocess.run(  # noqa: S603  # nosec B603
        [SHELLCHECK, "-x", str(SCRIPT)], check=True
    )


# --- The "Outbound firewall" health check ----------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("states", "seen_minutes_ago", "expected"),
    [
        ({"egress": "active", "egress_networks": "2"}, 1, True),
        ({"egress": "absent", "egress_networks": "0"}, 1, False),
        ({"egress": "missing", "egress_networks": "2"}, 1, False),
        ({"egress": "stale", "egress_networks": "2"}, 1, False),
        ({"egress": "no-networks", "egress_networks": "0"}, 1, False),
        ({"egress": "<b>odd</b>", "egress_networks": "x"}, 1, False),
        ({"falco": "active"}, 1, None),  # an agent from before D101
        ({"egress": "missing"}, 30, None),  # no recent report
    ],
)
def test_outbound_firewall_check(states, seen_minutes_ago, expected):
    from core import health
    from core.models import UpdateStatus

    UpdateStatus.objects.create(
        pk=1,
        host_security=states,
        agent_seen_at=timezone.now() - datetime.timedelta(minutes=seen_minutes_ago),
    )
    result = health._egress_firewall()
    if expected is None:
        assert result is None
        return
    assert result[0] is expected
    if expected:
        assert "2 portal network" in result[1]
    else:
        assert "Re-apply host security" in result[1] and "<b>" not in result[1]


def test_outbound_firewall_check_is_registered():
    from core import health

    assert ("egress_firewall", "Outbound firewall") in [(k, label) for k, label, _ in health.CHECKS]


def test_system_status_card_lists_the_firewall():
    from core.views import _host_security

    rows = _host_security({"falco": "active", "egress": "active"})
    assert ("Outbound firewall", "active", True) in rows
    rows = _host_security({"falco": "active", "egress": "missing"})
    assert ("Outbound firewall", "missing", False) in rows
