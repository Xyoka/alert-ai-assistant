from datetime import datetime
from urllib.parse import parse_qs, urlparse

from alert_ai_assistant.config import MonitorApiConfig
from alert_ai_assistant.sources import MonitorApiSource


def test_monitor_api_paginates_until_short_page():
    source = FakeMonitorApiSource(
        pages=[
            [{"id": "1"}, {"id": "2"}],
            [{"id": "3"}, {"id": "4"}],
            [{"id": "5"}],
        ],
        page_limit=2,
    )

    items = source._request_with_units("unhandled", [])

    assert [item["id"] for item in items] == ["1", "2", "3", "4", "5"]
    assert source.calls == [(0, 2), (2, 2), (4, 2)]


def test_monitor_api_stops_when_api_repeats_same_page():
    repeated = [{"id": "1"}, {"id": "2"}]
    source = FakeMonitorApiSource(
        pages=[repeated, repeated, repeated],
        page_limit=2,
    )

    items = source._request_with_units("unhandled", [])

    assert [item["id"] for item in items] == ["1", "2"]
    assert source.calls == [(0, 2), (2, 2)]


def test_monitor_fetches_unhandled_and_processing_in_target_hour_only():
    source = RecordingMonitorApiSource()
    window_start = datetime(2026, 5, 8, 17, 0, 0)
    window_end = datetime(2026, 5, 8, 17, 59, 59)

    source.fetch_for_summary(window_start, window_end)

    unhandled = source.ranges["unhandled"][0]
    processing = source.ranges["processing"][0]
    assert unhandled == (window_start, window_end)
    assert processing == (window_start, window_end)


def test_payload_to_record_falls_back_to_create_time_when_last_alarm_time_missing():
    source = MonitorApiSource(
        MonitorApiConfig(
            field_mapping={
                "device_ip": "ip",
                "hostname": "instance_name",
                "alarm_time": "create_time",
                "title": "alarm_title",
                "content": "alarm_content",
                "severity": "alarm_level_id",
                "external_id": "id",
            }
        )
    )

    record = source._payload_to_record(
        {
            "id": "a1",
            "ip": "10.0.0.1",
            "instance_name": "SW-A",
            "create_time": "2026-05-08 17:42:18",
            "alarm_title": "CPU告警",
            "alarm_content": "CPU high",
        },
        "processing",
    )

    assert record.alarm_time_text == "2026-05-08 17:42:18"


def test_monitor_api_uses_configured_sid_param_name():
    source = MonitorApiSource(
        MonitorApiConfig(
            base_url="https://example.invalid",
            search_path="/api/monitor/alarm/search",
            sid="sid",
            sid_param_name="token",
        )
    )

    url = source._build_search_url(
        {
            "offset": 0,
            "limit": 100,
            "search_unit_list": "[]",
            source.config.sid_param_name: source.config.sid,
        }
    )

    query = parse_qs(urlparse(url).query)
    assert query["token"] == ["sid"]
    assert "secret" not in query


def test_ended_alarm_accepts_recover_time_as_end_time():
    source = EndedPayloadSource(
        payload={
            "id": "1",
            "ip": "10.0.0.1",
            "hostname": "SW-A",
            "create_time": "2026-05-08 16:00:00",
            "recover_time": "2026-05-08 17:10:00",
            "title": "端口Down恢复",
            "content": "up",
        }
    )

    records = source.fetch_for_summary(
        datetime(2026, 5, 8, 17, 0, 0),
        datetime(2026, 5, 8, 17, 59, 59),
    )

    assert len(records) == 1
    assert records[0].alarm_time_text == "2026-05-08 17:10:00"


class FakeMonitorApiSource(MonitorApiSource):
    def __init__(self, pages: list[list[dict]], page_limit: int, sid_param_name: str = "token") -> None:
        super().__init__(
            MonitorApiConfig(
                base_url="https://example.invalid",
                sid="sid",
                sid_param_name=sid_param_name,
                page_limit=page_limit,
                max_pages=10,
            )
        )
        self.pages = pages
        self.calls: list[tuple[int, int]] = []

    def _request_page(self, bucket, units, offset, limit):
        self.calls.append((offset, limit))
        page_index = offset // limit
        if page_index >= len(self.pages):
            return []
        return list(self.pages[page_index])


class RecordingMonitorApiSource(MonitorApiSource):
    def __init__(self) -> None:
        super().__init__(MonitorApiConfig(active_lookback_days=5))
        self.ranges: dict[str, list[tuple[datetime, datetime]]] = {
            "unhandled": [],
            "processing": [],
            "ended": [],
        }

    def fetch_bucket(self, bucket, start, end):
        self.ranges[bucket].append((start, end))
        return []

    def _fetch_processing_window(self, bucket, start, end):
        self.ranges[bucket].append((start, end))
        return []

    def _request_bucket(self, bucket, start, end):
        self.ranges[bucket].append((start, end))
        return []


class EndedPayloadSource(MonitorApiSource):
    def __init__(self, payload: dict) -> None:
        super().__init__(
            MonitorApiConfig(
                field_mapping={
                    "device_ip": "ip",
                    "hostname": "hostname",
                    "alarm_time": "create_time",
                    "title": "title",
                    "content": "content",
                    "external_id": "id",
                }
            )
        )
        self.payload = payload

    def fetch_bucket(self, bucket, start, end):
        return []

    def _fetch_processing_window(self, bucket, start, end):
        return []

    def _request_bucket(self, bucket, start, end):
        return [self.payload]
