#!/usr/bin/env python3
"""Run observation processing across bounded PBS allocations."""

import argparse
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

from config_utils import load_config
from cycle_simulations import lock, read_json, seconds, submission_path, write_json


def stop_processing(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        pass
    # Joblib children may outlive their parent after a signal or operator error.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def validate(config):
    if type(config.get("EnableSensitivityCycling", False)) is not bool:
        raise ValueError("EnableSensitivityCycling must be a YAML boolean")
    if not config.get("EnableSensitivityCycling", False):
        return
    if (config["SchedulerType"] != "PBS" or config["UseGCHP"]
            or config["Species"] != "CH4" or config["KalmanMode"]
            or config.get("EnableOSSE", False)):
        raise ValueError("Sensitivity cycling requires PBS, GCClassic CH4, and non-Kalman/non-OSSE mode")
    budget = seconds(config.get("SensitivityCycleTime", "05:30:00"))
    walltime = seconds(config.get("InversionTime", config["RequestedTime"]))
    if budget <= 0 or walltime - budget < 300:
        raise ValueError("SensitivityCycleTime must be positive and at least five minutes below InversionTime")


def progress(state_path, log):
    state = read_json(state_path) if state_path.exists() else dict(done={}, files=[])
    text = f"Sensitivities   {len(state['done']):>6}/{len(state['files']):<6} observation files complete"
    print(text, flush=True)
    with log.open("a") as stream:
        stream.write(text + "\n")


def worker(manifest):
    path = Path(manifest)
    job = read_json(path)
    with lock(Path(job["state"] + ".worker.lock")):
        if "result" in job:
            raise ValueError("Sensitivity submission has already finished")
        run_worker(path, job)


def run_worker(path, job):
    state_path = Path(job["state"])
    process = None
    try:
        started = float(os.environ["IMI_JOB_START_TIME"])
        remaining = started + job["seconds"] - time.time()
        if not 0 < remaining <= job["seconds"]:
            raise ValueError("Sensitivity cycle budget exhausted during setup or invalid start time")
        env = os.environ.copy()
        env["IMI_SENSITIVITY_STATE"] = str(state_path)
        env["IMI_SENSITIVITY_ATTEMPT"] = str(path)
        process = subprocess.Popen(job["command"], env=env, start_new_session=True)
        expired = False
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            expired = True
            stop_processing(process)
            code = process.returncode
        if code and not expired:
            raise ValueError(f"Observation processing exited {code}")
        state = read_json(state_path)
        if state.get("attempt") != str(path):
            raise ValueError("Observation processing did not finish initializing this cycle")
        done, total = len(state["done"]), len(state["files"])
        if not expired and done != total:
            raise ValueError("Observation processing exited without completing all files")
        if expired and done <= state["cycle_start_completed"]:
            raise ValueError("No observation-file progress; increase cycle time or reduce per-file cost")
        job["result"] = "ready" if expired else "complete"
        write_json(path, job)
    except BaseException:
        if process is not None:
            stop_processing(process)
        raise


def controller(config_path, root, stage, command):
    config = load_config(config_path)
    validate(config)
    root = Path(root).absolute()
    run_root = (root / config["OutputPath"]).absolute() / config["RunName"]
    directory = run_root / "scheduler" / stage
    directory.mkdir(parents=True, exist_ok=True)
    state_path = Path.cwd() / f"{stage}-progress.json"
    log = root / "imi_output.log"
    with lock(directory / "controller.lock"):
        # A previous hard kill must be reconciled with PBS before retrying.
        for previous in directory.glob("cycle-*.json"):
            if "result" not in read_json(previous):
                raise ValueError(f"Unfinished submission: reconcile PBS job before retrying {previous}")
        while True:
            manifest = submission_path(directory)
            write_json(manifest, dict(command=command, state=str(state_path),
                                     seconds=seconds(config.get("SensitivityCycleTime", "05:30:00"))))
            variables = dict(InversionPath=str(root), RunDirs=str(run_root),
                             ConfigPath=str(Path(config_path).absolute()),
                             IMICycling="true", IMISchedulerStage=stage, UseGCHP="false")
            for key in ("PBSAccount", "PBSModel", "PBSSiteNeeded"):
                variables[key] = config.get(key, "")
            script = "source " + shlex.quote(str(root / "src/utilities/common.sh")) + "\n"
            script += "\n".join(key + "=" + shlex.quote(value) for key, value in variables.items()) + "\n"
            args = ["", config["PythonEnv"], "PBS", "true",
                    str(config.get("InversionMemory", config["RequestedMemory"])),
                    str(config.get("InversionCPUs", config["RequestedCPUs"])),
                    config.get("InversionTime", config["RequestedTime"]),
                    config.get("InvSchedulerPartition", config["SchedulerPartition"]),
                    str(Path(__file__).absolute()), "worker", str(manifest)]
            script += "_imi_submit " + shlex.join(args)
            submission_log = manifest.with_suffix(".log")
            with submission_log.open("w") as output:
                process = subprocess.Popen(["bash", "-c", script], stdout=output, stderr=subprocess.STDOUT)
                try:
                    while True:
                        try:
                            code = process.wait(timeout=600)
                            break
                        except subprocess.TimeoutExpired:
                            progress(state_path, log)
                except BaseException:
                    process.terminate()
                    process.wait()
                    raise
            text = submission_log.read_text()
            print(text, end="", flush=True)
            with log.open("a") as stream:
                stream.write(text)
            progress(state_path, log)
            result = read_json(manifest).get("result")
            if code or result not in ("ready", "complete"):
                raise ValueError(f"Sensitivity submission failed; see {submission_log}")
            if result == "complete":
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "worker", "controller"))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise InterruptedError(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    if args.mode == "validate":
        validate(load_config(args.args[0]))
    elif args.mode == "worker":
        worker(args.args[0])
    else:
        controller(*args.args[:3], args.args[3:])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Sensitivity cycling: {exc}", file=sys.stderr)
        sys.exit(1)
