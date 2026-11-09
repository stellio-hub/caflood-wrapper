#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EGM
"""
CAFLOOD run status: what state a run folder on the CAFLOOD VM is in, as a
dict / JSON object, e.g.

  {"state": "running", "phase": "simulation", "progress": 0.42, "code": null,
   "exitCode": null, "error": null, ...}

Library used by caflood_poll.py (which fetches every run folder in one SSH
call and calls determine_status() on each). Can also be run by hand on a
local copy of a run folder, for debugging:

  python caflood_status.py <run folder>

Launch contract (set by the NiFi launch command): the run folder holds
  pid        PID of the `sh -c` wrapper around cafloodpro, alive while it runs
  exit_code  written by that wrapper once cafloodpro exits (0 = success)
  config.csv the setup file (Time End / Update DT give the run length)
  output.log cafloodpro stdout + stderr

Decision table ("state"):
  nothing launched yet      -> pending (no pid, no exit_code, no log)
  exit_code present, 0      -> completed
  exit_code present, != 0   -> failed (error = CAFLOOD's error lines, or the
                               signal if it was killed)
  no exit_code, pid alive   -> running (phase + progress from output.log)
  no exit_code, pid dead    -> failed (died without finishing: killed, VM
                               reboot...)
  no pid file at all        -> launched before the pid/exit_code contract:
                               inferred from output.log only (cannot detect
                               a crash)

Progress: the main loop logs "Simulation time (MIN) = X" every output
period, so simulation progress = X*60 / Time End. Post-processing then
writes every snapshot in time order ("Write Raster Grid: <path>_<t>"), so
its progress = last t / Time End. Overall progress weights them
SIMULATION_WEIGHT / 1 - SIMULATION_WEIGHT. "phase" is preprocessing /
simulation / postprocessing / done; "code" is CAFLOOD's quality flag
(0 / 2 / 4).

Inputs (parse_bundle / read_dir): the "exit_code", "alive" (yes / no /
nopid), "config", "code", "claim_age" (seconds since the publishing claim,
or empty), "publish_result" (saved final update, or empty) and "log" parts
of a run folder. "log" is only the
last LOG_TAIL_BYTES of output.log; "code" is the "CODE:" line grepped from
the whole log, since post-processing output can push it out of the tail.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

LOG_TAIL_BYTES = 65536

# Publishing bookkeeping in the run folder (written by caflood_publish.py):
#   publishing             claim while a publish is in progress
#   publish_result.json    final ServiceExecution update (success, or
#                          failure after giving up), kept until reported
#   publish_failures.json  {"first": epoch, "attempts": n} of failed attempts
CLAIM_FILE = "publishing"
RESULT_FILE = "publish_result.json"
FAILURES_FILE = "publish_failures.json"
CLAIM_TIMEOUT_S = 3600  # a claim older than this is a crashed publisher

SIMULATION_WEIGHT = 0.95

_SIM_TIME = re.compile(r"Simulation time \(MIN\) = ([0-9.eE+-]+)")
# Post-processing snapshot lines, e.g.
#   Write Raster Grid: output_dir/<short>_raster_WD_124500
#   Write Raster Grid: output_dir/<short>_raster_VEL_V_124500
#   Write Raster Grid: output_dir/<short>_raster_VEL.csv_124500.csv
# (PEAK / TemporalOut_TIME lines don't end in a time, so don't match.)
_POST_WRITE = re.compile(r"^Write Raster Grid: .*_(\d+(?:\.\d+)?)(?:\.csv)?\s*$")
_QUALITY_CODE = re.compile(r"^CODE:\s*(\d+)")
# What CAFLOOD prints (on stderr) before returning EXIT_FAILURE - see
# main.cpp / ArgsData.hpp READ_TOKEN and the per-file CSV parsers.
_ERROR_LINE = re.compile(
    r"^(Error\b|Element '|Missing |Too many values|The index 0 |"
    r"Spatial Temporal .* must|Spatial Height Adjustment must|"
    r"Simulation stopped|ATTENTION!|Use the -help)"
)

# 128 + signal number, as reported by the `sh -c` wrapper.
_SIGNALS = {
    130: "interrupted (SIGINT)",
    134: "aborted (SIGABRT)",
    137: "killed (SIGKILL - out of memory, or killed manually)",
    139: "crashed (SIGSEGV - segmentation fault)",
    143: "terminated (SIGTERM)",
}


class StatusInputError(Exception):
    pass


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


_SECTIONS = ("exit_code", "alive", "config", "code", "claim_age", "publish_result", "log")


def parse_bundle(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("@@") and line[2:].strip() in _SECTIONS:
            current = line[2:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    missing = [s for s in ("exit_code", "alive", "config", "log") if s not in sections]
    if missing:
        raise StatusInputError(
            f"status input is missing section(s) {missing} - run folder not found?"
        )
    parts = {name: "\n".join(lines) for name, lines in sections.items()}
    for optional in ("code", "claim_age", "publish_result"):
        parts.setdefault(optional, "")
    return parts


def pid_alive(pid: int) -> bool:
    """Equivalent of `kill -0 <pid>`. Not os.kill(pid, 0) on Windows: there
    it TERMINATES the process instead of probing it."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def read_dir(run_dir: Path) -> dict[str, str]:
    """Local equivalent of caflood_poll.py's remote scan, for one run folder."""
    if not run_dir.is_dir():
        raise StatusInputError(f"run folder not found: {run_dir}")

    def read(name: str) -> str:
        path = run_dir / name
        return path.read_text(errors="replace") if path.is_file() else ""

    pid_file = run_dir / "pid"
    if not pid_file.is_file():
        alive = "nopid"
    else:
        try:
            alive = "yes" if pid_alive(int(pid_file.read_text().strip())) else "no"
        except ValueError:
            alive = "no"

    log_path = run_dir / "output.log"
    log = ""
    if log_path.is_file():
        with log_path.open("rb") as fh:
            fh.seek(max(log_path.stat().st_size - LOG_TAIL_BYTES, 0))
            log = fh.read().decode(errors="replace")
    code = ""
    if log_path.is_file():
        with log_path.open(errors="replace") as fh:
            code = "\n".join(l.strip() for l in fh if l.startswith("CODE:"))
    claim = run_dir / CLAIM_FILE
    claim_age = str(int(time.time() - claim.stat().st_mtime)) if claim.is_file() else ""
    return {
        "exit_code": read("exit_code"),
        "alive": alive,
        "config": read("config.csv"),
        "code": code,
        "claim_age": claim_age,
        "publish_result": read(RESULT_FILE),
        "log": log,
    }


def config_value(config_text: str, keyword: str) -> str | None:
    """First value of a config.csv row, matched by keyword prefix like
    CAFLOOD does ("Time End" matches "Time End (seconds)")."""
    for line in config_text.splitlines():
        key, _, rest = line.partition(",")
        if key.strip().lower().startswith(keyword.lower()):
            value = rest.split(",")[0].strip()
            return value or None
    return None


def parse_time_end(config_text: str) -> float | None:
    """Time End as CAFLOOD actually runs it: rounded UP to a multiple of
    Update DT, itself capped at 60 s (DOCUMENTATION.md 5.2)."""
    try:
        time_end = float(config_value(config_text, "Time End") or "")
    except ValueError:
        return None
    try:
        update_dt = min(float(config_value(config_text, "Update DT") or 60.0), 60.0)
    except ValueError:
        update_dt = 60.0
    if update_dt > 0:
        time_end = math.ceil(time_end / update_dt) * update_dt
    return time_end


def service_execution_id(folder: str, config_text: str) -> str:
    """caflood_build.py writes the full ServiceExecution id as "Simulation
    Name" in config.csv; fall back on the folder name."""
    name = config_value(config_text, "Simulation Name")
    if name:
        return name
    return folder if folder.startswith("urn:") else f"urn:ngsi-ld:ServiceExecution:{folder}"


# --------------------------------------------------------------------------
# Log analysis
# --------------------------------------------------------------------------


def analyse_log(log: str, time_end: float | None) -> dict[str, Any]:
    """Phase / progress / quality code / error lines from the log tail."""
    phase = None
    sim_time_s = None
    post_time_s = None
    finished = False
    code = None
    errors: list[str] = []
    previous = ""

    for raw in log.splitlines():
        line = raw.strip()
        if not line:
            continue
        # main.cpp's catch-all prints e.what() (any text) then "Simulation
        # stopped" - keep the exception text too.
        if line.startswith("Simulation stopped") and previous and previous not in errors:
            errors.append(previous)
        previous = line
        if line.startswith("Pre-processing :"):
            phase = "preprocessing"
        elif line.startswith("Start main loop"):
            phase = "simulation"
        elif (m := _SIM_TIME.search(line)):
            phase = "simulation"
            sim_time_s = float(m.group(1)) * 60
        elif (m := _QUALITY_CODE.match(line)):
            code = int(m.group(1))
            sim_time_s = time_end  # main loop done
        elif line.startswith("Post-processing :"):
            phase = "postprocessing"
            sim_time_s = time_end
        elif (m := _POST_WRITE.match(line)):
            phase = "postprocessing"
            post_time_s = float(m.group(1))
        elif line.startswith("Cleaned data"):
            finished = True
        if _ERROR_LINE.match(line) and line not in errors:
            errors.append(line)

    def fraction(t: float | None) -> float | None:
        if t is None or not time_end:
            return None
        return max(0.0, min(t / time_end, 1.0))

    sim_fraction = fraction(sim_time_s)
    post_fraction = fraction(post_time_s)
    if phase == "postprocessing":
        progress = SIMULATION_WEIGHT + (1 - SIMULATION_WEIGHT) * (post_fraction or 0.0)
        sim_fraction = 1.0
    elif phase == "simulation":
        progress = SIMULATION_WEIGHT * (sim_fraction or 0.0)
    else:
        progress = 0.0

    tail = [l for l in (raw.strip() for raw in log.splitlines()) if l][-5:]
    return {
        "phase": phase,
        "progress": round(progress, 4),
        "simulationProgress": None if sim_fraction is None else round(sim_fraction, 4),
        "simulationTimeS": sim_time_s,
        "finished": finished,
        "code": code,
        "errors": errors,
        "tail": tail,
    }


def determine_status(parts: dict[str, str]) -> dict[str, Any]:
    time_end = parse_time_end(parts["config"])
    info = analyse_log(parts["log"], time_end)
    if info["code"] is None and (m := _QUALITY_CODE.match(parts.get("code", "").strip())):
        info["code"] = int(m.group(1))
    alive = parts["alive"].strip()
    exit_text = parts["exit_code"].strip()
    exit_code = int(exit_text) if re.fullmatch(r"-?\d+", exit_text) else None

    status: dict[str, Any] = {
        "state": None,
        "phase": info["phase"],
        "progress": info["progress"],
        "simulationProgress": info["simulationProgress"],
        "simulationTimeS": info["simulationTimeS"],
        "timeEndS": time_end,
        "code": info["code"],
        "exitCode": exit_code,
        "error": None,
        "warnings": [],
    }
    if time_end is None:
        status["warnings"].append("No 'Time End' in config.csv: progress unavailable")

    def fail(reason: str | None) -> None:
        status["state"] = "failed"
        # CAFLOOD's own error lines when there are some; otherwise the
        # reason we know, plus the last log lines for context.
        details = info["errors"] or ([] if reason else info["tail"])
        status["error"] = " | ".join(([reason] if reason else []) + details) or None

    if exit_code is not None:
        if exit_code == 0:
            status.update(state="completed", phase="done", progress=1.0, simulationProgress=1.0, simulationTimeS=time_end)
            if info["code"] is None:
                status["warnings"].append("Run completed but no quality 'CODE:' found in the log tail")
        else:
            reason = _SIGNALS.get(exit_code)
            fail(f"cafloodpro {reason}" if reason else (None if info["errors"] else f"cafloodpro exited with code {exit_code}"))
    elif exit_text:
        status["warnings"].append(f"Unreadable exit_code content: {exit_text!r}")
        fail("exit_code file present but unreadable")
    elif alive == "yes":
        status["state"] = "running"
    elif alive == "no":
        fail("cafloodpro is no longer running but did not write an exit code (killed, or VM rebooted?)")
    elif not parts["log"].strip():
        # Job folder unzipped but cafloodpro not launched (yet).
        status["state"] = "pending"
    else:  # "nopid": launched without the pid/exit_code wrapper
        status["warnings"].append("No pid file: state inferred from output.log only")
        if info["finished"]:
            status.update(state="completed", phase="done", progress=1.0, simulationProgress=1.0, simulationTimeS=time_end)
        elif info["errors"]:
            fail(None)
        else:
            status["state"] = "running"
    return status


# --------------------------------------------------------------------------
# ServiceExecution update bodies (what NiFi PATCHes to the broker)
# --------------------------------------------------------------------------
# Only three keys: executionStatus (cancelled / failure / success / pending /
# executing), progress (0..1) and output (free object). The broker refuses
# null values, so empty / null keys are dropped (compact).

QUALITY_CODE_WARNINGS = {
    1: "CAFLOOD quality code 1: water on NODATA cells of the DEM - results are compromised",
    2: "CAFLOOD quality code 2: the flood touches NODATA cells of the DEM - results next to them are unreliable",
    4: "CAFLOOD quality code 4: the flood reaches the edge of the domain - results near the edges are unreliable",
}


def compact(values: dict[str, Any]) -> dict[str, Any]:
    """Drop None / empty values (the broker refuses nulls)."""
    return {k: v for k, v in values.items() if v not in (None, "", [], {})}


def executing_update(progress: float, warnings: list[str] | None = None) -> dict[str, Any]:
    return compact({
        "executionStatus": "executing",
        "progress": round(max(0.0, min(progress, 1.0)), 4),
        "output": compact({"warnings": warnings}),
    })


def failure_update(progress: float, error: str, *, exit_code: int | None = None,
                   warnings: list[str] | None = None) -> dict[str, Any]:
    return compact({
        "executionStatus": "failure",
        "progress": round(max(0.0, min(progress, 1.0)), 4),
        "output": compact({"error": error, "exitCode": exit_code, "warnings": warnings}),
    })


def success_update(output: dict[str, Any]) -> dict[str, Any]:
    return {"executionStatus": "success", "progress": 1, "output": compact(output)}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path, help="Local run folder (config.csv, output.log, pid, exit_code)")
    args = parser.parse_args(argv)

    try:
        parts = read_dir(args.run_dir)
    except StatusInputError as exc:
        print(f"caflood_status: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(determine_status(parts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
