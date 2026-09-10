"""Tests for camera.tv46_gvsp: header parsing, reassembly, payload layout.

No hardware required. Multi-packet streams are synthesised in-memory with
exact GVSP header encoding.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from thermal_monitor.camera.tv46_gvsp import (
    COMBINED_BYTES,
    FORMAT_LEADER,
    FORMAT_PAYLOAD,
    FORMAT_TRAILER,
    GVSPBlock,
    GVSPReceiver,
    IR_BYTES,
    IR_HEIGHT,
    IR_WIDTH,
    LEADER_BYTES,
    VL_BYTES,
    parse_combined_payload,
    parse_gvsp_header,
)


def _datagram(block_id: int, packet_id: int, fmt: int, payload: bytes = b"") -> bytes:
    header = struct.pack(">HHI", 0, block_id, (fmt << 24) | (packet_id & 0x00FFFFFF))
    return header + payload


def test_parse_gvsp_header_fields():
    raw = _datagram(0x1234, 0xABCDE, FORMAT_PAYLOAD, b"xx")
    status, block_id, fmt, packet_id, is_last = parse_gvsp_header(raw)
    assert (status, block_id, fmt, packet_id, is_last) == (0, 0x1234, FORMAT_PAYLOAD, 0xABCDE, False)


def test_parse_gvsp_header_trailer_is_last():
    _, _, fmt, _, is_last = parse_gvsp_header(_datagram(1, 9, FORMAT_TRAILER))
    assert fmt == FORMAT_TRAILER and is_last is True


def test_parse_gvsp_header_rejects_short():
    with pytest.raises(ValueError):
        parse_gvsp_header(b"\x00\x01\x02")


def test_block_reassembles_in_order():
    block = GVSPBlock(10)
    assert block.add_packet(10, 0, b"aa", False) is True
    assert block.add_packet(10, 1, b"bb", True) is True
    assert block.is_complete is True
    assert block.reassemble() == b"aabb"


def test_block_reassembles_out_of_order_arrival():
    block = GVSPBlock(10)
    block.add_packet(10, 1, b"bb", True)
    assert block.is_complete is False  # packet 0 still missing
    assert block.get_missing_packets() == [0]
    block.add_packet(10, 0, b"aa", False)
    assert block.is_complete is True
    assert block.reassemble() == b"aabb"


def test_block_rejects_wrong_block_and_duplicates():
    block = GVSPBlock(10)
    assert block.add_packet(11, 0, b"xx", False) is False
    assert block.add_packet(10, 0, b"aa", False) is True
    assert block.add_packet(10, 0, b"aa", False) is False  # duplicate


def test_incomplete_block_reassemble_none():
    block = GVSPBlock(3)
    block.add_packet(3, 0, b"aa", False)
    assert block.reassemble() is None


def test_combined_payload_ir_little_endian_vl_shape():
    payload = bytearray(COMBINED_BYTES)
    payload[LEADER_BYTES : LEADER_BYTES + 2] = b"\x34\x12"
    frame = parse_combined_payload(bytes(payload), 77)
    assert frame.block_id == 77
    assert frame.ir.shape == (IR_HEIGHT, IR_WIDTH)
    assert frame.ir.dtype == np.uint16
    assert int(frame.ir[0, 0]) == 0x1234
    assert frame.vl.shape == (IR_HEIGHT, IR_WIDTH * 2)
    assert frame.vl.dtype == np.uint8


def test_combined_payload_accepts_trailer():
    frame = parse_combined_payload(bytes(COMBINED_BYTES + 4), 1)
    assert frame.ir.shape == (480, 640)


def test_combined_payload_rejects_bad_size():
    with pytest.raises(ValueError):
        parse_combined_payload(b"too-short", 1)
    with pytest.raises(ValueError):
        parse_combined_payload(bytes(COMBINED_BYTES + 5), 1)


def _feed(receiver: GVSPReceiver, block_id: int, count: int) -> None:
    """Drive _process_buffer directly with a synthetic `count`-packet block."""
    for pid in range(count - 1):
        datagram = _datagram(block_id, pid, FORMAT_LEADER if pid == 0 else FORMAT_PAYLOAD, b"P" * 8)
        receiver._receive_buffer[: len(datagram)] = datagram
        receiver._process_buffer(len(datagram))
    datagram = _datagram(block_id, count - 1, FORMAT_TRAILER, b"T" * 8)
    receiver._receive_buffer[: len(datagram)] = datagram
    receiver._process_buffer(len(datagram))


def test_receiver_completes_block_and_reports_id():
    rx = GVSPReceiver(local_ip="127.0.0.1")
    _feed(rx, 5, 4)
    result = rx.get_block_with_id(timeout=0.1)
    assert result is not None
    block_id, payload = result
    assert block_id == 5
    assert payload == b"P" * 8 * 3 + b"T" * 8
    stats = rx.get_stats()
    assert stats["blocks_completed"] == 1


def test_receiver_new_block_abandons_old_as_incomplete():
    rx = GVSPReceiver(local_ip="127.0.0.1")
    datagram = _datagram(5, 0, FORMAT_LEADER, b"xx")
    rx._receive_buffer[: len(datagram)] = datagram
    rx._process_buffer(len(datagram))
    datagram2 = _datagram(6, 0, FORMAT_LEADER, b"yy")
    rx._receive_buffer[: len(datagram2)] = datagram2
    rx._process_buffer(len(datagram2))
    assert rx.get_block_with_id(timeout=0.05) is None
    assert rx.get_stats()["blocks_incomplete"] == 1


def test_receiver_counts_duplicates_and_out_of_order():
    rx = GVSPReceiver(local_ip="127.0.0.1")
    first = _datagram(7, 0, FORMAT_LEADER, b"aa")
    rx._receive_buffer[: len(first)] = first
    rx._process_buffer(len(first))
    rx._receive_buffer[: len(first)] = first
    rx._process_buffer(len(first))  # duplicate
    second = _datagram(7, 2, FORMAT_PAYLOAD, b"cc")
    rx._receive_buffer[: len(second)] = second
    rx._process_buffer(len(second))
    first_again = _datagram(7, 1, FORMAT_PAYLOAD, b"bb")
    rx._receive_buffer[: len(first_again)] = first_again
    rx._process_buffer(len(first_again))  # packet_id 1 < last seen 2
    stats = rx.get_stats()
    assert stats["duplicate_packets"] == 1
    assert stats["out_of_order_packets"] == 1


def test_receiver_ignores_stale_block_id():
    rx = GVSPReceiver(local_ip="127.0.0.1")
    _feed(rx, 100, 2)
    stale = _datagram(99, 0, FORMAT_LEADER, b"zz")  # backwards: ignored
    rx._receive_buffer[: len(stale)] = stale
    rx._process_buffer(len(stale))
    result = rx.get_block_with_id(timeout=0.1)
    assert result is not None and result[0] == 100


def test_receiver_block_id_wrap_forward_distance():
    rx = GVSPReceiver(local_ip="127.0.0.1")
    _feed(rx, 0xFFFF, 2)
    first = rx.get_block_with_id(timeout=0.1)
    _feed(rx, 0x0000, 2)  # wrap: forward distance 1 -> accepted
    second = rx.get_block_with_id(timeout=0.1)
    assert first is not None and second is not None
    assert (first[0], second[0]) == (0xFFFF, 0x0000)


def test_receiver_block_timeout_marks_incomplete():
    rx = GVSPReceiver(local_ip="127.0.0.1", block_timeout_s=0.0)
    datagram = _datagram(42, 0, FORMAT_LEADER, b"xx")
    rx._receive_buffer[: len(datagram)] = datagram
    rx._process_buffer(len(datagram))
    rx._cleanup_old_blocks()
    stats = rx.get_stats()
    assert stats["block_timeouts"] == 1
    assert stats["blocks_incomplete"] == 1


def test_receiver_latest_only_overwrite_counts_incomplete():
    rx = GVSPReceiver(local_ip="127.0.0.1")
    _feed(rx, 1, 2)
    _feed(rx, 2, 2)  # overwrites unconsumed block 1
    assert rx.get_stats()["blocks_incomplete"] == 1
    result = rx.get_block_with_id(timeout=0.1)
    assert result is not None and result[0] == 2


def test_receiver_get_block_timeout_returns_none_no_synthesis():
    rx = GVSPReceiver(local_ip="127.0.0.1")
    assert rx.get_block_with_id(timeout=0.05) is None
