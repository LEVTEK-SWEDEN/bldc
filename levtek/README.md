# LEVTEK tooling

LEVTEK-specific additions to this fork, kept in one directory so the diff against
upstream `vedderb/bldc` stays confined and rebases do not conflict. Nothing here is
used by the firmware build itself.

## Packing firmware for OTA

`levkart-ota` updates the LESCs over CAN by staging an image in the bootloader's
new-app area. The raw build output cannot go there unchanged: the linker keeps
`.crcinfo` at the absolute address `0x0807FFF0` (`ld_eeprom_emu.ld:30,156-161`), so
`objcopy` pads every image out to 524280 bytes, while the staging area is 393216
bytes (`flash_helper.c:42-45`).

VESC Tool handles this by heatshrink-compressing anything over the threshold and
flagging it with `0xCC` in the top byte of the size field; the bootloader
decompresses on the way in. That path is not gated on any capability check, so it
is the normal case rather than a fallback.

`pack_firmware_ota.py` is a dependency-free equivalent of VESC Tool's
`--packFirmware`, for local use, for recovering a raw image, and as the independent
check on what CI produces. CI itself packs with VESC Tool — see [In CI](#in-ci).

### Setup

Python 3.10 or newer. On Ubuntu 24.04 and other PEP 668 distributions a virtualenv
is required; a system-wide `pip install` is refused.

```bash
cd levtek
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Packing an image

```bash
./pack_firmware_ota.py ../build/levtek_1_0_3/levtek_1_0_3.bin levtek_1_0_3_packed.bin
```

```
compressed 524280 -> 348176 bytes (66.4%), 45032 bytes of headroom under the 393208-byte limit
wrote levtek_1_0_3_packed.bin (348182 bytes)
```

The script fails rather than emitting an image the bootloader would reject: it
round-trips every compressed payload, and errors out if the result still does not
fit, in which case the firmware itself has to shrink.

A packed image stays loadable by VESC Tool, which unpacks it transparently — so
flashing one by hand is a good way to confirm the packing before any OTA code is
involved.

### Recovering the raw image

CI publishes the packed image, so recovering a bricked board over SWD needs the
raw one back:

```bash
./pack_firmware_ota.py --unpack levtek_1_0_3_packed.bin recovered.bin
```

This reproduces the original build byte for byte. Unpacking refuses anything whose
header or CRC does not check out, rather than emitting garbage.

### In CI

`cloudbuild.yaml` packs with **VESC Tool**, not with this script: it downloads
`vesc_tool_<version>` from Artifact Registry and runs
`--packFirmware in.bin:out.bin`, so the reference implementation of the format is
the one producing the artifact. The same pattern `levkart-esc-software` already
uses for conf generation.

A following step then unpacks that output with `pack_firmware_ota.py --unpack` and
requires the result to match the image just built. That independent check is what
makes using an opaque prebuilt binary safe — if the two implementations ever
disagree, whether from a vesc_tool upgrade changing the format, a truncated
download or a codec-parameter change, the build fails instead of shipping an image
the bootloader cannot decompress.

The result is written to `build/packed/<board>.bin` and published from there. The
artifact keeps the basename `<board>.bin` — only the contents change — so nothing
downstream has to learn a new file name.

`_VESC_TOOL_VERSION` is pinned deliberately: the format is fixed by the bootloader
on the boards, not by whatever is newest upstream, so tracking upstream
automatically would be the wrong default.

Verified byte-identical: `vesc_tool 7.00 --packFirmware` and `pack_firmware_ota.py`
produce the same sha256 for a real `levtek_1_0_3` build. So this script remains a
faithful stand-in for local use and recovery.

### Tests

```bash
./test_pack_firmware_ota.py                                       # boundaries and header format
./test_pack_firmware_ota.py ../build/levtek_1_0_3/levtek_1_0_3.bin  # against a real build
pytest test_pack_firmware_ota.py                                  # same tests under pytest
```

By default the round-trip uses `heatshrink2` on both sides, which proves the format
but not that the codec parameters match the bootloader's. Setting `HS_DECODE` to a
decoder built from the heatshrink sources VESC Tool ships cross-checks against the
same upstream library the bootloader uses; `test_pack_firmware_ota.py`'s docstring
has the build line.

## Notes

- The codec parameters (window 13, lookahead 5) are an unversioned contract with a
  bootloader that lives in a different repo. The round-trip check only proves the
  encoder and decoder agree in Python — confirm against hardware by flashing a
  packed image with VESC Tool.
- Headroom on `levtek_1_0_3` is currently ~45 KB (11.5%). It shrinks as the
  firmware grows, and the failure mode is a hard wall at build time with no
  workaround short of shrinking the firmware.
