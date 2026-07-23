"""
UUID v7 generation utility.
UUID v7 is time-ordered and uses Unix timestamp in milliseconds.
"""

import os
import time
from uuid import UUID


def uuid7() -> UUID:
    """
    Generate a UUID v7.

    UUID v7 format:
    - 48 bits: Unix timestamp in milliseconds
    - 4 bits: version (7)
    - 12 bits: random
    - 2 bits: variant
    - 62 bits: random
    """
    # Get current timestamp in milliseconds
    timestamp_ms = int(time.time() * 1000)

    # Generate random bytes
    random_bytes = os.urandom(10)

    # Build UUID bytes
    uuid_bytes = bytearray(16)

    # First 6 bytes: timestamp (48 bits)
    uuid_bytes[0] = (timestamp_ms >> 40) & 0xFF
    uuid_bytes[1] = (timestamp_ms >> 32) & 0xFF
    uuid_bytes[2] = (timestamp_ms >> 24) & 0xFF
    uuid_bytes[3] = (timestamp_ms >> 16) & 0xFF
    uuid_bytes[4] = (timestamp_ms >> 8) & 0xFF
    uuid_bytes[5] = timestamp_ms & 0xFF

    # Next 2 bytes: version (4 bits) + random (12 bits)
    uuid_bytes[6] = 0x70 | (random_bytes[0] & 0x0F)  # version 7
    uuid_bytes[7] = random_bytes[1]

    # Next 2 bytes: variant (2 bits) + random (14 bits)
    uuid_bytes[8] = 0x80 | (random_bytes[2] & 0x3F)  # variant 10
    uuid_bytes[9] = random_bytes[3]

    # Remaining 6 bytes: random
    uuid_bytes[10:16] = random_bytes[4:10]

    return UUID(bytes=bytes(uuid_bytes))
