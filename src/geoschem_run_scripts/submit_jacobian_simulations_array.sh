#!/bin/bash
echo "running {END} jacobian simulations" >> {InversionPath}/imi_output.log

# remove error status file if present
rm -f .error_status_file.txt

#sbatch --array={START}-{END}{JOBS} --mem $RequestedMemory \
#-c $RequestedCPUs \
#-t $RequestedTime \
#-p $SchedulerPartition \
#-o imi_output.tmp \
#--open-mode=append \
#-W run_jacobian_simulations.sh

qsub -r y \
    -W block=true \
    -J "{START}-{END}{JOBS}" \
    -l "select=1:ncpus=${RequestedCPUs}:mem=${RequestedMemory}" \
    -l "walltime=${RequestedTime}" \
    -o imi_output.tmp \
    ./run_jacobian_simulations.sh

cat imi_output.tmp >> {InversionPath}/imi_output.log
rm imi_output.tmp