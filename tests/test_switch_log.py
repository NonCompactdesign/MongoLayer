"""Switch-log writer/reader: no database, fake SwitchEvents."""

from adaptive_layer.decision import FAST_SETTINGS, MODERATE_SETTINGS, SAFE_SETTINGS, settings_name
from adaptive_layer.feedback import SwitchEvent
from adaptive_layer.monitor import Stats
from benchmark.switch_log import time_in_settings, write_switch_log


def _event(ts, old, new, reads, writes):
    stats = Stats(read_count=reads, write_count=writes, read_write_ratio=0.0, write_freq=0.0, seconds_since_last_write=None)
    return SwitchEvent(timestamp=ts, collection="c", old=old, new=new, stats=stats)


def test_settings_name_covers_the_three_named_configs_and_custom():
    assert [settings_name(s) for s in (SAFE_SETTINGS, FAST_SETTINGS, MODERATE_SETTINGS)] == ["SAFE", "FAST", "MODERATE"]
    assert settings_name(type(SAFE_SETTINGS)(write_concern=2, read_concern="local", read_preference="primary")) == "CUSTOM"


def test_time_in_settings_adds_up_to_the_run_length(tmp_path):
    path = tmp_path / "sw.csv"
    log = [
        _event(1010.0, MODERATE_SETTINGS, FAST_SETTINGS, 95, 5),   # 10 s into the run
        _event(1040.0, FAST_SETTINGS, SAFE_SETTINGS, 5, 95),       # 40 s
    ]
    write_switch_log(path, log, started_at=1000.0, stopped_at=1100.0)

    totals = time_in_settings(path)

    assert totals == {"FAST": 30.0, "MODERATE": 10.0, "SAFE": 60.0, "switches": 2}
    assert sum(totals[n] for n in ("FAST", "MODERATE", "SAFE")) == 100.0


def test_run_with_no_switches_is_all_moderate(tmp_path):
    path = tmp_path / "sw.csv"
    write_switch_log(path, [], started_at=500.0, stopped_at=560.0)
    assert time_in_settings(path) == {"FAST": 0.0, "MODERATE": 60.0, "SAFE": 0.0, "switches": 0}
