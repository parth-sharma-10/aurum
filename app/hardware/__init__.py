"""The hardware boundary: software routing to physical actuation.

Nothing above this package imports pyserial. An ACK from the board is not
evidence that a servo physically moved - that is established on the bench and
recorded in docs/hardware.md.
"""

from app.hardware.arduino import (
    PROTOCOL,
    ArduinoController,
    Command,
    CommandState,
    build_frame,
    new_command_id,
    parse_response,
)
from app.hardware.link import BoardLink
from app.hardware.transport import FakeTransport, LinkState, SerialTransport, Transport

__all__ = [
    "PROTOCOL",
    "BoardLink",
    "ArduinoController",
    "Command",
    "CommandState",
    "FakeTransport",
    "LinkState",
    "SerialTransport",
    "Transport",
    "build_frame",
    "new_command_id",
    "parse_response",
]
