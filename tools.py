"""Handlers for the sunshine-moonlight plugin.

Design rules baked in (each learned from a real onyx/laptop deployment):
- Port binds are ground truth, "is-active: active" is not.
- Sunshine on a desktop session runs as a USER systemd service; needs
  XDG_RUNTIME_DIR (and DISPLAY) in the environment.
- Encoder fallback (nvenc -> libx264) is silent-ish: verify reads the log.
- Pairing is two-sided (PIN shown by host, entered by client); the client
  GUI click can't be automated on Wayland, so we return instructions.
- Never start sunshine twice: check pgrep + port binds before starting.
"""

import base64
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

STREAM_PORTS = [47984, 47989, 48010]
WEBUI_PORT = 47990
SUNSHINE_CONFIG = os.path.expanduser("~/.config/sunshine/sunshine.yml")
SUNSHINE_STATE = os.path.expanduser("~/.config/sunshine/sunshine_state.json")
SUNSHINE_LOG = os.path.expanduser("~/.config/sunshine/sunshine.log")


# Only scalar display/encoder options may be changed through this tool.
CONFIG_CHOICES = {
    "encoder": {"nvenc", "vaapi", "software", "amdvce", "quicksync", "videotoolbox"},
    "address_family": {"ipv4", "both"},
    "upnp": {"enabled", "disabled"},
    "hevc_mode": {"0", "1", "2", "3"},
    "av1_mode": {"0", "1", "2", "3"},
}


def _validate_host(value):
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise ValueError("host must be a hostname or IP address")
    try:
        ipaddress.ip_address(value)
        if "%" in value:
            raise ValueError("scoped addresses are not supported")
        return
    except ValueError:
        pass
    if len(value) > 253 or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value
    ):
        raise ValueError("host must be a hostname or IP address")


def _validate_ssh(user, host):
    if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_.-]*", user):
        raise ValueError("invalid ssh_user")
    _validate_host(host)


def _validate_settings(settings):
    if not isinstance(settings, dict) or not settings:
        raise ValueError("configure needs settings={...}")
    for key, value in settings.items():
        if key not in CONFIG_CHOICES or type(value) not in (str, int):
            raise ValueError("unsupported configure key or value: %s" % key)
        if str(value) not in CONFIG_CHOICES[key]:
            raise ValueError("unsupported value for %s" % key)


def _validate_args(args):
    if ("ssh_user" in args) != ("ssh_host" in args):
        raise ValueError("ssh_user and ssh_host must be supplied together")
    if "ssh_user" in args:
        _validate_ssh(args["ssh_user"], args["ssh_host"])
    if "host" in args:
        _validate_host(args["host"])
    if "resolution" in args and (
        not isinstance(args["resolution"], str)
        or not re.fullmatch(r"[0-9]+x[0-9]+(@[0-9]+)?", args["resolution"])
    ):
        raise ValueError("resolution must be WIDTHxHEIGHT or WIDTHxHEIGHT@HZ")
    if "role" in args and args["role"] not in ("host", "client"):
        raise ValueError("role must be host or client")
    if "client_name" in args and (
        not isinstance(args["client_name"], str) or
        any(ord(c) < 32 or ord(c) == 127 for c in args["client_name"])
    ):
        raise ValueError("invalid client_name")
    if "apps" in args and (not isinstance(args["apps"], list) or
                           any(not isinstance(a, str) or a.startswith("-") or
                               any(ord(c) < 32 or ord(c) == 127 for c in a)
                               for a in args["apps"])):
        raise ValueError("apps must contain application names, not options")
    if args.get("action") == "configure":
        _validate_settings(args.get("settings"))


# ---------------------------------------------------------------- plumbing --

def _run(cmd, timeout=30):
    """Run a local command; return (rc, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after %ss" % timeout
    except Exception as e:  # noqa: BLE001
        return 127, "EXEC ERROR: %s" % e


def _ssh(ssh_user, ssh_host, remote_cmd, timeout=30):
    """Run a command on a remote host over SSH (BatchMode; no password prompts)."""
    _validate_ssh(ssh_user, ssh_host)
    target = "%s@%s" % (ssh_user, ssh_host)
    # Pass the command as ONE argv element to ssh; ssh concatenates argv and the
    # remote shell re-parses it, so shell operators (||, ;, |) survive.
    return _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 "--", target, remote_cmd], timeout=timeout)


def _host_run(args, remote_cmd, local_cmd, timeout=30):
    """Dispatch: remote over SSH when ssh_user+ssh_host given, else local."""
    if args.get("ssh_user") and args.get("ssh_host"):
        return _ssh(args["ssh_user"], args["ssh_host"], remote_cmd, timeout)
    return _run(local_cmd, timeout=timeout)


def _is_local(args):
    return not (args.get("ssh_user") and args.get("ssh_host"))


def _user_service_env():
    """Env prefix for driving a user systemd service over ssh (no login session)."""
    uid = _run("id -u")[1].strip() if _is_local_implicit() else None
    return "XDG_RUNTIME_DIR=/run/user/%s " % (uid or os.environ.get("USER_UID", "1000"))


def _is_local_implicit():
    return True  # only called inside local context


def _ok(payload):
    return json.dumps({"success": True, **payload})


def _fail(payload):
    return json.dumps({"success": False, **payload})


# ------------------------------------------------------------- primitives --

def _probe_ports(host, timeout=3):
    """TCP-connect probe; returns {port: 'OPEN'|'closed'}."""
    results = {}
    for port in STREAM_PORTS + [WEBUI_PORT]:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            results[port] = "OPEN"
        except OSError:
            results[port] = "closed"
    return results


def _sunshine_running():
    """Ground truth = process + port binds, not systemctl's opinion."""
    rc, out = _run("pgrep -a sunshine; echo ---; ss -tln 2>/dev/null | grep -oE ':(47984|47989|48010|47990)' | sort -u")
    return out


def _encoder_reality():
    """Last 'Found ... encoder' line from the sunshine log."""
    rc, out = _run("grep -aE 'Found .*encoder' %s 2>/dev/null | tail -1" % shlex.quote(SUNSHINE_LOG))
    return out.strip() or "no encoder line in log"


def _tailscale_hosts():
    rc, out = _run("tailscale status --peers=true 2>/dev/null")
    if rc != 0:
        return []
    hosts = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and re.match(r"^100\.\d+\.\d+\.\d+$", parts[0]):
            hosts.append({"ip": parts[0], "name": parts[1]})
    return hosts


# ---------------------------------------------------------------- actions --

def act_discover(args):
    """Find candidate Sunshine hosts: tailnet peers with stream ports open."""
    hosts = _tailscale_hosts()
    found, unreachable = [], []
    for h in hosts:
        ports = _probe_ports(h["ip"], timeout=2)
        if ports.get(47984) == "OPEN":
            found.append({**h, "ports": ports})
        else:
            unreachable.append(h["name"])
    return _ok({
        "action": "discover",
        "sunshine_hosts": found,
        "peers_without_sunshine": unreachable,
        "hint": "found entries have the Sunshine web port open; pair with action=pair.",
    })


def act_status(args):
    """Installed? running? where?"""
    rc, out = _host_run(
        args,
        "command -v sunshine || echo NO-SUNSHINE; command -v moonlight || echo NO-MOONLIGHT; "
        "echo ---; pgrep -a sunshine || echo NO-PROCESS; "
        "echo ---; ss -tln 2>/dev/null | grep -oE ':(47984|47989|48010|47990)' | sort -u || echo NO-PORTS",
        "command -v sunshine || echo NO-SUNSHINE; command -v moonlight || echo NO-MOONLIGHT; "
        "echo ---; pgrep -a sunshine || echo NO-PROCESS; "
        "echo ---; ss -tln 2>/dev/null | grep -oE ':(47984|47989|48010|47990)' | sort -u || echo NO-PORTS",
    )
    return _ok({"action": "status", "target": args.get("ssh_host") or args.get("host") or "local",
                "report": out.strip()})


def act_preflight(args):
    """From HERE, can we reach the host's stream ports?"""
    host = args.get("host")
    if not host:
        return _fail({"error": "preflight needs host=<sunshine host name or IP>"})
    ports = _probe_ports(host)
    open_count = sum(1 for v in ports.values() if v == "OPEN")
    out = {"action": "preflight", "host": host, "ports": ports,
           "stream_ready": all(ports.get(p) == "OPEN" for p in STREAM_PORTS)}
    if open_count == 0:
        out["hint"] = ("All closed. Check: sunshine running on host? tailscale route fresh? "
                       "A just-rebooted peer can show closed for minutes while routes "
                       "re-establish — re-test before debugging sunshine.")
    return _ok(out)


def act_install(args):
    """Install sunshine (host) or moonlight (client). Package manager detected."""
    role = args.get("role", "host")
    rc, osr = _run("grep -E '^(ID|ID_LIKE)=' /etc/os-release 2>/dev/null | tr '\\n' ' '")
    pm = None
    for cand, cmd in (("apt", "sudo apt-get install -y"), ("dnf", "sudo dnf install -y"),
                      ("pacman", "sudo pacman -S --noconfirm"), ("zypper", "sudo zypper install -y")):
        if cand in osr or shutil.which(cand):
            pm = cmd
            break
    if not pm:
        return _fail({"error": "no known package manager found", "os_release": osr.strip()})
    pkg = "sunshine" if role == "host" else "moonlight-qt"
    rc, out = _run("%s %s" % (pm, pkg), timeout=300)
    return _ok({"action": "install", "package": pkg, "rc": rc, "output_tail": out.strip()[-800:]})


def act_start(args):
    """start|stop|restart|enable|disable the sunshine USER service."""
    state = args.get("state", "start")
    if state not in ("start", "stop", "restart", "enable", "disable"):
        return _fail({"error": "state must be start|stop|restart|enable|disable"})

    # Guard: never start twice — port bind failure looks like success in the log tail.
    # (restart itself stops the old instance first, so it is exempt.)
    if state == "start":
        rc, truth = _host_run(args,
                              "pgrep -a sunshine || echo NO-PROCESS; ss -tln 2>/dev/null | grep -cE ':(47984|47989|48010)' || true",
                              "pgrep -a sunshine || echo NO-PROCESS; ss -tln 2>/dev/null | grep -cE ':(47984|47989|48010)' || true")
        if "NO-PROCESS" not in truth:
            return _ok({"action": "start", "skipped": True,
                        "reason": "a sunshine process is already running — starting twice "
                                  "kills the new one with 'Couldn't bind RTSP server'. "
                                  "Use state=restart if you meant to cycle it.",
                        "processes": truth.strip()})

    # User service needs XDG_RUNTIME_DIR; discover the uid on the target.
    uid_cmd = "id -u"
    rc, uid = _host_run(args, uid_cmd, uid_cmd)
    uid = (uid or "").strip() or "1000"
    if not re.fullmatch(r"[0-9]+", uid):
        return _fail({"error": "could not determine target uid"})
    env = "XDG_RUNTIME_DIR=/run/user/%s DISPLAY=${DISPLAY:-:0}" % uid
    svc = "systemctl --user %s sunshine" % state
    rc, out = _host_run(args, "%s %s" % (env, svc), svc, timeout=30)

    # Verify by ground truth, not by rc.
    rc2, truth = _host_run(args,
                           "pgrep -a sunshine || echo NO-PROCESS; ss -tln 2>/dev/null | grep -oE ':(47984|47989|48010)' | sort -u || echo NO-PORTS",
                           "pgrep -a sunshine || echo NO-PROCESS; ss -tln 2>/dev/null | grep -oE ':(47984|47989|48010)' | sort -u || echo NO-PORTS")
    return _ok({"action": "start", "state": state, "rc": rc, "output": out.strip()[-500:],
                "ground_truth": truth.strip(),
                "note": "port binds are the truth; if ports missing after start, read the log tail."})


def act_configure(args):
    """Merge keys into sunshine.yml (YAML), back it up first."""
    settings = args.get("settings") or {}
    _validate_settings(settings)
    if not settings:
        return _fail({"error": "configure needs settings={...}"})
    rc, existing = _host_run(args, 'cat "$HOME/.config/sunshine/sunshine.yml" 2>/dev/null || true',
                             "cat %s 2>/dev/null || true" % shlex.quote(SUNSHINE_CONFIG))
    lines = {}
    for line in (existing or "").splitlines():
        m = re.match(r"^([a-zA-Z0-9_]+):\s*(.*)$", line.strip())
        if m:
            lines[m.group(1)] = m.group(2)
    for k, v in settings.items():
        lines[k] = str(v).lower() if isinstance(v, bool) else str(v)
    body = "\n".join("%s: %s" % (k, v) for k, v in lines.items()) + "\n"
    b64 = __import__("base64").b64encode(body.encode()).decode()
    write = ("mkdir -p ~/.config/sunshine && cp -n %s %s.bak-preplugin 2>/dev/null; "
             "printf %%s '%s' | base64 -d > %s && echo WROTE"
             % (shlex.quote(SUNSHINE_CONFIG), shlex.quote(SUNSHINE_CONFIG), b64, shlex.quote(SUNSHINE_CONFIG)))
    remote_write = write.replace(shlex.quote(SUNSHINE_CONFIG), '"$HOME/.config/sunshine/sunshine.yml"')
    rc, out = _host_run(args, remote_write, write)
    return _ok({"action": "configure", "written": settings, "output": out.strip(),
                "backup": SUNSHINE_CONFIG + ".bak-preplugin",
                "note": "restart the service (action=start state=restart) to apply."})


def act_pair_pin(args):
    """List paired clients from sunshine_state.json (PIN entry is interactive on host)."""
    rc, out = _host_run(args, 'cat "$HOME/.config/sunshine/sunshine_state.json" 2>/dev/null || echo NO-STATE',
                        "cat %s 2>/dev/null || echo NO-STATE" % shlex.quote(SUNSHINE_STATE))
    clients = []
    try:
        state = json.loads(out)
        clients = [c.get("name", "?") for c in state.get("clients", {}).values()] \
            if isinstance(state.get("clients"), dict) else [c.get("name", "?") for c in state.get("clients", [])]
    except (json.JSONDecodeError, AttributeError):
        pass
    return _ok({
        "action": "pair_pin", "paired_clients": clients,
        "how_to_pair": ("Start Moonlight on the client, add host by name/IP, it shows a PIN; "
                        "enter that PIN in the Sunshine web UI on the host "
                        "(http://<host>:47990, LAN/pc origins only) or via the host's own "
                        "Hermes. Verify afterwards: this action should list the new client."),
    })


def act_pair(args):
    """Client-side pairing reality + verification hook."""
    host = args.get("host")
    rc, ml = _run("command -v moonlight || echo NO-MOONLIGHT")
    instructions = [
        "1. On the client: open Moonlight (GUI) and 'add host' -> %s." % (host or "<host>"),
        "2. Moonlight shows a 4-digit PIN.",
        "3. On the HOST: open its Sunshine web UI at http://<host-ip>:47990 (from the host "
        "itself or LAN), PIN tab, enter the code. (The web UI refuses remote origins by "
        "default — do it on the host.)",
        "4. Verify with action=pair_pin — the client should now be listed.",
    ]
    return _ok({"action": "pair", "moonlight_cli": ml.strip(),
                "automatable": False,
                "why": "the client pairing step is a GUI click (Wayland-safe automation "
                       "needs ydotool); the PIN entry is a host-side web form.",
                "instructions": instructions})


def act_unpair(args):
    name = args.get("client_name")
    if not name:
        return _fail({"error": "unpair needs client_name=<paired client>"})
    encoded = base64.b64encode(name.encode("utf-8")).decode("ascii")
    script = (
        "import base64,json,os,sys\n"
        "name=base64.b64decode(sys.argv[1]).decode('utf-8')\n"
        "p=os.path.expanduser('~/.config/sunshine/sunshine_state.json')\n"
        "with open(p, encoding='utf-8') as f: d=json.load(f)\n"
        "cl=d.get('clients',{})\n"
        "hit=[k for k,v in (cl.items() if isinstance(cl,dict) else enumerate(cl)) "
        "if (v.get('name') if isinstance(v,dict) else v)==name]\n"
        "print('NOT-FOUND' if not hit else 'FOUND:'+','.join(map(str,hit)))"
    )
    command = "python3 -c %s %s" % (shlex.quote(script), shlex.quote(encoded))
    rc, out = _host_run(args, command, command)
    return _ok({"action": "unpair", "probe": out.strip(),
                "note": "if FOUND, remove via the Sunshine web UI Clients tab, or ask and "
                        "I'll edit sunshine_state.json with a backup + service restart."})


def act_verify(args):
    """Full host health: process, ports, encoder reality, log errors."""
    rc, out = _host_run(
        args,
        "pgrep -a sunshine || echo NO-PROCESS; "
        "echo ---PORTS; ss -tln 2>/dev/null | grep -oE ':(47984|47989|48010|47990)' | sort -u || echo NO-PORTS; "
        "echo ---ENCODER; grep -aE 'Found .*encoder' ~/.config/sunshine/sunshine.log 2>/dev/null | tail -1 || echo NO-LOG; "
        "echo ---ERRORS; grep -aE 'Fatal|failed|CUDA' ~/.config/sunshine/sunshine.log 2>/dev/null | tail -3 || true",
        "pgrep -a sunshine || echo NO-PROCESS; "
        "echo ---PORTS; ss -tln 2>/dev/null | grep -oE ':(47984|47989|48010|47990)' | sort -u || echo NO-PORTS; "
        "echo ---ENCODER; grep -aE 'Found .*encoder' ~/.config/sunshine/sunshine.log 2>/dev/null | tail -1 || echo NO-LOG; "
        "echo ---ERRORS; grep -aE 'Fatal|failed|CUDA' ~/.config/sunshine/sunshine.log 2>/dev/null | tail -3 || true",
    )
    report = out.strip()
    warnings = []
    if "libx264" in report or "software" in report:
        warnings.append("encoder is SOFTWARE (libx264) — GPU encode failed; check VRAM "
                        "contention (an inference server's static reserve starves NVENC's "
                        "CUDA context) before blaming sunshine.")
    if "NO-PORTS" in report:
        warnings.append("no stream ports bound — sunshine is not actually serving, "
                        "regardless of systemctl.")
    return _ok({"action": "verify", "report": report, "warnings": warnings})


def act_launch(args):
    """Start a Moonlight stream from this client to host (GUI app; needs session env)."""
    host = args.get("host")
    if not host:
        return _fail({"error": "launch needs host=<sunshine host>"})
    ports = _probe_ports(host)
    if not all(ports.get(p) == "OPEN" for p in STREAM_PORTS):
        return _fail({"error": "host ports not open", "ports": ports,
                      "hint": "run action=preflight / check tailscale freshness first."})
    rc, moonlight = _run("command -v moonlight || echo NO-MOONLIGHT")
    if "NO-MOONLIGHT" in moonlight:
        return _fail({"error": "moonlight not installed on this client",
                      "hint": "action=install role=client"})
    # Harvest the live desktop session env (agent terminals lack it).
    rc, pids = _run("pgrep -u $(id -u) -x Hyprland | head -1")
    session_env = {}
    pid = pids.strip()
    if re.fullmatch(r"[0-9]+", pid):
        try:
            envraw = Path("/proc") / pid / "environ"
            allowed = {"WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "HYPRLAND_INSTANCE_SIGNATURE",
                       "DISPLAY", "DBUS_SESSION_BUS_ADDRESS"}
            for entry in envraw.read_bytes().split(b"\0"):
                key, sep, value = entry.partition(b"=")
                key = os.fsdecode(key)
                if sep and key in allowed:
                    session_env[key] = os.fsdecode(value)
        except OSError:
            pass
    res = args.get("resolution")
    app = (args.get("apps") or [""])[0]
    cmd = ["moonlight", "stream", *(["-res", res] if res else []),
           host, *([app] if app else [])]
    p = subprocess.Popen(cmd, env={**os.environ, **session_env},
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         start_new_session=True)
    time.sleep(4)
    alive = p.poll() is None
    err = ""
    if not alive:
        try:
            err = (p.stderr.read() or b"").decode(errors="replace")[:400]
        except Exception:
            pass
    return _ok({"action": "launch", "pid": p.pid, "host": host, "alive": alive,
                "stderr": err,
                "note": alive and ("Moonlight takes 20-35s to map its window (SDL+QML init) — "
                                   "don't kill/relaunch after a few seconds of nothing. If the "
                                   "host is not paired yet the stream fails fast: pair first.")
                or "exited immediately — see stderr (unpaired host? wrong app name?)"})


def act_doctor(args):
    """All read-only checks + a fix plan, host and client in one shot."""
    host = args.get("host") or "local"
    steps = {}
    steps["ports_from_here"] = _probe_ports(host) if host != "local" else _probe_ports("127.0.0.1")
    rc, out = _host_run(args,
                        "pgrep -a sunshine || echo NO-PROCESS; "
                        "grep -aE 'Found .*encoder' ~/.config/sunshine/sunshine.log 2>/dev/null | tail -1 || echo NO-ENCODER-LINE; "
                        "cat ~/.config/sunshine/sunshine_state.json 2>/dev/null | grep -o '\"name\"' | wc -l || echo 0",
                        "pgrep -a sunshine || echo NO-PROCESS; "
                        "grep -aE 'Found .*encoder' ~/.config/sunshine/sunshine.log 2>/dev/null | tail -1 || echo NO-ENCODER-LINE; "
                        "cat ~/.config/sunshine/sunshine_state.json 2>/dev/null | grep -o '\"name\"' | wc -l || echo 0")
    steps["host_report"] = out.strip()
    plan = []
    if "NO-PROCESS" in out:
        plan.append("sunshine not running on host -> action=start state=start")
    if "NO-ENCODER-LINE" in out:
        plan.append("no encoder line -> sunshine never started or log moved; start it, then action=verify")
    if any(v == "closed" for v in steps["ports_from_here"].values()):
        plan.append("some ports closed from here -> check tailscale route freshness, then firewall")
    steps["fix_plan"] = plan or ["all checks green"]
    return _ok({"action": "doctor", "host": host, **steps})


ACTIONS = {
    "discover": act_discover,
    "status": act_status,
    "preflight": act_preflight,
    "install": act_install,
    "start": act_start,
    "configure": act_configure,
    "pair_pin": act_pair_pin,
    "pair": act_pair,
    "unpair": act_unpair,
    "verify": act_verify,
    "launch": act_launch,
    "doctor": act_doctor,
}


def game_stream_handler(params, **kwargs):
    action = params.get("action", "")
    fn = ACTIONS.get(action)
    if fn is None:
        return _fail({"error": "unknown action %r" % action, "known": sorted(ACTIONS)})
    try:
        _validate_args(params)
        return fn(params)
    except Exception as e:  # noqa: BLE001
        return _fail({"error": "%s: %s" % (type(e).__name__, e), "action": action})
