"""Ableton Live integration through the Model Context Protocol."""

__version__ = "1.1.0"

from .transport import AbletonConnection


def get_ableton_connection():
    """Keep the public helper without importing the server before ``python -m``."""
    from .server import get_ableton_connection as connect
    return connect()
