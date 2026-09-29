#!/usr/bin/env python3
"""Tests for pack_firmware_ota.py.

Run standalone, optionally against a real build:

    ./test_pack_firmware_ota.py
    ./test_pack_firmware_ota.py ../build/levtek_1_0_3/levtek_1_0_3.bin

or under pytest, if you have it:

    pytest test_pack_firmware_ota.py

By default the round-trip is checked with heatshrink2 on both sides, which proves
the format but not that our codec parameters match the bootloader's. To cross-check
against the very decoder VESC Tool ships -- the same upstream library the bootloader
is built from -- compile it and point HS_DECODE at the result:

    cat > /tmp/hs_decode.c <<'EOF'
    #include <stdio.h>
    #include "heatshrink_decoder.h"
    int main(void) {
        static heatshrink_decoder hsd; heatshrink_decoder_reset(&hsd);
        static uint8_t in[1 << 20]; uint8_t out[4096];
        size_t in_len = fread(in, 1, sizeof(in), stdin), sunk = 0, count = 0;
        while (sunk < in_len) {
            if (heatshrink_decoder_sink(&hsd, &in[sunk], in_len - sunk, &count) < 0) return 2;
            sunk += count;
            HSD_poll_res p;
            do { p = heatshrink_decoder_poll(&hsd, out, sizeof(out), &count);
                 if (p < 0) return 3; fwrite(out, 1, count, stdout); } while (p == HSDR_POLL_MORE);
        }
        HSD_finish_res f;
        do { f = heatshrink_decoder_finish(&hsd);
             if (f < 0) return 4;
             if (f == HSDR_FINISH_MORE) {
                 if (heatshrink_decoder_poll(&hsd, out, sizeof(out), &count) < 0) return 5;
                 fwrite(out, 1, count, stdout); } } while (f == HSDR_FINISH_MORE);
        return 0;
    }
    EOF
    gcc -O2 -I <vesc_tool>/heatshrink -o /tmp/hs_decode /tmp/hs_decode.c \
        <vesc_tool>/heatshrink/heatshrink_decoder.c
    HS_DECODE=/tmp/hs_decode ./test_pack_firmware_ota.py

The C decoder's static config already pins WINDOW_BITS 13 / LOOKAHEAD_BITS 5
(heatshrink_config.h:15-17), so a parameter mismatch shows up as a decode failure.
"""

import os
import random
import struct
import subprocess
import sys
from binascii import crc_hqx

from pack_firmware_ota import (
    COMPRESSED_MARKER,
    HEADER_LEN,
    LOOKAHEAD_SZ2,
    STAGING_MAX,
    PackError,
    WINDOW_SZ2,
    pack,
)

HS_DECODE = os.environ.get("HS_DECODE")


def synthetic_firmware(size: int) -> bytes:
    """Something shaped like a real image: dense code, the EEPROM hole, an 0xFF tail.

    The code half is random, which heatshrink cannot compress at all, so this is a
    deliberately pessimistic stand-in for real firmware.
    """
    random.seed(1234)
    code = bytes(random.getrandbits(8) for _ in range(size // 2))
    gap = b"\x00" * min(32 * 1024, size - len(code))
    return code + gap + b"\xff" * (size - len(code) - len(gap))


def unpack(packed: bytes) -> tuple[int, bool, bytes]:
    """Split a packed image into (declared length, compressed?, payload)."""
    size_field, crc = struct.unpack(">IH", packed[:HEADER_LEN])
    payload = packed[HEADER_LEN:]
    assert crc == crc_hqx(payload, 0), "header CRC does not match the payload"
    return size_field & 0x00FFFFFF, (size_field >> 24) == COMPRESSED_MARKER, payload


def decode_with_c(payload: bytes) -> bytes:
    r = subprocess.run([HS_DECODE], input=payload, capture_output=True)
    assert r.returncode == 0, f"C decoder failed: rc={r.returncode} {r.stderr!r}"
    return r.stdout


def check_image(raw: bytes) -> tuple[int, bool]:
    """Pack ``raw``, validate the result, and return (payload length, compressed?)."""
    packed = pack(raw)
    declared, compressed, payload = unpack(packed)

    assert declared == len(payload), f"size field {declared} != payload {len(payload)}"
    assert len(payload) <= STAGING_MAX, f"payload {len(payload)} exceeds the staging area"

    if compressed:
        import heatshrink2

        assert heatshrink2.decompress(payload, window_sz2=WINDOW_SZ2, lookahead_sz2=LOOKAHEAD_SZ2) == raw
        if HS_DECODE:
            assert decode_with_c(payload) == raw, "vesc_tool's C decoder did not reproduce the input"
    else:
        assert payload == raw, "uncompressed payload was modified"

    return len(payload), compressed


# --- tests ----------------------------------------------------------------


def test_small_image_is_stored_uncompressed():
    _, compressed = check_image(b"\xa5" * 1024)
    assert not compressed


def test_image_at_the_threshold_is_not_compressed():
    _, compressed = check_image(synthetic_firmware(STAGING_MAX))
    assert not compressed


def test_image_over_the_threshold_is_compressed():
    # 524280 is what objcopy emits for every board built from ld_eeprom_emu.ld.
    _, compressed = check_image(synthetic_firmware(524280))
    assert compressed


def test_incompressible_oversize_image_is_rejected():
    """An image that cannot be squeezed under the limit must fail loudly, not silently."""
    random.seed(99)
    incompressible = bytes(random.getrandbits(8) for _ in range(700 * 1024))
    try:
        pack(incompressible)
    except PackError as e:
        assert "too large" in str(e)
    else:
        raise AssertionError("expected PackError for an incompressible oversize image")


def test_header_is_big_endian():
    packed = pack(b"\x11" * 64)
    assert packed[:HEADER_LEN] == struct.pack(">IH", 64, crc_hqx(b"\x11" * 64, 0))


# --- standalone runner ----------------------------------------------------


def main(argv: list[str]) -> int:
    if HS_DECODE:
        print(f"Cross-checking against the C decoder at {HS_DECODE}")
    else:
        print("heatshrink2 round-trip only (set HS_DECODE for the C cross-check)")

    if len(argv) > 1:
        raw = open(argv[1], "rb").read()
        n, compressed = check_image(raw)
        pct = 100.0 * n / len(raw)
        print(f"  PASS {argv[1]}: {len(raw)} -> {n} bytes ({pct:.1f}%), compressed={compressed}")
        print(f"  {STAGING_MAX - n} bytes of headroom under the {STAGING_MAX}-byte limit")
        return 0

    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS {name}")
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            failures += 1
    print("All checks passed." if not failures else f"{failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
