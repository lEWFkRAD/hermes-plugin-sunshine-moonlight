<!-- project-home:start -->
![hermes-plugin-sunshine-moonlight project banner](https://raw.githubusercontent.com/lEWFkRAD/hermes-plugin-sunshine-moonlight/46554af075ffbd2919f3b30326ef774a8bd489d2/docs/banner.jpg)

**[Sunshine / Moonlight for Hermes project homepage](https://lewfkrad.github.io/hermes-plugin-sunshine-moonlight/)** · [Documentation](https://github.com/lEWFkRAD/hermes-plugin-sunshine-moonlight#actions)

Set up and diagnose Sunshine-to-Moonlight game streaming from Hermes.

<!-- project-home:end -->

# Sunshine / Moonlight for Hermes Agent

A Hermes plugin for setting up and diagnosing
[Moonlight](https://moonlight-stream.org/) game streaming
([Sunshine](https://app.lizardbyte.dev/Sunshine/) host <-> Moonlight client)
between machines that run Hermes.

It adds one action-based tool, `game_stream`. Read-only actions are safe to
run any time; the tool writes to remote machines only over SSH, and only
when the chosen action says so.

## Actions

| Action | What it does |
|---|---|
| `discover` | Find Sunshine hosts on the tailnet / LAN (read-only) |
| `status` | Is Sunshine/Moonlight installed and running here, or on an SSH host |
| `preflight` | Probe the streaming ports (47984/47989/48010/47990) on a host |
| `install` | Install Sunshine (host) or Moonlight (client) via the detected package manager |
| `start` | Start/stop/restart/enable/disable the Sunshine user service |
| `configure` | Merge keys into `sunshine.yml` (backs the file up first) |
| `pair_pin` | List paired clients from `sunshine_state.json` |
| `pair` | Pair this Moonlight client with a host |
| `unpair` | Look up a paired client and return manual removal instructions |
| `verify` | Check the running encoder, bound ports, and recent fatal errors |
| `launch` | Launch a stream from this client to a host |
| `doctor` | Combined diagnosis with hints (encoder, ports, service state) |

Targets are local by default; pass `ssh_user` + `ssh_host` to drive a
remote machine over SSH (BatchMode, short connect timeout).

## Requirements

- Hermes Agent >= 0.21.3
- Linux: local actions drive the Sunshine **user** systemd service and use
  standard tools (`pgrep`, `ss`). Remote targets are expected to be Linux
  hosts reachable over SSH.
- Sunshine and/or Moonlight themselves are installed by the `install`
  action (or already present); the plugin has no Python dependencies.

## Notes on the security scan

`hermes plugins validate` reports a `caution` for `sudo` use in
`tools.py` — that is the `install` action elevating to install the
`sunshine` / `moonlight-qt` system packages. Nothing else in the plugin
elevates, and no action writes anywhere except Sunshine's own config/state
files (always with a backup first).

- Author: Jeff's laptop Hermes (lewfkrad).

## Input boundaries

SSH usernames and targets are validated before any action; supply both or neither.
Moonlight runs with an argument list and a separate environment, without shell
parsing. Resolution accepts WIDTHxHEIGHT or WIDTHxHEIGHT@HZ. Installation is local.
Configuration accepts only encoder, address_family, upnp, hevc_mode and av1_mode,
with bounded scalar choices. Command hooks, app paths and arbitrary YAML are refused.
Unpair only looks up a name; it does not remove clients or restart services.

Regression checks: python -m unittest discover -s tests -v (no live hosts needed).
