#!/bin/bash
echo "running {END} jacobian simulations" >> {InversionPath}/imi_output.log

# remove error status file if present
rm -f .error_status_file.txt

#sbatch --array={START}-{END} --mem $JacobianMemory -c $JacobianCPUs -t $RequestedTime -W run_jacobian_simulations.sh
qsub -W block=true -J {START}-{END} -l select=1:ncpus=$JacobianCPUs:mem=$JacobianMemory,walltime=$RequestedTime ./run_jacobian_simulations.sh
