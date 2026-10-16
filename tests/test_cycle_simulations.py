import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
import xarray as xr
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/utilities"))
import cycle_simulations as cycle

HISTORY = """COLLECTIONS: #'Restart',
 'SpeciesConc',
::
Restart.filename: './Restarts/GEOSChem.Restart.%y4%m2%d2_%h2%n2z.nc4',
Restart.frequency: 'End',
Restart.duration: 'End',
Restart.mode: 'instantaneous'
Restart.fields: 'SpeciesRst_?ALL?', 'Met_DELPDRY', 'Met_BXHEIGHT', 'Met_TropLev',
::
SpeciesConc.frequency: 00000000 010000
SpeciesConc.duration: 00000001 000000
SpeciesConc.mode: 'time-averaged'
SpeciesConc.fields: 'SpeciesConcVV_?ALL?',
::
"""
HEMCO = """DiagnFreq: End
(((GC_RESTART
* SPC_CH4 initial.nc SpeciesBC_CH4 $YYYY/$MM/$DD/$HH CYS xyz 1 CH4 - 1 1
* SPC_CH4_0001 - - - - - - CH4_0001 - 1 1
* DELPDRY initial.nc Met_DELPDRY $YYYY/$MM/$DD/$HH EY xyz 1 * - 1 1
)))GC_RESTART
* BC_CH4 boundary.nc SpeciesBC_CH4 $YYYY/$MM/$DD/$HH EFY xyz 1 CH4 - 1 1
"""


def fixture(path, end=20230104):
    path.mkdir(parents=True)
    (path / "OutputDir").mkdir()
    (path / "Restarts").mkdir()
    (path / "geoschem_config.yml").write_text(f"""simulation:
  start_date: [20230101, 000000]
  end_date: [{end}, 000000]
operations:
  transport:
    transported_species:
      - CH4
      - CH4_0001
""")
    (path / "HISTORY.rc").write_text(HISTORY)
    (path / "HEMCO_Config.rc").write_text(HEMCO)
    return path


def restart(path, when, missing=False):
    names = ["SpeciesRst_CH4", "SpeciesRst_CH4_0001", "Met_DELPDRY", "Met_BXHEIGHT", "Met_TropLev"]
    if missing:
        names.remove("SpeciesRst_CH4_0001")
    ds = xr.Dataset({n: (("time", "lev", "lat", "lon"), np.ones((1, 1, 1, 1))) for n in names},
                    coords={"time": [np.datetime64(when)]})
    ds.to_netcdf(path / "Restarts" / f"GEOSChem.Restart.{cycle.stamp(when)}_0000z.nc4")


def diagnostics(path, day, hours=24):
    ts = np.datetime64(day) + np.arange(hours) * np.timedelta64(1, "h") + np.timedelta64(30, "m")
    xr.Dataset({"SpeciesConcVV_" + n: (("time",), np.ones(hours)) for n in ["CH4", "CH4_0001"]},
               coords={"time": ts}).to_netcdf(path / "OutputDir" / f"GEOSChem.SpeciesConc.{cycle.stamp(day)}_0000z.nc4")


class CyclingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.run = fixture(self.base / "run")

    def tearDown(self):
        self.temp.cleanup()

    def test_history_and_multitracer_resume(self):
        state = cycle.prepare(self.run)
        diagnostics(self.run, cycle.date(20230101))
        restart(self.run, cycle.date(20230102))
        when, path = cycle.checkpoint(self.run, state)
        archive = self.base / "archive"
        archive.mkdir()
        diagnostics(self.run, when, 3)
        cycle.resume(self.run, when, path, archive)
        hemco = (self.run / "HEMCO_Config.rc").read_text()
        self.assertIn("SpeciesRst_?ALL?", hemco)
        self.assertNotIn("* SPC_CH4_0001 -", hemco)
        self.assertIn("* BC_CH4 boundary.nc", hemco)
        self.assertTrue((archive / "overlap/OutputDir/GEOSChem.SpeciesConc.20230102_0000z.nc4").exists())
        self.assertEqual(yaml.safe_load((self.run / "geoschem_config.yml").read_text())["simulation"]["start_date"], [20230102, 0])
        self.assertIn("Restart.frequency: 00000001 000000", (self.run / "HISTORY.rc").read_text())

    def test_corrupt_newer_restart_falls_back(self):
        state = cycle.prepare(self.run)
        diagnostics(self.run, cycle.date(20230101))
        restart(self.run, cycle.date(20230102))
        (self.run / "Restarts/GEOSChem.Restart.20230103_0000z.nc4").write_bytes(b"truncated")
        self.assertEqual(cycle.checkpoint(self.run, state)[0], cycle.date(20230102))

    def test_missing_tag_rejected(self):
        state = cycle.prepare(self.run)
        restart(self.run, cycle.date(20230102), missing=True)
        with self.assertRaisesRegex(ValueError, "no valid advancing"):
            cycle.checkpoint(self.run, state)

    def test_partial_diagnostics_rejected(self):
        state = cycle.prepare(self.run)
        diagnostics(self.run, cycle.date(20230101), 12)
        restart(self.run, cycle.date(20230102))
        with self.assertRaisesRegex(ValueError, "no valid advancing"):
            cycle.checkpoint(self.run, state)

    def test_year_boundary(self):
        path = self.run / "geoschem_config.yml"
        path.write_text(path.read_text().replace("20230101", "20231231").replace("20230104", "20240101"))
        state = cycle.prepare(self.run)
        diagnostics(self.run, cycle.date(20231231))
        restart(self.run, cycle.date(20240101))
        self.assertEqual(cycle.checkpoint(self.run, state)[0], cycle.date(20240101))

    def test_stale_and_modified_state_rejected(self):
        state = cycle.prepare(self.run)
        state["status"] = "running"
        cycle.save_state(self.run, state)
        with self.assertRaisesRegex(ValueError, "previous cycle"):
            cycle.prepare(self.run)
        state["status"] = "ready"
        cycle.save_state(self.run, state)
        with (self.run / "HEMCO_Config.rc").open("a") as stream:
            stream.write("changed\n")
        with self.assertRaisesRegex(ValueError, "changed since"):
            cycle.prepare(self.run)

    def test_existing_output_requires_fresh_run(self):
        diagnostics(self.run, cycle.date(20230101))
        with self.assertRaisesRegex(ValueError, "existing output"):
            cycle.prepare(self.run)

    def test_three_cycles_and_completion(self):
        cycle.prepare(self.run)
        code = f'''#!{sys.executable}
import sys, time
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_cycle_simulations import *
p=Path.cwd()
gc=yaml.safe_load((p/'geoschem_config.yml').read_text())
a=cycle.date(gc['simulation']['start_date'][0]); b=cycle.date(gc['simulation']['end_date'][0])
diagnostics(p,a)
restart(p,a+cycle.dt.timedelta(days=1))
if a+cycle.dt.timedelta(days=1)<b:
    diagnostics(p,a+cycle.dt.timedelta(days=1),3)
    time.sleep(30)
'''
        (self.run / "gcclassic").write_text(code)
        (self.run / "gcclassic").chmod(0o755)
        manifest = self.base / "manifest.json"
        cycle.write_json(manifest, {"runs": [str(self.run)], "seconds": 3})
        with patch.dict(os.environ, IMI_ARRAY_TASK_ID="0", IMI_CPUS_PER_TASK="2"):
            for i in range(3):
                state = cycle.read_json(self.run / ".imi_cycles/state.json")
                state.update(status="queued", submission=str(manifest))
                cycle.save_state(self.run, state)
                cycle.worker(str(manifest))
                state = cycle.read_json(self.run / ".imi_cycles/state.json")
                self.assertEqual(state["next"], str(20230102 + i))
        self.assertEqual(state["status"], "complete")
        self.assertEqual(len(list((self.run / ".imi_cycles").glob("cycle*"))), 3)
        self.assertEqual(len(list((self.run / "OutputDir").glob("GEOSChem.SpeciesConc.*"))), 3)

    def test_model_failure_not_retried(self):
        state = cycle.prepare(self.run)
        executable = self.run / "gcclassic"
        executable.write_text("#!/bin/bash\nexit 7\n")
        executable.chmod(0o755)
        manifest = self.base / "manifest.json"
        cycle.write_json(manifest, {"runs": [str(self.run)], "seconds": 3})
        state.update(status="queued", submission=str(manifest))
        cycle.save_state(self.run, state)
        with patch.dict(os.environ, IMI_ARRAY_TASK_ID="0", IMI_CPUS_PER_TASK="2"):
            with self.assertRaisesRegex(ValueError, "exited 7"):
                cycle.worker(str(manifest))
        self.assertEqual(cycle.read_json(self.run / ".imi_cycles/state.json")["status"], "failed")

    def test_no_progress_stops(self):
        state = cycle.prepare(self.run)
        executable = self.run / "gcclassic"
        executable.write_text("#!/bin/bash\nsleep 30\n")
        executable.chmod(0o755)
        manifest = self.base / "manifest.json"
        cycle.write_json(manifest, {"runs": [str(self.run)], "seconds": 0.1})
        state.update(status="queued", submission=str(manifest))
        cycle.save_state(self.run, state)
        with patch.dict(os.environ, IMI_ARRAY_TASK_ID="0", IMI_CPUS_PER_TASK="2"):
            with self.assertRaisesRegex(ValueError, "no valid advancing"):
                cycle.worker(str(manifest))

    def test_worker_lock(self):
        cycle.prepare(self.run)
        with cycle.lock(self.run / ".imi_cycles/worker.lock"):
            with self.assertRaises(BlockingIOError):
                with cycle.lock(self.run / ".imi_cycles/worker.lock"):
                    self.fail("Duplicate worker acquired lock")

    def test_controller_array_then_singleton(self):
        self.controller_case()

    def test_missing_array_member_stops_without_resubmitting(self):
        self.controller_case(missing=True)

    def controller_case(self, missing=False):
        repo = self.base / "repo"
        (repo / "src/utilities").mkdir(parents=True)
        shutil.copy2(ROOT / "src/utilities/common.sh", repo / "src/utilities/common.sh")
        output = self.base / "output/Experiment"
        output.mkdir(parents=True)
        runs = output / "jacobian_runs"
        for i in range(2):
            run = fixture(runs / f"Experiment_{i:04d}")
            code = f"""#!{sys.executable}
import sys, time
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_cycle_simulations import *
p=Path.cwd()
gc=yaml.safe_load((p/'geoschem_config.yml').read_text())
a=cycle.date(gc['simulation']['start_date'][0]); b=cycle.date(gc['simulation']['end_date'][0])
while a<b:
    diagnostics(p,a)
    a+=cycle.dt.timedelta(days=1)
    restart(p,a)
    if {i} == 0 and a<b:
        time.sleep(30)
"""
            (run / "gcclassic").write_text(code)
            (run / "gcclassic").chmod(0o755)
        mockbin = self.base / "bin"
        mockbin.mkdir()
        (mockbin / "qsub").write_text(f"""#!{sys.executable}
import os, sys, subprocess, json
from pathlib import Path
a=sys.argv[1:]
with Path(os.environ['MOCK_CALLS']).open('a') as stream: stream.write(json.dumps(a)+'\\n')
ids=[0]
if '-J' in a:
    assert a[a.index('-r')+1]=='y'
    first,last=a[a.index('-J')+1].split('%')[0].split('-');ids=range(int(first),int(last)+1)
rc=0
for i in ids:
    if {missing!r} and i == 1:
        continue
    env=os.environ.copy();env['PBS_ARRAY_INDEX']=str(i);env['PBS_JOBID']=f'123[{{i}}].mock'
    rc=max(rc,subprocess.run(['bash',a[-1]],cwd='/',env=env).returncode)
print('123[].mock')
sys.exit(rc)
""")
        (mockbin / "qsub").chmod(0o755)
        (repo / "env.sh").write_text("sleep 0.2\n")
        config = yaml.safe_load((ROOT / "test_gta_2.3a.yml").read_text())
        config.update(RunName="Experiment", OutputPath=str(output.parent), PythonEnv="./env.sh",
                      SimulationCycleTime="00:00:03", RequestedTime="00:05:03", MaxSimultaneousRuns=1)
        cfg = self.base / "config.yml"
        cfg.write_text(yaml.safe_dump(config))
        calls = self.base / "calls.jsonl"
        with patch.dict(os.environ, PATH=str(mockbin) + os.pathsep + os.environ["PATH"], MOCK_CALLS=str(calls)):
            if missing:
                with self.assertRaisesRegex(ValueError, "Submission failed"):
                    cycle.controller(str(cfg), str(repo), "jacobian", "0-1%1", str(runs))
                self.assertEqual(len(list((output / "cycling").glob("jacobian.*/manifest.json"))), 1)
                state = cycle.read_json(runs / "Experiment_0001/.imi_cycles/state.json")
                self.assertEqual((state["status"], state["cycle"]), ("queued", 0))
                self.assertIn("worker never recorded a start", (repo / "imi_output.log").read_text())
                return
            cycle.controller(str(cfg), str(repo), "jacobian", "0-1%1", str(runs))
            posterior = fixture(output / "posterior_run")
            shutil.copy2(runs / "Experiment_0001/gcclassic", posterior / "gcclassic")
            cycle.controller(str(cfg), str(repo), "posterior", "", str(posterior))
            self.assertEqual(cycle.read_json(posterior / ".imi_cycles/state.json")["status"], "complete")
        manifests = [cycle.read_json(p) for p in (output / "cycling").glob("jacobian.*/manifest.json")]
        self.assertEqual(sorted(len(m["runs"]) for m in manifests), [1, 1, 2])
        for run in runs.iterdir():
            self.assertEqual(cycle.read_json(run / ".imi_cycles/state.json")["status"], "complete")
        self.assertIn("100.0%", (repo / "imi_output.log").read_text())
        first = cycle.read_json(runs / "Experiment_0000/.imi_cycles/cycle00001/result.json")
        later = cycle.read_json(runs / "Experiment_0001/.imi_cycles/cycle00001/result.json")
        self.assertGreaterEqual(later["wrapper_started"], first["deadline"])
        self.assertAlmostEqual(later["deadline"] - later["wrapper_started"], 3)
        self.assertEqual(later["job_id"], "123[1].mock")
        argv = cycle.json.loads(calls.read_text().splitlines()[0])
        self.assertEqual(argv[argv.index("-J") + 1], "0-1%1")

    def test_setup_exhausts_cycle_budget(self):
        state = cycle.prepare(self.run)
        manifest = self.base / "manifest.json"
        cycle.write_json(manifest, {"runs": [str(self.run)], "seconds": 3})
        state.update(status="queued", submission=str(manifest))
        cycle.save_state(self.run, state)
        with patch.dict(os.environ, IMI_ARRAY_TASK_ID="0", IMI_CPUS_PER_TASK="2",
                        PBS_JOBID="123[0].mock", IMI_JOB_START_TIME=str(time.time() - 10)):
            with patch.object(cycle.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(ValueError, "budget exhausted during batch setup"):
                    cycle.worker(str(manifest))
                launch.assert_not_called()
        self.assertEqual(cycle.read_json(self.run / ".imi_cycles/state.json")["status"], "failed")

    def test_pbs_requires_wrapper_start(self):
        manifest = self.base / "manifest.json"
        cycle.write_json(manifest, {"runs": [str(self.run)], "seconds": 3})
        with patch.dict(os.environ, {"PBS_JOBID": "123[0].mock"}, clear=True):
            with self.assertRaisesRegex(ValueError, "did not record its start time"):
                cycle.worker(str(manifest))

    def test_walltime_validation(self):
        config = dict(EnableSimulationCycling=True, SchedulerType="PBS", UseGCHP=False, Species="CH4",
                      KalmanMode=False, HourlySpecies=True, RequestedTime="06:00:00", SimulationCycleTime="05:30:00")
        cycle.validate(config)
        config["RequestedTime"] = "03:00:00"
        with self.assertRaises(ValueError):
            cycle.validate(config)
        config["SimulationCycleTime"] = "02:30:00"
        cycle.validate(config)


if __name__ == "__main__":
    unittest.main()
