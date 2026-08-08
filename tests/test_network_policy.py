from datetime import datetime

from baixatube.network_policy import scheduled_rate_limit


def test_bandwidth_profile_switches_across_overnight_window():
    settings = {
        "bandwidth_day": "5m",
        "bandwidth_night": "20M",
        "bandwidth_night_start": 22,
        "bandwidth_night_end": 7,
    }

    assert scheduled_rate_limit(settings, datetime(2026, 8, 7, 14, 0)) == "5M"
    assert scheduled_rate_limit(settings, datetime(2026, 8, 7, 23, 0)) == "20M"
    assert scheduled_rate_limit(settings, datetime(2026, 8, 7, 2, 0)) == "20M"
