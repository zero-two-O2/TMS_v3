"""Tests for storage database and repositories."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

try:
    import pyodbc
    _HAS_PYODBC = True
except ImportError:
    _HAS_PYODBC = False

pytestmark = pytest.mark.skipif(not _HAS_PYODBC, reason="pyodbc not installed")

from thermal_monitor.core.models import (
    AlarmCondition,
    AlarmRule,
    AlarmSeverity,
    AnalysisConfig,
    CameraConfig,
    CameraIdentity,
    PositionROIAssociation,
    RecordingConfig,
    RecordingMetadata,
    RecordingState,
    RecordingTrigger,
    ROIConfig,
    ROIGeometry,
    ROIShape,
    SystemConfig,
    TemperatureLimits,
    TemperatureUnit,
)
from thermal_monitor.storage.database import Database, DatabaseConfig, run_migrations
from thermal_monitor.storage.repositories import (
    AlarmEventRepository,
    AlarmRuleRepository,
    AnalysisConfigRepository,
    CameraRepository,
    PositionROIRepository,
    RecordingConfigRepository,
    RecordingRepository,
    ROIRepository,
    SystemConfigRepository,
)


class MockCursor:
    """Mock database cursor."""

    def __init__(self) -> None:
        self.rowcount = 0
        self._results: list[tuple] = []
        self._index = 0
        self.executed_sql = ""
        self.executed_params = ()

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed_sql = sql
        self.executed_params = params

    def fetchone(self) -> tuple | None:
        if self._index < len(self._results):
            result = self._results[self._index]
            self._index += 1
            return result
        return None

    def fetchall(self) -> list[tuple]:
        results = self._results[self._index:]
        self._index = len(self._results)
        return results

    def set_results(self, results: list[tuple]) -> None:
        self._results = results
        self._index = 0


class MockConnection:
    """Mock database connection."""

    def __init__(self) -> None:
        self.cursor = MagicMock(return_value=MockCursor())
        self._committed = False
        self._rolled_back = False
        self.closed = False
        self.timeout = 30

    def commit(self) -> None:
        self._committed = True

    def rollback(self) -> None:
        self._rolled_back = True

    def close(self) -> None:
        self.closed = True


class TestDatabaseConfig:
    def test_connection_string_with_credentials(self):
        config = DatabaseConfig(
            server="localhost",
            database="tms_v3",
            username="user",
            password="pass",
        )
        conn_str = config.connection_string
        assert "UID=user" in conn_str
        assert "PWD=pass" in conn_str
        assert "Trusted_Connection=yes" not in conn_str

    def test_connection_string_without_credentials(self):
        config = DatabaseConfig(
            server="localhost",
            database="tms_v3",
        )
        conn_str = config.connection_string
        assert "Trusted_Connection=yes" in conn_str


class TestDatabase:
    def test_connect_disconnect(self):
        config = DatabaseConfig(server="localhost", database="test")
        with patch("pyodbc.connect", return_value=MockConnection()) as mock_connect:
            db = Database(config)
            conn = db.connect()
            assert conn is not None
            assert mock_connect.called

            db.disconnect()
            assert conn.closed

    def test_transaction_commit(self):
        config = DatabaseConfig(server="localhost", database="test")
        mock_conn = MockConnection()
        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(config)
            with db.transaction() as cursor:
                cursor.execute("SELECT 1")
            assert mock_conn._committed

    def test_transaction_rollback(self):
        config = DatabaseConfig(server="localhost", database="test")
        mock_conn = MockConnection()
        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(config)
            try:
                with db.transaction() as cursor:
                    cursor.execute("SELECT 1")
                    raise RuntimeError("test error")
            except RuntimeError:
                pass
            assert mock_conn._rolled_back


class TestCameraRepository:
    def create_camera_config(self) -> CameraConfig:
        identity = CameraIdentity(
            camera_id="cam_001",
            serial_number="SN12345",
            model="TV46L",
            vendor="Fluke",
        )
        return CameraConfig(
            identity=identity,
            name="Camera 1",
            thermal_enabled=True,
            visible_enabled=False,
        )

    def test_insert(self):
        config = self.create_camera_config()
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = CameraRepository(db)
            result = repo.insert(config)

            assert result.success
            assert result.data == config

    def test_find_by_camera_id(self):
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value

        # Mock row data matching CameraRow structure
        mock_cursor.set_results([(
            1, "cam_001", "SN12345", "TV46L", "Fluke", "", "",
            "Camera 1", "", True, True, False,
            "device_id", "192.168.1.1",
            9, 500, 1048576, 8,
            "IR_Data", 16, None, -1,
            3, 3.0, 2.0, 10,
            -170.0, 170.0, -90.0, 90.0, 1.0, 30.0,
            0.0, 0.0, 1.0, "manual", 10.0, 10.0, 5.0,
        )])

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = CameraRepository(db)
            result = repo.find_by_camera_id("cam_001")

            assert result.success
            assert result.data is not None
            assert len(result.data) == 1
            assert result.data[0].identity.camera_id == "cam_001"


class TestROIRepository:
    def create_roi_config(self) -> ROIConfig:
        geometry = ROIGeometry(
            shape=ROIShape.RECT,
            parameters={"x": 100, "y": 100, "width": 200, "height": 150},
        )
        limits = TemperatureLimits(
            unit=TemperatureUnit.CELSIUS,
            max_warning=80.0,
            max_critical=100.0,
        )
        return ROIConfig(
            roi_id="roi_1",
            name="Test ROI",
            geometry=geometry,
            temperature_limits=limits,
        )

    def test_insert(self):
        config = self.create_roi_config()
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = ROIRepository(db)
            result = repo.insert(config)

            assert result.success


class TestAlarmRuleRepository:
    def create_alarm_rule(self) -> AlarmRule:
        return AlarmRule(
            rule_id="rule_1",
            roi_id="roi_1",
            condition=AlarmCondition.ABOVE,
            severity=AlarmSeverity.WARNING,
            threshold=80.0,
        )

    def test_insert(self):
        rule = self.create_alarm_rule()
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = AlarmRuleRepository(db)
            result = repo.insert(rule)

            assert result.success


class TestAlarmEventRepository:
    def test_insert(self):
        event = AlarmEvent(
            event_id="evt_001",
            rule_id="rule_1",
            camera_id="cam_001",
            roi_id="roi_1",
            severity=AlarmSeverity.WARNING,
            measured_value=85.0,
            threshold_value=80.0,
            timestamp=1234567890.0,
            frame_sequence=100,
        )
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = AlarmEventRepository(db)
            result = repo.insert(event)

            assert result.success


class TestRecordingRepository:
    def create_recording_metadata(self) -> RecordingMetadata:
        return RecordingMetadata(
            recording_id="rec_001",
            camera_id="cam_001",
            trigger=RecordingTrigger.ALARM,
            state=RecordingState.RECORDING,
            start_timestamp=1234567890.0,
            start_sequence=100,
            pre_alarm_frames=90,
            post_alarm_frames=270,
        )

    def test_insert(self):
        metadata = self.create_recording_metadata()
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = RecordingRepository(db)
            result = repo.insert(metadata)

            assert result.success

    def test_update_state(self):
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.rowcount = 1

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = RecordingRepository(db)
            result = repo.update_state("rec_001", RecordingState.COMPLETED)

            assert result.success
            assert result.data is True


class TestSystemConfigRepository:
    def test_get_set_config(self):
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.set_results([('{"key": "value"}',)])

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = SystemConfigRepository(db)

            # Test set
            result = repo.set_config("test_key", {"key": "value"}, "Test description")
            assert result

            # Test get
            value = repo.get_config("test_key")
            assert value == {"key": "value"}

    def test_get_nonexistent(self):
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value
        mock_cursor.set_results([])

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = SystemConfigRepository(db)

            value = repo.get_config("nonexistent", "default")
            assert value == "default"


class TestRecordingConfigRepository:
    def create_recording_config(self) -> RecordingConfig:
        return RecordingConfig(
            camera_id="cam_001",
            enabled=True,
            pre_alarm_seconds=10.0,
            post_alarm_seconds=30.0,
        )

    def test_insert(self):
        config = self.create_recording_config()
        mock_conn = MockConnection()
        mock_cursor = mock_conn.cursor.return_value

        with patch("pyodbc.connect", return_value=mock_conn):
            db = Database(DatabaseConfig(server="localhost", database="test"))
            repo = RecordingConfigRepository(db)
            result = repo.insert(config)

            assert result.success


if __name__ == "__main__":
    pytest.main([__file__, "-v"])