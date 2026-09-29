#!/usr/bin/env python3
"""Pack a built firmware .bin into the layout the bootloader's new-app area expects.

This is the headless equivalent of VESC Tool's ``--packFirmware`` (vesc_tool
main.cpp:807-868), so CI can produce an OTA-ready image without pulling in Qt.
levkart-ota then uploads the result over CAN essentially verbatim.

Layout, matching what the bootloader reads from the start of the new-app area
(see libcanard/canard_driver.c:1041-1080, which builds the same header):

    [uint32be size]   (0xCC << 24) | len(payload) when heatshrink-compressed,
                      else len(payload)
    [uint16be crc16]  CRC-16/XMODEM over the payload exactly as written
    [payload]

Why compression is not optional here: the linker keeps .crcinfo at the absolute
address 0x0807FFF0 (ld_eeprom_emu.ld:30,156-161), so objcopy pads every image out
to 524280 bytes -- larger than the 393216-byte staging area (flash_helper.c:42-45).
VESC Tool handles this by heatshrink-compressing anything over the threshold and
flagging it with 0xCC in the top size byte; the bootloader decompresses on the way
in. That path is not gated on any capability check, so it is the normal case
rather than a fallback.

Usage:
    ./pack_firmware_ota.py ../build/levtek_1_0_3/levtek_1_0_3.bin packed.bin

Requires: pip install heatshrink2
"""

import struct
import sys
from binascii import crc_hqx

# VESC Tool's threshold: the 393216-byte new-app staging area minus 8
# (vescinterface.cpp:1561). Images at or below this are stored uncompressed.
STAGING_MAX = 393216 - 8

# These MUST match the decoder compiled into the bootloader
# (vesc_tool heatshrink/heatshrink_config.h:15-17). Changing either one produces
# an image the bootloader cannot decompress, which bricks the board.
WINDOW_SZ2 = 13
LOOKAHEAD_SZ2 = 5

# Top byte of the size field, marking the payload as heatshrink-compressed.
COMPRESSED_MARKER = 0xCC

HEADER_LEN = 6


class PackError(Exception):
    """Raised when an image cannot be packed into something the bootloader accepts."""


def pack(raw: bytes) -> bytes:
    """Return ``raw`` packed as a 6-byte header followed by the payload."""
    if len(raw) > STAGING_MAX:
        payload = _compress(raw)
        size_field = (COMPRESSED_MARKER << 24) | len(payload)
    else:
        payload = raw
        size_field = len(raw)

    return struct.pack(">IH", size_field, crc_hqx(payload, 0)) + payload


def _compress(raw: bytes) -> bytes:
    """Heatshrink-compress ``raw``, verifying it round-trips and still fits."""
    try:
        import heatshrink2
    except ImportError as e:  # pragma: no cover - environment problem, not logic
        raise PackError(
            f"{e}. This image is {len(raw)} bytes, over the {STAGING_MAX}-byte staging "
            "area, so it must be compressed. Install it with: pip install heatshrink2"
        ) from e

    payload = heatshrink2.compress(raw, window_sz2=WINDOW_SZ2, lookahead_sz2=LOOKAHEAD_SZ2)

    if len(payload) > STAGING_MAX:
        raise PackError(
            f"firmware is too large for the bootloader even after compression: "
            f"{len(payload)} > {STAGING_MAX} bytes. The firmware itself has to shrink."
        )

    # Round-trip here rather than discovering a codec mismatch on the vehicle.
    # This only proves the encoder and decoder agree in Python; that the deployed
    # bootloader agrees is established by flashing a packed image with VESC Tool.
    if heatshrink2.decompress(payload, window_sz2=WINDOW_SZ2, lookahead_sz2=LOOKAHEAD_SZ2) != raw:
        raise PackError("heatshrink round-trip mismatch -- refusing to emit this image")

    return payload


def describe(raw: bytes, packed: bytes) -> str:
    """One-line human summary of what packing did."""
    payload_len = len(packed) - HEADER_LEN
    size_field = struct.unpack(">I", packed[:4])[0]
    if (size_field >> 24) == COMPRESSED_MARKER:
        pct = 100.0 * payload_len / len(raw)
        headroom = STAGING_MAX - payload_len
        return (
            f"compressed {len(raw)} -> {payload_len} bytes ({pct:.1f}%), "
            f"{headroom} bytes of headroom under the {STAGING_MAX}-byte limit"
        )
    return f"stored uncompressed, {payload_len} bytes fits the {STAGING_MAX}-byte staging area"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write(f"usage: {argv[0]} <firmware.bin> <packed.bin>\n")
        return 2

    with open(argv[1], "rb") as f:
        raw = f.read()

    if not raw:
        sys.stderr.write(f"{argv[1]} is empty\n")
        return 1

    try:
        packed = pack(raw)
    except PackError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    with open(argv[2], "wb") as f:
        f.write(packed)

    sys.stderr.write(f"{describe(raw, packed)}\nwrote {argv[2]} ({len(packed)} bytes)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
