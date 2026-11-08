"""Resource-limit tests for Narwal map decoding and rendering."""

from __future__ import annotations

import zlib

from custom_components.narwal.narwal_client.map_renderer import (
    MAX_COMPRESSED_MAP_BYTES,
    MAX_DECOMPRESSED_MAP_BYTES,
    MAX_MAP_DIMENSION,
    decompress_map,
    render_base_map,
    render_map_png,
)


def test_decompress_map_rejects_oversized_compressed_payload() -> None:
    # Not a zlib/gzip header, so without the size gate this would be echoed
    # back as legacy raw map data instead of returning empty bytes.
    compressed = b"\xff" * (MAX_COMPRESSED_MAP_BYTES + 1)

    assert decompress_map(compressed) == b""


def test_decompress_map_rejects_excessive_expansion() -> None:
    # Raw deflate avoids the zlib header so a missing limit cannot be masked
    # by the malformed-compressed-data fallback.
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(b"\x00" * (MAX_DECOMPRESSED_MAP_BYTES + 1))
    compressed += compressor.flush()

    assert len(compressed) < MAX_COMPRESSED_MAP_BYTES
    assert decompress_map(compressed) == b""


def test_map_renderers_reject_unsafe_dimensions_before_image_allocation() -> None:
    packed = b"\x0a\x01\x00"

    assert render_map_png(packed, MAX_MAP_DIMENSION + 1, 1) == b""
    assert render_base_map(zlib.compress(packed), MAX_MAP_DIMENSION, MAX_MAP_DIMENSION) is None
    # 1025 * 1024 exceeds the total-pixel cap while each side stays within limits.
    assert render_base_map(zlib.compress(packed), 1025, 1024) is None
