"""
V3 Calibration Parser

Loads and parses the TV46L calibration blob (hex-encoded text file).
Matches V2 proven implementation exactly.
"""

from __future__ import annotations

import binascii
import struct
from pathlib import Path

from thermal_monitor.calibration.models import (
    CameraCalibration,
    CalibrationRange,
    UniverseSegment,
)

from thermal_monitor.core.logging import get_logger

logger = get_logger(__name__)


class CalibrationParser:
    """
    Parses the TV46L calibration blob.

    File Layout
    -----------
    Header: 16 bytes
    Range 0: Descriptor
    Range 1: Descriptor
    Range 2: Descriptor
    """

    HEADER_SIZE = 16
    SEGMENT_SIZE = 20
    SEGMENTS_PER_RANGE = 11

    def load(self, calibration_file: Path) -> CameraCalibration:
        """
        Load and parse a calibration blob.

        Parameters
        ----------
        calibration_file
            Path to calibration_blob.txt

        Returns
        -------
        CameraCalibration
        """
        logger.info(f"Loading calibration: {calibration_file}")

        blob = self._load_blob(calibration_file)

        calibration = CameraCalibration()

        offset = self._parse_header(blob, calibration)

        self._parse_ranges(blob, calibration, offset)

        logger.info("Calibration parsing completed.")

        return calibration

    def _load_blob(self, calibration_file: Path) -> bytes:
        """
        Load calibration blob from text file.

        Returns
        -------
        bytes
        """
        with open(calibration_file, "r") as file:
            hex_string = file.read().strip()

        if hex_string.startswith("0x"):
            hex_string = hex_string[2:]

        blob = binascii.unhexlify(hex_string)

        logger.info(f"Calibration blob size: {len(blob)} bytes")

        return blob

    def _parse_header(
        self,
        blob: bytes,
        calibration: CameraCalibration,
    ) -> int:
        """
        Parse calibration header.

        Returns
        -------
        Offset of first descriptor.
        """
        (
            calibration.magic,
            calibration.enabled_ranges,
            calibration.enabled_mask,
            encoded_date,
        ) = struct.unpack_from("<IIII", blob, 0)

        run_number = encoded_date & 0x03

        calibration.calibration_date = self._decode_date(encoded_date)

        logger.info("=" * 60)
        logger.info("Calibration Header")
        logger.info("=" * 60)
        logger.info(f"Magic            : 0x{calibration.magic:08X}")
        logger.info(f"Enabled Ranges   : {calibration.enabled_ranges}")
        logger.info(f"Enabled Mask     : 0x{calibration.enabled_mask:08X}")
        logger.info(f"Calibration Date : {calibration.calibration_date}")
        logger.info(f"Run Number       : {run_number}")

        return self.HEADER_SIZE

    @staticmethod
    def _decode_date(encoded_date: int) -> str:
        """
        Decode packed calibration date.

        Bit Layout:
        bits 0-1: Run number
        bits 2-6: Day
        bits 7-10: Month
        bits 11-15: Year offset from 2000
        """
        day = (encoded_date >> 2) & 0x1F
        month = (encoded_date >> 7) & 0x0F
        year = ((encoded_date >> 11) & 0x1F) + 2000

        return f"{day:02d}/{month:02d}/{year}"

    def _parse_ranges(
        self,
        blob: bytes,
        calibration: CameraCalibration,
        offset: int,
    ) -> None:
        """Parse every calibration range contained in the blob."""
        logger.info("=" * 60)
        logger.info("Calibration Ranges")
        logger.info("=" * 60)

        current_offset = offset

        for range_index in range(calibration.enabled_ranges):
            calibration_range, current_offset = self._parse_range(
                blob, current_offset
            )

            calibration.add_range(calibration_range)

            logger.info(f"Range {range_index}")
            logger.info(
                f"  Temperature Range: "
                f"{calibration_range.calibration_min:.2f} °C -> "
                f"{calibration_range.calibration_max:.2f} °C"
            )
            logger.info(
                f"  Display Range    : "
                f"{calibration_range.display_min:.2f} °C -> "
                f"{calibration_range.display_max:.2f} °C"
            )
            logger.info(
                f"  Polynomial Segments: {calibration_range.num_segments}"
            )

        logger.info(f"Parsed {len(calibration.ranges)} calibration ranges.")

    def _parse_range(
        self,
        blob: bytes,
        offset: int,
    ) -> tuple[CalibrationRange, int]:
        """Parse one calibration descriptor."""
        position = offset

        calibration_min = struct.unpack_from("<f", blob, position)[0]
        position += 4

        calibration_max = struct.unpack_from("<f", blob, position)[0]
        position += 4

        display_min = struct.unpack_from("<f", blob, position)[0]
        position += 4

        display_max = struct.unpack_from("<f", blob, position)[0]
        position += 4

        manual_palette_span = struct.unpack_from("<f", blob, position)[0]
        position += 4

        auto_palette_span = struct.unpack_from("<f", blob, position)[0]
        position += 4

        number_of_segments = struct.unpack_from("<I", blob, position)[0]
        position += 4

        calibration_range = CalibrationRange(
            calibration_min=calibration_min,
            calibration_max=calibration_max,
            display_min=display_min,
            display_max=display_max,
            manual_palette_span=manual_palette_span,
            auto_palette_span=auto_palette_span,
            num_segments=number_of_segments,
        )

        # Read all polynomial segments.
        # The blob always stores 11 segments.
        # Only num_segments are actually valid.
        for _ in range(self.SEGMENTS_PER_RANGE):
            segment, position = self._parse_segment(blob, position)
            calibration_range.segments.append(segment)

        return calibration_range, position

    def _parse_segment(
        self,
        blob: bytes,
        offset: int,
    ) -> tuple[UniverseSegment, int]:
        """Parse one polynomial segment."""
        (
            u0,
            u1,
            u2,
            start_temp,
            end_temp,
        ) = struct.unpack_from("<5f", blob, offset)

        segment = UniverseSegment(
            u0=u0,
            u1=u1,
            u2=u2,
            start_temp=start_temp,
            end_temp=end_temp,
        )

        return segment, offset + self.SEGMENT_SIZE