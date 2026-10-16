#!/usr/bin/env python3
"""Cycle GCClassic methane simulations through scheduler allocations."""

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import numpy as np
import xarray as xr
import yaml

from config_utils import load_config

FILES = ("geoschem_config.yml", "HEMCO_Config.rc", "HISTORY.rc")


def seconds(value):
    if not re.fullmatch(r"\d+:[0-5]\d:[0-5]\d", str(value)):
        raise ValueError("Cycling times must use HHH:MM:SS")
    h, m, s = map(int, value.split(":"))
    return h * 3600 + m * 60 + s


def validate(config):
    if type(config.get("EnableSimulationCycling", False)) is not bool:
        raise ValueError("EnableSimulationCycling must be a YAML boolean")
    if not config.get("EnableSimulationCycling", False):
        return
    if (config["SchedulerType"] != "PBS" or config["UseGCHP"]
            or config["Species"] != "CH4" or config["KalmanMode"]
            or config.get("EnableOSSE", False) or not config["HourlySpecies"]):
        raise ValueError("Simulation cycling requires PBS, GCClassic CH4, HourlySpecies, and non-Kalman/non-OSSE mode")
    budget = seconds(config.get("SimulationCycleTime", "05:30:00"))
    if budget <= 0 or seconds(config["RequestedTime"]) - budget < 300:
        raise ValueError("SimulationCycleTime must be positive and at least five minutes below RequestedTime")


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


@contextlib.contextmanager
def lock(path):
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def date(value):
    return dt.datetime.strptime(str(value), "%Y%m%d")


def stamp(value):
    return value.strftime("%Y%m%d")


def replace_one(text, pattern, replacement):
    text, count = re.subn(pattern, replacement, text, flags=re.M)
    if count != 1:
        raise ValueError(f"Expected one setting: {pattern}")
    return text


def history(text):
    head, tail = text.split("::", 1)
    head = re.sub(r"#\s*'Restart'", "'Restart'", head)
    active = re.findall(r"'([A-Za-z0-9_]+)'", "\n".join(x.split("#")[0] for x in head.splitlines()))
    if "Restart" not in active or "SpeciesConc" not in active:
        raise ValueError("HISTORY must contain Restart and SpeciesConc collections")
    text = head + "::" + tail
    for key in ("frequency", "duration"):
        text = replace_one(text, rf"^(\s*Restart\.{key}:).*", r"\g<1> 00000001 000000")
    if not re.search(r"Restart.mode:\s*'instantaneous'", text) or "SpeciesRst_?ALL?" not in text:
        raise ValueError("Restart must save instantaneous SpeciesRst_?ALL?")
    for field in ("Met_DELPDRY", "Met_BXHEIGHT", "Met_TropLev"):
        if field not in text:
            raise ValueError(f"Missing restart field {field}")
    if not re.search(r"Restart.filename:\s*'./Restarts/GEOSChem.Restart.%y4%m2%d2_%h2%n2z.nc4'", text):
        raise ValueError("Cycling requires the standard Restart filename")
    for collection in active:
        if collection != "Restart":
            text = replace_one(text, rf"^(\s*{collection}\.duration:).*", r"\g<1> 00000001 000000")
            text = re.sub(rf"^(\s*{collection}\.frequency:)\s*00000100 000000.*", r"\g<1> 00000001 000000", text, flags=re.M)
    for collection in ("SpeciesConc", "StateMetLevEdge"):
        if collection in active:
            if not re.search(rf"{collection}\.frequency:\s*00000000 010000", text):
                raise ValueError(f"Cycling requires hourly {collection} diagnostics")
    return text, active


def prepare(run, queued=False):
    state_dir = run / ".imi_cycles"
    state_dir.mkdir(exist_ok=True)
    state_file = state_dir / "state.json"
    if state_file.exists():
        state = read_json(state_file)
        if state["status"] not in (("queued",) if queued else ("ready", "complete")):
            raise ValueError(f"{run.name}: previous cycle is {state['status']}; inspect before retrying")
        for name, content in state["settings"].items():
            if (run / name).read_text() != content:
                raise ValueError(f"{run.name}: {name} changed since the recorded cycle")
        return state
    if any((run / "OutputDir").glob("GEOSChem.SpeciesConc.*.nc4")):
        raise ValueError(f"{run.name}: existing output has no cycling state; use a fresh cycling run")
    gc = yaml.safe_load((run / FILES[0]).read_text())
    start, end = gc["simulation"]["start_date"], gc["simulation"]["end_date"]
    if start[1] != 0 or end[1] != 0 or date(end[0]) <= date(start[0]):
        raise ValueError("Cycling requires a positive interval with midnight endpoints")
    species = gc["operations"]["transport"]["transported_species"]
    if not species or any(not re.fullmatch(r"CH4(?:_\d+)?", x) for x in species):
        raise ValueError("Cycling supports CH4 and tagged CH4 transported species only")
    original = state_dir / "original"
    original.mkdir()
    for name in FILES:
        shutil.copy2(run / name, original / name)
    hist, active = history((run / "HISTORY.rc").read_text())
    (run / "HISTORY.rc").write_text(hist)
    hemco = (run / "HEMCO_Config.rc").read_text()
    hemco = replace_one(hemco, r"^(DiagnFreq:)\s*.*", r"\g<1> Daily")
    (run / "HEMCO_Config.rc").write_text(hemco)
    state = dict(start=stamp(date(start[0])), end=stamp(date(end[0])), next=stamp(date(start[0])),
                 species=species, collections=active, status="ready", cycle=0)
    save_state(run, state)
    return state


def save_state(run, state):
    state["settings"] = {name: (run / name).read_text() for name in FILES}
    write_json(run / ".imi_cycles/state.json", state)


def check_restart(path, when, species):
    with xr.open_dataset(path) as ds:
        if ds.sizes.get("time") != 1 or np.datetime64(when) != ds.time.values[0]:
            raise ValueError("Restart timestamp mismatch")
        for name in ["SpeciesRst_" + x for x in species] + ["Met_DELPDRY", "Met_BXHEIGHT", "Met_TropLev"]:
            values = ds[name].values
            if not values.size or not np.isfinite(values).all():
                raise ValueError(f"Incomplete restart field {name}")


def check_days(run, state, end):
    day = date(state["next"])
    while day < end:
        for collection in ("SpeciesConc", "StateMetLevEdge"):
            if collection not in state["collections"]:
                continue
            path = run / "OutputDir" / f"GEOSChem.{collection}.{stamp(day)}_0000z.nc4"
            with xr.open_dataset(path) as ds:
                ts = ds.time.values.astype("datetime64[s]")
                if len(ts) != 24 or len(np.unique(ts)) != 24 or not np.all(np.diff(ts) == np.timedelta64(1, "h")):
                    raise ValueError(f"Incomplete hourly diagnostics: {path}")
                if ts[0] < np.datetime64(day) or ts[-1] > np.datetime64(day + dt.timedelta(days=1)):
                    raise ValueError(f"Wrong diagnostic timestamps: {path}")
                names = ["SpeciesConcVV_" + x for x in state["species"]] if collection == "SpeciesConc" else list(ds.data_vars)
                for name in names:
                    if not np.isfinite(ds[name].values).all():
                        raise ValueError(f"Incomplete diagnostic field: {path}: {name}")
        day += dt.timedelta(days=1)


def checkpoint(run, state):
    rejected = ""
    for path in sorted((run / "Restarts").glob("GEOSChem.Restart.*_0000z.nc4"), reverse=True):
        match = re.fullmatch(r"GEOSChem.Restart.(\d{8})_0000z.nc4", path.name)
        if not match or path.is_symlink():
            continue
        when = date(match[1])
        if not date(state["next"]) < when <= date(state["end"]):
            continue
        try:
            check_restart(path, when, state["species"])
            check_days(run, state, when)
        except (ValueError, KeyError, OSError, RuntimeError) as exc:
            rejected = f" {path.name}: {exc}"
            continue
        return when, path
    raise ValueError(f"{run.name}: no valid advancing daily restart; check cycle duration and diagnostics.{rejected}")


def resume(run, when, path, archive):
    gc = (run / "geoschem_config.yml").read_text()
    gc = replace_one(gc, r"^(\s*start_date:)\s*\[.*?\].*", rf"\g<1> [{stamp(when)}, 000000]")
    hemco = (run / "HEMCO_Config.rc").read_text()
    a = hemco.index("(((GC_RESTART")
    b = hemco.index(")))GC_RESTART", a)
    block = hemco[a:b]
    lines = block.splitlines()
    indices = [i for i, line in enumerate(lines) if re.match(r"\s*\*\s+SPC_", line)]
    if not indices:
        raise ValueError("No HEMCO species restart entries")
    restart = "./Restarts/" + path.name
    replacement = f"* SPC_ {restart} SpeciesRst_?ALL? $YYYY/$MM/$DD/$HH EFYO xyz 1 * - 1 1"
    lines = [replacement if i == indices[0] else line for i, line in enumerate(lines) if i not in indices[1:]]
    block = "\n".join(lines) + "\n"
    block = re.sub(r"(?m)^(\s*\*\s+DELPDRY\s+)\S+", lambda m: m[1] + restart, block)
    hemco = hemco[:a] + block + hemco[b:]
    overlap = archive / "overlap"
    overlap.mkdir()
    for folder in (run / "OutputDir", run / "Restarts"):
        for item in folder.iterdir():
            match = re.search(r"(?:^|[._-])(\d{8})(?:\d{4})?(?=[._-]|$)", item.name)
            if match and item != path and date(match[1]) >= when and item.is_file():
                target = overlap / folder.name
                target.mkdir(exist_ok=True)
                item.rename(target / item.name)
    (run / "geoschem_config.yml").write_text(gc)
    (run / "HEMCO_Config.rc").write_text(hemco)


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def worker(manifest):
    job = read_json(Path(manifest))
    started = float(os.environ.get("IMI_JOB_START_TIME", time.time()))
    if not np.isfinite(started) or started > time.time():
        raise ValueError("Invalid batch wrapper start time")
    if "PBS_JOBID" in os.environ and "IMI_JOB_START_TIME" not in os.environ:
        raise ValueError("Batch wrapper did not record its start time")
    deadline = started + job["seconds"]
    run = Path(job["runs"][int(os.environ["IMI_ARRAY_TASK_ID"])])
    with lock(run / ".imi_cycles/worker.lock"):
        state = prepare(run, queued=True)
        if state.get("submission") != manifest:
            raise ValueError("Worker submission does not match cycling state")
        if state["status"] == "complete":
            return
        state["cycle"] += 1
        archive = run / ".imi_cycles" / f"cycle{state['cycle']:05d}"
        archive.mkdir()
        for name in FILES:
            shutil.copy2(run / name, archive / name)
        state.update(status="running", job_id=os.environ.get("PBS_JOBID", ""),
                     wrapper_started=started, deadline=deadline)
        save_state(run, state)
        process = None
        try:
            env = os.environ.copy()
            env["OMP_NUM_THREADS"] = env["IMI_CPUS_PER_TASK"]
            remaining = deadline - time.time()
            if remaining <= 0:
                raise ValueError("Cycle budget exhausted during batch setup; model was not started")
            with (archive / "gcclassic.log").open("w") as log:
                process = subprocess.Popen(["./gcclassic"], cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                expired = False
                try:
                    code = process.wait(timeout=max(0, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    expired = True
                    stop(process)
                    code = process.returncode
            if code != 0 and not expired:
                raise ValueError(f"GEOS-Chem exited {code}; see {archive / 'gcclassic.log'}")
            when, restart = checkpoint(run, state)
            complete = not expired and when == date(state["end"])
            if not expired and not complete:
                raise ValueError("GEOS-Chem returned success without its final restart")
            shutil.copy2(restart, archive / restart.name)
            write_json(archive / "result.json", dict(timed_stop=expired, returncode=code, restart=str(restart), date=stamp(when),
                                                    job_id=state["job_id"], wrapper_started=started, deadline=deadline))
            if expired and when == date(state["end"]):
                raise ValueError("Timed stop at final restart; finalization did not return success")
            if not complete:
                resume(run, when, restart, archive)
            state.update(status="complete" if complete else "ready", next=stamp(when))
            save_state(run, state)
        except BaseException as exc:
            if process is not None:
                stop(process)
            state.update(status="failed", error=str(exc))
            save_state(run, state)
            raise


def summary(runs, stage, log):
    states = [read_json(run / ".imi_cycles/state.json") for run in runs]
    count = lambda status: sum(s["status"] == status for s in states)
    dates = []
    for run, state in zip(runs, states):
        seen = [state["next"]]
        if state["status"] == "running":
            seen += [p.name.split(".")[-2][:8] for p in (run / "Restarts").glob("GEOSChem.Restart.*_0000z.nc4")
                     if re.fullmatch(r"GEOSChem.Restart.\d{8}_0000z.nc4", p.name)
                     and state["next"] < p.name.split(".")[-2][:8] <= state["end"]]
        dates.append(max(seen))
    done_days = sum((date(s["next"]) - date(s["start"])).days for s in states)
    total_days = sum((date(s["end"]) - date(s["start"])).days for s in states)
    message = (f"{stage:<12} {count('complete'):>5}/{len(states):<5} {count('running'):>7} "
               f"{count('queued'):>7} {count('ready'):>5} {count('failed'):>6} {min(dates)}..{max(dates)} {100 * done_days / total_days:6.1f}%")
    print(message, flush=True)
    with log.open("a") as stream:
        stream.write(message + "\n")
    return states


def controller(config_path, root, stage, range_, directory):
    config = load_config(config_path)
    validate(config)
    root, directory = Path(root).absolute(), Path(directory).absolute()
    if stage == "jacobian":
        m = re.fullmatch(r"(\d+)-(\d+)(?:%\d+)?", range_)
        if not m:
            raise ValueError("Invalid Jacobian array range")
        runs = [directory / f"{config['RunName']}_{i:04d}" for i in range(int(m[1]), int(m[2]) + 1)]
    else:
        runs = [directory]
    if not runs:
        raise ValueError("No simulation directories")
    run_root = (root / config["OutputPath"]).absolute() / config["RunName"]
    metadata = run_root / "cycling"
    metadata.mkdir(exist_ok=True)
    with lock(metadata / f"{stage}.lock"):
        for run in runs:
            prepare(run)
        log = root / "imi_output.log"
        header = "Stage        Complete     Working Waiting Ready Failed Latest restart files   Validated"
        print(header, flush=True)
        with log.open("a") as stream:
            stream.write(header + "\n")
        while True:
            states = summary(runs, stage, log)
            pending = [str(run) for run, state in zip(runs, states) if state["status"] != "complete"]
            if not pending:
                return
            submission = Path(tempfile.mkdtemp(prefix=stage + ".", dir=metadata))
            manifest = submission / "manifest.json"
            write_json(manifest, dict(runs=pending, seconds=seconds(config.get("SimulationCycleTime", "05:30:00"))))
            for name in pending:
                run = Path(name)
                state = read_json(run / ".imi_cycles/state.json")
                state.update(status="queued", submission=str(manifest))
                save_state(run, state)
            array = "" if len(pending) == 1 else f"0-{len(pending)-1}"
            limit = config["MaxSimultaneousRuns"]
            if array and limit > 0:
                array += f"%{min(limit, len(pending))}"
            variables = dict(InversionPath=str(root), RunDirs=str(run_root), ConfigPath=str(Path(config_path).absolute()),
                             UseGCHP="false", IMICycling="true")
            for key in ("PBSAccount", "PBSModel", "PBSSiteNeeded"):
                variables[key] = config.get(key, "")
            script = "source " + shlex.quote(str(root / "src/utilities/common.sh")) + "\n"
            script += "\n".join(key + "=" + shlex.quote(value) for key, value in variables.items()) + "\n"
            args = [array, config["PythonEnv"], "PBS", "false", str(config["RequestedMemory"]), str(config["RequestedCPUs"]),
                    config["RequestedTime"], config["SchedulerPartition"], str(Path(__file__).absolute()), "worker", str(manifest)]
            script += "_imi_submit " + shlex.join(args)
            with (submission / "submission.log").open("w") as output:
                process = subprocess.Popen(["bash", "-c", script], cwd=directory, stdout=output, stderr=subprocess.STDOUT)
                try:
                    while True:
                        try:
                            code = process.wait(timeout=60)
                            break
                        except subprocess.TimeoutExpired:
                            summary(runs, stage, log)
                except BaseException:
                    process.terminate()
                    process.wait()
                    raise
            text = (submission / "submission.log").read_text()
            print(text, end="", flush=True)
            with log.open("a") as stream:
                stream.write(text)
            if code:
                states = summary(runs, stage, log)
                for run, state in zip(runs, states):
                    if state["status"] in ("failed", "queued", "running"):
                        reason = state.get("error", "worker did not finish; reconcile its PBS job before retrying")
                        if state["status"] == "queued":
                            reason = "worker never recorded a start; check PBS and the batch setup log before retrying"
                        message = f"{run.name}: {state['status']}: {reason}"
                        print(message, flush=True)
                        with log.open("a") as stream:
                            stream.write(message + "\n")
                raise ValueError(f"Submission failed ({code}); see {submission}")
            states = [read_json(run / ".imi_cycles/state.json") for run in runs]
            if any(s["status"] not in ("ready", "complete") for s in states):
                raise ValueError("Missing successful cycling result; no automatic retry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["worker", "controller", "validate"])
    parser.add_argument("args", nargs="+")
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise InterruptedError(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    if args.mode == "worker":
        worker(*args.args)
    elif args.mode == "controller":
        controller(*args.args)
    else:
        validate(load_config(args.args[0]))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Simulation cycling: {exc}", file=sys.stderr)
        sys.exit(1)
