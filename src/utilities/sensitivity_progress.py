"""Completion records for resumable observation-file processing."""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pickle


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


@contextlib.contextmanager
def locked(path):
    with Path(str(path) + ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def initialize(path, files, arguments, config, code):
    files = [str(Path(name).absolute()) for name in files]
    stems = [Path(name).name.split(".")[0] for name in files]
    if len(stems) != len(set(stems)):
        raise ValueError("Observation filenames produce duplicate output names")
    inputs = {name: [Path(name).stat().st_size, Path(name).stat().st_mtime_ns]
              for name in files}
    signature = dict(arguments=arguments, config=config, inputs=inputs, code=code)
    signature = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    with locked(path):
        state = read(path) if Path(path).exists() else dict(signature=signature, done={})
        if state["signature"] != signature:
            raise ValueError("Sensitivity inputs or code changed; use a fresh inversion directory")
        state["files"] = files
        state["done"] = {name: record for name, record in state["done"].items()
                         if all(Path(output).is_file() and digest(output) == checksum
                                for output, checksum in record.items())}
        state["cycle_start_completed"] = len(state["done"])
        state["attempt"] = os.environ.get("IMI_SENSITIVITY_ATTEMPT", "")
        write(path, state)
    return set(state["done"])


def record(path, filename, outputs=()):
    checksums = {str(Path(output).absolute()): digest(output) for output in outputs}
    with locked(path):
        state = read(path)
        name = str(Path(filename).absolute())
        if name not in state["files"]:
            raise ValueError(f"Unexpected observation file: {name}")
        state["done"][name] = checksums
        write(path, state)


def save_pair(output, visualization, output_path, visualization_path, n_elements, build):
    rows = output["obs_GC"].shape[0]
    if build and output["K"].shape != (rows, n_elements):
        raise ValueError("Sensitivity matrix does not match observation and element counts")
    if not isinstance(visualization, dict) or "obs_GC" not in visualization:
        raise ValueError("Missing visualization output for completed observations")
    for value, filename in ((output, output_path), (visualization, visualization_path)):
        path = Path(filename)
        temporary = path.with_suffix(path.suffix + ".part")
        with temporary.open("wb") as stream:
            pickle.dump(value, stream, protocol=pickle.HIGHEST_PROTOCOL)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
