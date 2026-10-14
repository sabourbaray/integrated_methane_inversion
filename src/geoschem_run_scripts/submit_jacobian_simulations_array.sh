#!/bin/bash
# Sourced by run_jacobian, where common.sh and configuration are already loaded.
rm -f .error_status_file.txt
submit_array_job "$SchedulerType" true "$RequestedMemory" "$RequestedCPUs" \
    "$RequestedTime" "$SchedulerPartition" "{START}-{END}{JOBS}" \
    bash ./run_jacobian_simulations.sh || imi_failed "$LINENO" submit_jacobian_simulations_array.sh
