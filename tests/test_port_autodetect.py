"""Choosing the board's serial port when no profile named one.

Selection is on the USB vendor id, not on the port's name. macOS lists
Bluetooth serial profiles as callout nodes beside real USB devices - this
bench sees an incoming-port node, a debug console and a pair of earbuds - and
a name pattern written for `usbmodem` matches those too. None of them carries
a vendor id, because none is on the USB bus. Opening one instead of the board
presents as a healthy link that never answers PING.
"""

from __future__ import annotations

from serial.tools import list_ports

from app.hardware.transport import autodetect_port

ARDUINO_VID = 0x2341
CH340_VID = 0x1A86


class FakePort:
    def __init__(self, device: str, vid: int | None) -> None:
        self.device = device
        self.vid = vid


def attach(monkeypatch, *entries: tuple[str, int | None]) -> None:
    monkeypatch.setattr(list_ports, "comports", lambda: [FakePort(d, v) for d, v in entries])


def test_it_picks_the_only_usb_device(monkeypatch):
    attach(monkeypatch, ("/dev/cu.usbmodem101", ARDUINO_VID))
    port, why = autodetect_port()
    assert port == "/dev/cu.usbmodem101"
    assert "one USB serial device" in why


def test_it_ignores_nodes_with_no_vendor_id(monkeypatch):
    """The real bench listing: three Bluetooth-ish nodes and one board."""
    attach(
        monkeypatch,
        ("/dev/cu.Bluetooth-Incoming-Port", None),
        ("/dev/cu.GalaxyBudsProBA2B", None),
        ("/dev/cu.debug-console", None),
        ("/dev/cu.usbmodem1101", ARDUINO_VID),
    )
    port, _ = autodetect_port()
    assert port == "/dev/cu.usbmodem1101"


def test_it_prefers_the_callout_node_over_the_tty_node(monkeypatch):
    """macOS lists both for one device; tty blocks waiting for carrier detect."""
    attach(
        monkeypatch,
        ("/dev/tty.usbmodem101", ARDUINO_VID),
        ("/dev/cu.usbmodem101", ARDUINO_VID),
    )
    port, _ = autodetect_port()
    assert port == "/dev/cu.usbmodem101"


def test_it_refuses_to_guess_between_two_boards(monkeypatch):
    attach(
        monkeypatch,
        ("/dev/cu.usbmodem101", ARDUINO_VID),
        ("/dev/cu.usbserial-1420", CH340_VID),
    )
    port, why = autodetect_port()
    assert port is None
    assert "refusing to guess" in why


def test_it_says_the_board_is_not_enumerating_when_nothing_is_plugged_in(monkeypatch):
    attach(monkeypatch, ("/dev/cu.Bluetooth-Incoming-Port", None))
    port, why = autodetect_port()
    assert port is None
    assert "not enumerating" in why
