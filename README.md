# CAFLOOD service scripts

NiFi-side scripts that run CAFLOOD flood simulations from Stellio
`ServiceExecution`s: build the input job, track the run on the CAFLOOD VM,
and publish the results to S3. Full details: [PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md).

## Pipeline

| Step | Script | NiFi trigger | Output |
| --- | --- | --- | --- |
| 1. Build | `caflood_build.py` | Stellio notification (stdin) | job zip on stdout, then SFTP, unzip, launch |
| 2. Track | `caflood_poll.py` | timer, every minute | per run: `action` + broker `update` body |
| 3. Publish | `caflood_publish.py <folder>` | action `publish` | COGs + manifest in S3; `update` body (success, or failure after 1 h of failures) |
| - | `caflood_poll.py --mark-reported <folder>` | after the final update | run no longer polled |

Libraries: `caflood_common.py` (config, clients, SSH), `caflood_inputs.py`,
`caflood_setup_files.py`, `caflood_status.py`. Dev checks:
`check_file_storage.py`, `check_inputs.py`.

## Setup

Needs Python 3 and the `ssh` client (`openssh-client`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in the Stellio credentials (also used for file storage)
```

The CAFLOOD launch command must write `pid` and `exit_code` into the run
folder; the exact command is in [PIPELINE_REFERENCE.md §4](PIPELINE_REFERENCE.md#4-launch-on-the-caflood-vm).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests          # offline, a few seconds
```

## Limitations

The relative timeframe (profiles) works end to end. The absolute timeframe
(ControlPoint / WeatherInformation) is not implemented yet. See
[PIPELINE_REFERENCE.md §10](PIPELINE_REFERENCE.md#10-known-gaps-and-temporary-choices).
