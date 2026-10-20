"""Tool schemas for the sunshine-moonlight plugin (what the LLM sees)."""

GAME_STREAM_SCHEMA = {
    "name": "game_stream",
    "description": (
        "Set up or diagnose Moonlight game streaming between machines on the same "
        "network or tailnet. The HOST runs Sunshine (captures its desktop/GPU, encodes, "
        "serves); the CLIENT runs Moonlight (pairs, displays the stream). One tool, "
        "action-based. Prefer read-only actions (status, discover, preflight, verify) "
        "before mutating ones (install, start, pair, launch). Remote hosts are reached "
        "over SSH: pass ssh_user/ssh_host for those; leave them out for the local machine. "
        "Pairing is two-sided: pair_pin on the host shows a PIN that the client pairing "
        "step submits; the client-side GUI click cannot be automated on Wayland, so "
        "pair returns instructions for the human step when needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "discover",     # find Sunshine hosts on tailnet/LAN (read-only)
                    "status",       # is sunshine/moonlight installed/running here or on ssh host
                    "preflight",    # probe stream ports 47984/47989/48010/47990 on a host
                    "install",      # install sunshine (host) or moonlight-qt (client)
                    "start",        # start/stop/enable sunshine service on host (state: start|stop|enable)
                    "configure",    # write sunshine.yml keys (encoder, address_family, ...) + restart
                    "pair_pin",     # show the PIN Sunshine is advertising / list paired clients
                    "pair",         # client-side: pair to host (CLI where possible, else instructions)
                    "unpair",       # remove a paired client from the host
                    "verify",       # full health: service, ports, encoder reality (nvenc vs software)
                    "launch",       # start a Moonlight stream to a host from the client
                    "doctor",       # run all read-only checks and print a fix plan
                ],
            },
            "host": {
                "type": "string",
                "description": "Sunshine HOST machine: tailnet name (e.g. onyx-linux), IP, or localhost.",
            },
            "ssh_user": {
                "type": "string",
                "description": "SSH user for the host (e.g. jeffrey). Omit for local actions.",
            },
            "ssh_host": {
                "type": "string",
                "description": "SSH target if different from host (e.g. onyx-linux).",
            },
            "role": {"type": "string", "enum": ["host", "client"], "default": "host",
                     "description": "For install: Sunshine host or Moonlight client (local installation)."},
            "state": {
                "type": "string",
                "enum": ["start", "stop", "enable", "disable", "restart"],
                "description": "For action=start: what to do with the sunshine service.",
            },
            "settings": {
                "type": "object",
                "description": "For action=configure: only encoder, address_family, upnp, hevc_mode, av1_mode; e.g. {\"encoder\": \"nvenc\", \"address_family\": \"both\", \"upnp\": \"disabled\", \"hevc_mode\": 0}.",
            },
            "client_name": {
                "type": "string",
                "description": "For unpair: name of the paired client to remove.",
            },
            "apps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "For launch: app/title to stream (default: desktop).",
            },
            "resolution": {
                "type": "string",
                "description": "For launch: e.g. 1920x1080@60. Optional.",
            },
        },
        "required": ["action"],
    },
}
