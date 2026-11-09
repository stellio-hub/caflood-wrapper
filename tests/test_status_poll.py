# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EGM
"""caflood_status.py (run state from a run folder) and caflood_poll.py (scan + mark)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from caflood_poll import REPORTED_MARKER, mark_reported, poll_entry, scan_local, scan_remote, split_runs
from caflood_status import (
    CLAIM_FILE,
    CLAIM_TIMEOUT_S,
    RESULT_FILE,
    config_value,
    determine_status,
    parse_bundle,
    parse_time_end,
    pid_alive,
    read_dir,
    service_execution_id,
)
from conftest import EXECUTION_ID, caflood_log, config_text, status_bundle

LOG, CUTS = caflood_log(time_end_s=3600, period_s=900)


def status(**kwargs):
    return determine_status(parse_bundle(status_bundle(**kwargs)))


# --- config.csv --------------------------------------------------------------

def test_config_helpers():
    text = config_text(3600)
    assert config_value(text, "Simulation Name") == EXECUTION_ID
    assert config_value(text, "Missing") is None
    assert parse_time_end(text) == 3600
    assert parse_time_end(config_text(3601)) == 3660  # rounded up to Update DT like CAFLOOD
    assert service_execution_id("folder", text) == EXECUTION_ID
    assert service_execution_id("abc", "") == "urn:ngsi-ld:ServiceExecution:abc"


# --- decision table ----------------------------------------------------------

def test_completed():
    s = status(exit_code="0", alive="no", log_lines=LOG)
    assert (s["state"], s["phase"], s["progress"], s["code"], s["error"]) == ("completed", "done", 1.0, 0, None)


def test_pending_before_launch():
    assert status(alive="nopid", log_lines=[])["state"] == "pending"


@pytest.mark.parametrize("cut, phase, progress", [
    ("preprocessing", "preprocessing", 0.0),
    ("mid_simulation", "simulation", 0.95 * 0.5),
    ("end_of_simulation", "simulation", 0.95),
    ("mid_postprocessing", "postprocessing", 0.95 + 0.05 * 0.5),
])
def test_running_phases_and_progress(cut, phase, progress):
    s = status(alive="yes", log_lines=LOG[:CUTS[cut]])
    assert (s["state"], s["phase"]) == ("running", phase)
    assert s["progress"] == pytest.approx(progress, abs=1e-3)


def test_quality_code_found_even_outside_log_tail():
    padding = ["Write Raster Grid: output_dir/x_raster_WD_1\n"] * 2000  # > 64 KB after CODE:
    s = status(exit_code="0", alive="no", log_lines=LOG[:CUTS["end_of_simulation"]] + padding)
    assert s["code"] == 0


def test_died_without_exit_code():
    s = status(alive="no", log_lines=LOG[:CUTS["mid_simulation"]])
    assert s["state"] == "failed" and "no longer running" in s["error"]


@pytest.mark.parametrize("log_lines, expected", [
    (["Error opening CSV file: ./inflow_0.csv\n", "Error reading Inflow Event CSV file: inflow_0.csv\n"],
     "Error opening CSV file: ./inflow_0.csv | Error reading Inflow Event CSV file: inflow_0.csv"),
    (["Error reading 'Max DT' element\n"], "Error reading 'Max DT' element"),
    (LOG[:CUTS["mid_simulation"]] + ["std::bad_alloc\n", "Simulation stopped\n"], "std::bad_alloc | Simulation stopped"),
])
def test_caflood_errors_are_reported(log_lines, expected):
    s = status(exit_code="1", alive="no", log_lines=log_lines)
    assert (s["state"], s["error"]) == ("failed", expected)


def test_killed_by_signal():
    s = status(exit_code="137", alive="no", log_lines=LOG[:CUTS["mid_simulation"]])
    assert s["state"] == "failed" and "SIGKILL" in s["error"]


def test_old_run_without_pid_is_inferred_from_log():
    assert status(alive="nopid", log_lines=LOG)["state"] == "completed"
    assert status(alive="nopid", log_lines=LOG[:CUTS["mid_simulation"]])["state"] == "running"


def test_pid_alive_probes_without_killing():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(proc.pid)
        assert proc.poll() is None  # still running after the probe (os.kill(pid, 0) would kill it on Windows)
    finally:
        proc.kill()
        proc.wait()
    assert not pid_alive(proc.pid)
    assert not pid_alive(0)


# --- poll ----------------------------------------------------------------------

def test_split_runs():
    out = "@@run a\n@@exit_code\n0\n@@run b\n@@alive\nyes\n"
    assert split_runs(out) == {"a": "@@exit_code\n0", "b": "@@alive\nyes"}


def _run_folder(root, name, *, log=None, exit_code=None, reported=False, is_run=True):
    d = root / name
    d.mkdir()
    if is_run:
        (d / "config.csv").write_text(config_text(3600, f"urn:ngsi-ld:ServiceExecution:{name}"))
    if log is not None:
        (d / "output.log").write_text("".join(log))
    if exit_code is not None:
        (d / "exit_code").write_text(f"{exit_code}\n")
    if reported:
        (d / REPORTED_MARKER).touch()


def test_scan_local_lists_unreported_runs_only(tmp_path):
    _run_folder(tmp_path, "done", log=LOG, exit_code=0)
    _run_folder(tmp_path, "waiting")
    _run_folder(tmp_path, "handled", log=LOG, exit_code=0, reported=True)
    _run_folder(tmp_path, "not-a-run", is_run=False)

    entries = {e["folder"]: e for e in scan_local(tmp_path)}
    assert set(entries) == {"done", "waiting"}
    assert (entries["done"]["action"], entries["done"]["detail"]["state"]) == ("publish", "completed")
    assert entries["done"]["serviceExecutionId"] == "urn:ngsi-ld:ServiceExecution:done"
    assert entries["waiting"]["action"] == "update"

    mark_reported({}, "done", tmp_path)
    assert [e["folder"] for e in scan_local(tmp_path)] == ["waiting"]


# --- remote scan script, run by a local shell standing in for the VM -------------

def _vm(monkeypatch, tmp_path, login_shell="sh", **settings):
    """Run scan_remote() locally: `login_shell -c <remote command>`, like sshd."""
    if not shutil.which(login_shell):
        pytest.skip(f"{login_shell} not installed")
    monkeypatch.setattr("caflood_common.ssh_argv", lambda s, cmd: [login_shell, "-c", cmd])
    return {"runs_dir": (tmp_path / "runs").as_posix(), **settings}


@pytest.mark.parametrize("login_shell", ["sh", "zsh"])
def test_scan_remote_empty_or_missing_runs_dir(monkeypatch, tmp_path, login_shell):
    # zsh: a */ matching nothing is a fatal "no matches found" (#8246).
    settings = _vm(monkeypatch, tmp_path, login_shell)
    assert scan_remote(settings) == []  # missing -> created
    assert (tmp_path / "runs").is_dir()
    assert scan_remote(settings) == []  # empty


def test_scan_remote_cleans_up_reported_runs(monkeypatch, tmp_path):
    settings = _vm(monkeypatch, tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    _run_folder(runs, "running", log=LOG[:CUTS["mid_simulation"]])
    _run_folder(runs, "fresh", log=LOG, exit_code=0, reported=True)
    (runs / "fresh" / "output_dir").mkdir()
    (runs / "fresh" / "output_dir" / "depth.asc").write_text("x")
    _run_folder(runs, "old", log=LOG, exit_code=0, reported=True)
    eight_days_ago = time.time() - 8 * 86400
    os.utime(runs / "old" / REPORTED_MARKER, (eight_days_ago, eight_days_ago))
    _run_folder(runs, "not-a-run", is_run=False)
    (runs / "not-a-run" / "keep.txt").write_text("x")

    assert [e["folder"] for e in scan_remote(settings)] == ["running"]
    assert sorted(p.name for p in (runs / "fresh").iterdir()) == ["output.log", REPORTED_MARKER]
    assert not (runs / "old").exists()
    assert (runs / "not-a-run" / "keep.txt").exists()
    assert (runs / "running" / "config.csv").exists() and (runs / "running" / "output.log").exists()
    # Stripped folders stay out of the listing until they expire.
    assert [e["folder"] for e in scan_remote(settings)] == ["running"]


@pytest.mark.parametrize("days", ["-1", "a week"])
def test_scan_remote_refuses_bad_retention(tmp_path, days):
    with pytest.raises(ValueError, match="CAFLOOD_LOG_RETENTION_DAYS"):
        scan_remote({"runs_dir": str(tmp_path), "log_retention_days": days})


# --- poll entries: action + broker update body ----------------------------------

def entry(**kwargs):
    return poll_entry("run", parse_bundle(status_bundle(**kwargs)))


def test_entry_running_is_an_executing_update():
    e = entry(alive="yes", log_lines=LOG[:CUTS["mid_simulation"]])
    assert e["action"] == "update"
    assert e["update"] == {"executionStatus": "executing", "progress": pytest.approx(0.475, abs=1e-3)}
    assert e["detail"]["phase"] == "simulation"


def test_entry_pending_is_executing_at_zero():
    e = entry(alive="nopid", log_lines=[])
    assert e["update"] == {"executionStatus": "executing", "progress": 0.0}


def test_entry_completed_asks_for_publishing():
    e = entry(exit_code="0", alive="no", log_lines=LOG)
    assert e["action"] == "publish" and "update" not in e


def test_entry_completed_being_published_waits():
    e = entry(exit_code="0", alive="no", log_lines=LOG, claim_age=str(CLAIM_TIMEOUT_S - 60))
    assert e["action"] == "wait" and "update" not in e


def test_entry_stale_claim_is_published_again():
    # the publisher crashed without releasing its claim
    e = entry(exit_code="0", alive="no", log_lines=LOG, claim_age=str(CLAIM_TIMEOUT_S + 60))
    assert e["action"] == "publish"


def test_entry_saved_publish_result_is_resent_without_publishing():
    saved = {"executionStatus": "success", "progress": 1, "output": {"manifestUrl": "https://store/b/k"}}
    e = entry(exit_code="0", alive="no", log_lines=LOG, publish_result=json.dumps(saved), claim_age="5")
    assert (e["action"], e["update"]) == ("finalize", saved)


def test_entry_unreadable_publish_result_means_publish_again():
    e = entry(exit_code="0", alive="no", log_lines=LOG, publish_result="{not json")
    assert e["action"] == "publish"


def test_read_dir_reports_claim_age_and_saved_result(tmp_path):
    _run_folder(tmp_path, "run", log=LOG, exit_code=0)
    (tmp_path / "run" / CLAIM_FILE).touch()
    (tmp_path / "run" / RESULT_FILE).write_text('{"executionStatus": "success", "progress": 1}')
    parts = read_dir(tmp_path / "run")
    assert 0 <= int(parts["claim_age"]) < 60
    assert poll_entry("run", parts)["action"] == "finalize"


def test_entry_failed_is_a_failure_update():
    e = entry(exit_code="1", alive="no", log_lines=["Error reading 'Max DT' element\n"])
    assert e["action"] == "finalize"
    assert e["update"] == {"executionStatus": "failure", "progress": 0.0,
                           "output": {"error": "Error reading 'Max DT' element", "exitCode": 1}}


def test_entry_running_with_warnings_has_output():
    # no pid file: state inferred from the log, which is reported as a warning
    e = entry(alive="nopid", log_lines=LOG[:CUTS["mid_simulation"]])
    assert e["update"]["executionStatus"] == "executing"
    assert e["update"]["output"]["warnings"] == ["No pid file: state inferred from output.log only"]


@pytest.mark.parametrize("kwargs", [
    dict(alive="yes", log_lines=LOG[:CUTS["preprocessing"]]),
    dict(exit_code="0", alive="no", log_lines=LOG),
    dict(alive="no", log_lines=LOG[:CUTS["mid_simulation"]]),
])
def test_no_null_anywhere(kwargs):
    assert "null" not in json.dumps(entry(**kwargs))


@pytest.mark.parametrize("name", ["../etc", "a/b", "a b", "$(rm -rf x)", ".."])
def test_mark_reported_refuses_unsafe_names(tmp_path, name):
    with pytest.raises(ValueError):
        mark_reported({}, name, tmp_path)
