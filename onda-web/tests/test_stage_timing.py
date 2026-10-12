"""Real stage boundaries, repeated attempts, closed clocks and private schemas."""
import math
import pytest
from api.stage_timing import StageTiming
from api.progress import ProgressReporter
from api.download_history import validate_record
from test_download_history import record


def test_repeated_stages_add_wall_time_without_double_counting():
    clock=[0.0];timing=StageTiming(clock=lambda:clock[0])
    timing.enter('extracting');clock[0]=2;timing.enter('downloading')
    clock[0]=7;timing.enter('extracting');clock[0]=8;timing.enter('downloading')
    clock[0]=11;timing.enter('converting');clock[0]=15
    assert timing.snapshot()=={'extracting':3,'downloading':8,'converting':4}
    assert timing.snapshot()=={'extracting':3,'downloading':8,'converting':4}
    final=timing.snapshot(finish=True);clock[0]=50;timing.enter('delivering')
    assert timing.snapshot(finish=True)==final
    assert sum(final.values())==15


def test_untracked_verification_stops_worker_conversion_clock():
    clock=[0.0];timing=StageTiming(clock=lambda:clock[0])
    timing.enter('converting');clock[0]=3;timing.enter('verifying');clock[0]=8
    assert timing.snapshot()=={'converting':3}


def test_suppressed_progress_events_do_not_lose_phase_time():
    clock=[0.0];reporter=ProgressReporter(clock=lambda:clock[0])
    reporter.emit('extracting');clock[0]=2;reporter.begin_download({'private-cookie-file':100})
    clock[0]=2.1;reporter.transfer('private-cookie-file',1,100)
    clock[0]=5;reporter.emit('converting');clock[0]=9
    assert reporter.timing.snapshot()=={'extracting':2,'downloading':3,'converting':4}
    assert 'private-cookie-file' not in str(list(reporter.events.queue))


@pytest.mark.parametrize('times',[{},[],{'cookies':1},{'extracting':True},{'extracting':-1},{'extracting':math.nan},{'extracting':math.inf},{'extracting':'secret'},{'extracting':2**53}])
def test_timing_schema_rejects_unmeasured_or_sensitive_values(times):
    with pytest.raises(ValueError):validate_record(record(stageSeconds=times))


def test_old_records_and_measured_zero_are_both_supported():
    assert validate_record(record())
    assert validate_record(record(stageSeconds={'extracting':0,'downloading':2.3}))
