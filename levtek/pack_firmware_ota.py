#!/usr/bin/env python3
"""Pack a built firmware .bin into the layout the bootloader's new-app area expects.

This is a dependency-free equivalent of VESC Tool's ``--packFirmware`` (vesc_tool
main.cpp:807-868). Cloud Build packs with VESC Tool itself, since that is the
reference implementation of the format; this module is what independently verifies
that output, what recovers a raw image with --unpack, and what developers use
locally without needing the Qt binary and its shared libraries.

Verified byte-identical to ``vesc_tool 7.00 --packFirmware`` on a real
levtek_1_0_3 build (same sha256).

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
    ./pack_firmware_ota.py --unpack packed.bin recovered.bin

Unpacking exists because CI publishes the packed image: recovering a bricked
board over SWD needs the raw one back.

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


def unpack(packed: bytes) -> bytes:
    """Recover the original image from a packed one.

    The inverse of pack(). CI publishes the packed image, so this is how you get
    back to something an ST-Link can flash when a board needs recovering.
    """
    if len(packed) <= HEADER_LEN:
        raise PackError(f"not a packed image: only {len(packed)} bytes")

    size_field, crc = struct.unpack(">IH", packed[:HEADER_LEN])
    payload = packed[HEADER_LEN:]

    if (size_field & 0x00FFFFFF) != len(payload):
        raise PackError(
            f"not a packed image: header declares {size_field & 0x00FFFFFF} payload bytes, found {len(payload)}"
        )
    if crc != crc_hqx(payload, 0):
        raise PackError("not a packed image: header CRC does not match the payload")

    if (size_field >> 24) != COMPRESSED_MARKER:
        return payload

    try:
        import heatshrink2
    except ImportError as e:  # pragma: no cover - environment problem, not logic
        raise PackError(f"{e}. Install it with: pip install heatshrink2") from e

    return heatshrink2.decompress(payload, window_sz2=WINDOW_SZ2, lookahead_sz2=LOOKAHEAD_SZ2)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--unpack"]
    unpacking = "--unpack" in argv[1:]

    if len(args) != 2:
        sys.stderr.write(
            f"usage: {argv[0]} <firmware.bin> <packed.bin>\n"
            f"       {argv[0]} --unpack <packed.bin> <firmware.bin>\n"
        )
        return 2

    with open(args[0], "rb") as f:
        data = f.read()

    if not data:
        sys.stderr.write(f"{args[0]} is empty\n")
        return 1

    try:
        result = unpack(data) if unpacking else pack(data)
    except PackError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1

    with open(args[1], "wb") as f:
        f.write(result)

    if unpacking:
        sys.stderr.write(f"unpacked {len(data)} -> {len(result)} bytes\n")
    else:
        sys.stderr.write(f"{describe(data, result)}\n")
    sys.stderr.write(f"wrote {args[1]} ({len(result)} bytes)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
