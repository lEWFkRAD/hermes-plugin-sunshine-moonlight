"""sunshine-moonlight — Hermes plugin entry point.

Adds one tool: game_stream (action-based setup/diagnosis of Moonlight
game streaming between machines that run Hermes).
"""

from .schemas import GAME_STREAM_SCHEMA
from .tools import game_stream_handler


def register(ctx):
    ctx.register_tool(
        name="game_stream",
        toolset="game_stream",
        schema=GAME_STREAM_SCHEMA,
        handler=game_stream_handler,
        emoji="🌙",
    )
