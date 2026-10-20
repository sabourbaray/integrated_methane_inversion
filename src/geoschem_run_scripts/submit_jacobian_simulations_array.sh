#!/bin/bash
# Sourced by run_jacobian, where common.sh and configuration are already loaded.
rm -f .error_status_file.txt
if "${EnableSimulationCycling:-false}"; then
    submit_cycling_jobs jacobian "{START}-{END}{JOBS}" "$PWD" || imi_failed "$LINENO" submit_jacobian_simulations_array.sh
else
    submit_array_job "$SchedulerType" true "$RequestedMemory" "$RequestedCPUs" \
        "$RequestedTime" "$SchedulerPartition" "{START}-{END}{JOBS}" \
        bash ./run_jacobian_simulations.sh || imi_failed "$LINENO" submit_jacobian_simulations_array.sh
fi
