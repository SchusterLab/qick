"""QICK board server (lab fork): replaces the Pyro server.

Board side: `python -m qick.boardserver ...` (see slab/notes/board-server-install.md).
PC side: `from qick.boardserver.client import BoardClient`.
Protocol: slab/notes/board-server-protocol.md.

Nothing here has run on a board yet; everything is tested against the fake backend.
"""
from .protocol import PROTOCOL_VERSION
