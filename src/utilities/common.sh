#!/bin/bash

# Common shell function for the IMI
# Functions available in this file include:
#   - submit_job
#   - submit_python_job
#   - submit_python_stage
#   - submit_array_job
#   - print_stats
#   - imi_failed
#   - ncmax
#   - ncmin

# Synchronous single-node GCClassic/Python submission interface.
# submit_job SCHED SAVE MEM CPUS TIME QUEUE COMMAND [ARG...]
# submit_python_job SCHED SAVE MEM CPUS TIME QUEUE SCRIPT [ARG...]
# submit_array_job SCHED SAVE MEM CPUS TIME QUEUE START-END[%LIMIT] COMMAND [ARG...]
# Artifacts are retained under RunDirs/scheduler; no scientific files are deleted.
submit_job() { _imi_submit "" "" "$@"; }
submit_python_job() { _imi_submit "" "${PythonEnvFile:?PythonEnvFile is required}" "$@"; }
submit_array_job() {
    local scheduler=$1 save=$2 memory=$3 cpus=$4 walltime=$5 queue=$6 array=$7
    shift 7
    _imi_submit "$array" "" "$scheduler" "$save" "$memory" "$cpus" "$walltime" "$queue" "$@"
}

# Resource policy for standalone Python stages (not nested inversion commands).
submit_python_stage() {
    submit_python_job "$SchedulerType" true "${InversionMemory:-$RequestedMemory}" \
        "${InversionCPUs:-$RequestedCPUs}" "${InversionTime:-$RequestedTime}" \
        "${InvSchedulerPartition-$SchedulerPartition}" "$@"
}

_imi_submit() {
    local array=$1 envfile=$2 scheduler=$3 save=$4 memory=$5 cpus=$6 walltime=$7 queue=$8
    shift 8
    local first=0 last=0 limit='' i status=0 rc=0 dir submit_dir root wrapper
    local -a args command=("$@")
    case "$scheduler" in slurm|tmux|PBS) ;; *) echo "Unknown scheduler: $scheduler" >&2; return 2;; esac
    [[ $save == true || $save == false ]] || return 2
    [[ $cpus =~ ^[1-9][0-9]*$ && $# -gt 0 ]] || return 2
    [[ $memory =~ ^[1-9][0-9]*([kKmMgGtT][bB]?)?$ ]] || { echo 'Use positive integer memory with optional kb/mb/gb/tb units' >&2; return 2; }
    [[ $walltime =~ ^[0-9]+:[0-5][0-9]:[0-5][0-9]$ ]] || { echo 'Use walltime HHH:MM:SS' >&2; return 2; }
    if [[ -n $array ]]; then
        [[ $array =~ ^([0-9]+)-([0-9]+)(%([1-9][0-9]*))?$ ]] || return 2
        first=$((10#${BASH_REMATCH[1]})); last=$((10#${BASH_REMATCH[2]})); limit=${BASH_REMATCH[4]}
        (( last >= first )) || return 2
    fi
    [[ $scheduler != PBS || ${UseGCHP:-false} != true ]] || { echo 'PBS GCHP is not supported' >&2; return 2; }
    submit_dir=$(pwd -P) || return
    root=${RunDirs:?RunDirs is required}/scheduler
    mkdir -p "$root" || return
    dir=$(mktemp -d "$root/job.XXXXXX") || return
    dir=$(cd "$dir" && pwd -P) || return
    wrapper=$dir/job.sh
    if [[ -n ${ConfigPath:-} && -f $ConfigPath ]]; then
        cp -- "$ConfigPath" "$dir/config.yml" || return
    fi
    # Record exact argv and submission context; no eval or command-string concatenation.
    printf '%q ' "${command[@]}" > "$dir/command.txt" || return
    printf '\ncwd=%s\nscheduler=%s\narray=%s\ncpus=%s\nmemory=%s\nwalltime=%s\nqueue=%s\n' \
        "$submit_dir" "$scheduler" "$array" "$cpus" "$memory" "$walltime" "$queue" > "$dir/request.txt" || return
    {
        printf '#!/bin/bash\nset -Ee -o pipefail\n'
        printf 'job_dir=%q\n' "$dir"
        if [[ -n $array && $scheduler == PBS && $first == "$last" ]]; then
            printf 'task=%s\n' "$first"
        elif [[ -n $array ]]; then
            case "$scheduler" in
                PBS) printf 'task=${PBS_ARRAY_INDEX:?Missing PBS array index}\n';;
                *) printf 'task=${SLURM_ARRAY_TASK_ID:?Missing Slurm array index}\n';;
            esac
        else
            printf 'task=0\n'
        fi
        cat <<'WORKER'
[[ $task =~ ^[0-9]+$ ]] || exit 2
export IMI_ARRAY_TASK_ID=$task
rm -f "$job_dir/$task.status" "$job_dir/$task.status.part"
# One status record per task, atomically published after command exit.
finish() {
    local rc=$?
    trap - EXIT
    printf '%s\n' "$rc" > "$job_dir/$task.status.part"
    mv "$job_dir/$task.status.part" "$job_dir/$task.status"
    exit "$rc"
}
trap finish EXIT
exec > "$job_dir/$task.log" 2>&1
WORKER
        # Environment files resolve from the repository root, as in run_imi.sh.
        if [[ -n $envfile ]]; then
            printf 'cd %q\nsource %q\n' "$InversionPath" "$envfile"
        fi
        printf 'cd %q\nexport IMI_CPUS_PER_TASK=%q\n' "$submit_dir" "$cpus"
        if [[ -n $envfile ]]; then
            printf 'export LOKY_MAX_CPU_COUNT=%q\nexport OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1\n' "$cpus"
            printf 'python '
        fi
        printf '%q ' "${command[@]}"
        printf '\n'
    } > "$wrapper" || return
    chmod 700 "$wrapper" || return
    case "$scheduler" in
        PBS)
            # Unitless Slurm memory is MiB; PBS unitless memory is bytes.
            [[ $memory =~ [[:alpha:]] ]] || memory="${memory}mb"
            case "${memory,,}" in *kb|*mb|*gb|*tb) ;; *) memory="${memory}b";; esac
            local select="select=1:ncpus=$cpus:mem=${memory,,}"
            [[ -z ${PBSModel:-} ]] || select+=":model=$PBSModel"
            args=(-V -S /bin/bash -N imi_job -l "$select" -l "walltime=$walltime" -W block=true -j oe -o "$dir/")
            [[ -z $queue ]] || args+=(-q "$queue")
            [[ -z ${PBSAccount:-} ]] || args+=(-A "$PBSAccount")
            [[ -z ${PBSSiteNeeded:-} ]] || args+=(-l "site=needed=$PBSSiteNeeded")
            # A one-member logical array can run as an ordinary PBS job.
            if [[ -n $array ]] && (( first != last )); then
                args+=(-r y -J "$array")
            fi
            qsub "${args[@]}" "$wrapper" > "$dir/submit.stdout" 2> "$dir/submit.stderr" || status=$?
            ;;
        *)
            # Slurm accepts M/G etc.; normalize common PBS-style gb suffixes.
            memory=${memory^^}; memory=${memory%B}
            args=(--export=ALL -N 1 --mem "$memory" -c "$cpus" -t "$walltime" -W
                  -o "$dir/bootstrap-%A_%a.log" -e "$dir/bootstrap-%A_%a.err")
            [[ -z $queue ]] || args+=(-p "$queue")
            [[ -z $array ]] || args+=(--array="$array")
            sbatch "${args[@]}" "$wrapper" > "$dir/submit.stdout" 2> "$dir/submit.stderr" || status=$?
            ;;
    esac
    cat "$dir/submit.stdout" || rc=1
    cat "$dir/submit.stderr" >&2 || rc=1
    printf 'IMI submission artifacts: %s\n' "$dir"
    # Missing/deleted/timed-out members must not be mistaken for success.
    for ((i=first; i<=last; i++)); do
        if [[ ! -f $dir/$i.status ]] || [[ $(cat "$dir/$i.status") != 0 ]]; then
            echo "Task $i failed or has no successful completion record: $dir" >&2
            rc=1
        fi
        if [[ $save == true && -f $dir/$i.log ]]; then
            cat "$dir/$i.log" >> "${InversionPath:?}/imi_output.log" || rc=1
        fi
    done
    # Preserve scheduler status ahead of housekeeping/status-record errors.
    (( status == 0 )) || return "$status"
    return "$rc"
}

# Description: 
#   Print runtime stats based on existing variables
# Usage:
#   print_stats
print_stats() {
    printf "\nRuntime statistics (s):"
    printf "\n Setup     : $([[ ! -z $setup_end ]] && echo $(($setup_end - $setup_start)) || echo 0)"
    printf "\n Spinup     : $([[ ! -z $spinup_end ]] && echo $(($spinup_end - $spinup_start)) || echo 0)"
    printf "\n Jacobian     : $([[ ! -z $jacobian_end ]] && echo $(($jacobian_end - $jacobian_start)) || echo 0)"
    printf "\n Inversion     : $([[ ! -z $inversion_end ]] && echo $(($inversion_end - $inversion_start)) || echo 0)"
    printf "\n Posterior     : $([[ ! -z $posterior_end ]] && echo $(($posterior_end - $posterior_start)) || echo 0)\n\n"
}

# Description: Print error message for if the IMI fails
#   Copy output file to output directory if it exists
# Usage:
#   imi_failed
imi_failed() {
    printf "\nFATAL ERROR on line number ${1} of ${2}: IMI exiting."
    if [ -d "${OutputPath}/${RunName}" ]; then
        cp "${InversionPath}/imi_output.log" "${OutputPath}/${RunName}/imi_output.log"
    fi
    exit 1
}

# Description: Print max value of given variable in netCDF file
#   Returns int if only trailing zeros, float otherwise
# Usage:
#   ncmax <variable> <netCDF file path>
ncmax() {
    python -c "import sys; import xarray;\
    print('%g' % xarray.open_dataset(sys.argv[2])[sys.argv[1]].max())" $1 $2
}

# Description: Print min value of given variable in netCDF file
#   Returns int if only trailing zeros, float otherwise
# Usage:
#   ncmax <variable> <netCDF file path>
ncmin() {
    python -c "import sys; import xarray; \
    print('%g' % xarray.open_dataset(sys.argv[2])[sys.argv[1]].min())" $1 $2
}

# Description: Add/Subtract half the spacing between coordinate values
#   of the given NetCDF variable to the min and max values.
#   This is useful for adjusting lat/lon bounds because GEOS-Chem
#   uses the grid cell edges, not centers, for the lat/lon bounds
# Usage:
#   calculate_geoschem_domain <variable> <netCDF file path> <min> <max>
calculate_geoschem_domain() {
    python -c "import sys; import xarray; import numpy as np; \
    sv = xarray.open_dataset(sys.argv[2])[sys.argv[1]]; \
    diff = np.unique(sv.diff(sys.argv[1])).item()/2; \
    print('%f, %f' % (float(sys.argv[3]) - diff, float(sys.argv[4]) + diff))" $1 $2 $3 $4
}

# Description: Generate grid prefix for CS or SGCS grid files
# Usage:
# get_GridSpec_prefix <CS_RES> <STRETCH_GRID> [STRETCH_FACTOR TARGET_LAT TARGET_LON]
get_GridSpec_prefix() {
    local CS_RES="$1"
    local STRETCH_GRID="$2"
    local STRETCH_FACTOR="${3:-1.0}"
    local TARGET_LAT="${4:--90.0}"
    local TARGET_LON="${5:-170.0}"

    if [ "$#" -lt 2 ]; then
        echo "Usage: get_GridSpec_prefix <CS_RES> <STRETCH_GRID> [STRETCH_FACTOR TARGET_LAT TARGET_LON]" >&2
        return 1
    fi

    if "$STRETCH_GRID"; then
        # Format stretch factor: e.g., 1.50 -> 1d50
        local sf_formatted
        sf_formatted=$(printf "%.2f" "$STRETCH_FACTOR" | sed 's/\./d/')

        # Compute geohash using Python, safely quoting lat/lon
        local target_geohash
        target_geohash=$(python3 -c "import pygeohash as pgh; print(pgh.encode(float('$TARGET_LAT'), float('$TARGET_LON')))" )

        echo "c${CS_RES}_s${sf_formatted}_t${target_geohash}"
    else
        echo "c${CS_RES}"
    fi
}

# Description: Create CS or SGCS grid and save to netCDF
# Usage: generate_grid_from_GridSpec <CS_RES> <OUTPUT_FILE> <STRETCH_GRID> [STRETCH_FACTOR TARGET_LAT TARGET_LON]
generate_grid_from_GridSpec() {
    local CS_RES="$1"
    local OUTPUT_FILE="$2"
    local STRETCH_GRID="$3"
    local STRETCH_FACTOR="${4:-1.0}"
    local TARGET_LAT="${5:--90.0}"
    local TARGET_LON="${6:-170.0}"

    if [ "$#" -lt 3 ]; then
        echo "Usage: generate_grid_from_GridSpec <CS_RES> <OUTPUT_FILE> <STRETCH_GRID> [STRETCH_FACTOR TARGET_LAT TARGET_LON]"
        return 1
    fi

    local prefix
    prefix=$(get_GridSpec_prefix "$CS_RES" "$STRETCH_GRID" "$STRETCH_FACTOR" "$TARGET_LAT" "$TARGET_LON")

    python - "$CS_RES" "$OUTPUT_FILE" "$STRETCH_GRID" "$STRETCH_FACTOR" "$TARGET_LAT" "$TARGET_LON" "$prefix" <<EOF
import sys
import os
import numpy as np
import xarray as xr
import pygeohash as pgh

CS_RES = int(sys.argv[1])
OUTPUT_FILE = sys.argv[2]
STRETCH_GRID = sys.argv[3].lower() == "true"
STRETCH_FACTOR = float(sys.argv[4])
TARGET_LAT = float(sys.argv[5])
TARGET_LON = float(sys.argv[6])
prefix = sys.argv[7]

lon   = np.zeros((6, CS_RES, CS_RES))
lat   = np.zeros((6, CS_RES, CS_RES))
lon_b = np.zeros((6, CS_RES + 1, CS_RES + 1))
lat_b = np.zeros((6, CS_RES + 1, CS_RES + 1))
area  = np.zeros((6, CS_RES, CS_RES))

gridspec_file = f'{prefix}_gridspec.nc'
if not os.path.isfile(gridspec_file):
    print(f'ERROR: {gridspec_file} does not exist. Use GridSpec to generate it first!', file=sys.stderr)
    sys.exit(1)

for i in range(6):
    tile_idx = i + 1
    filepath = f"{prefix}.tile{tile_idx}.nc"
    if not os.path.isfile(filepath):
        print(f"ERROR: Tile file {filepath} does not exist!", file=sys.stderr)
        sys.exit(1)
    ds = xr.open_dataset(filepath)

    super_lats = ds['lats'].values   # shape (2*CS_RES+1, 2*CS_RES+1)
    super_lons = ds['lons'].values
    tile_area = ds['area'].values    # shape (CS_RES, CS_RES)

    lat_b[i] = super_lats[::2, ::2]
    lon_b[i] = super_lons[::2, ::2]
    lat[i]   = super_lats[1::2, 1::2]
    lon[i]   = super_lons[1::2, 1::2]
    area[i]  = tile_area

# Adjust longitudes to [0, 360)
lon[lon < 0] += 360
lon_b[lon_b < 0] += 360

# Create xarray DataArrays
area_da = xr.DataArray(
    area,
    dims=['nf', 'Ydim', 'Xdim'],
    coords={'lats': (['nf', 'Ydim', 'Xdim'], lat),
            'lons': (['nf', 'Ydim', 'Xdim'], lon)},
    attrs=dict(units='m2', long_name='Surface area of each grid box')
)

corner_lons_da = xr.DataArray(
    lon_b, dims=['nf', 'YCdim', 'XCdim'],
    attrs=dict(units='degrees_east', long_name='Longitude')
)

corner_lats_da = xr.DataArray(
    lat_b, dims=['nf', 'YCdim', 'XCdim'],
    attrs=dict(units='degrees_north', long_name='Latitude')
)

# Combine into dataset
data = xr.Dataset({
    'area': area_da,
    'corner_lons': corner_lons_da,
    'corner_lats': corner_lats_da
})

# Assign coordinate metadata
data['lats'].attrs = dict(units='degrees_north', long_name='Latitude')
data['lons'].attrs = dict(units='degrees_east', long_name='Longitude')

# Add global attributes if stretched grid
if STRETCH_GRID:
    data.attrs['STRETCH_FACTOR'] = np.float32(STRETCH_FACTOR)
    data.attrs['TARGET_LAT'] = np.float32(TARGET_LAT)
    data.attrs['TARGET_LON'] = np.float32(TARGET_LON)

# Save to netCDF
data.to_netcdf(OUTPUT_FILE)
print(f"Combined c{CS_RES} cubed-sphere grid data saved to {OUTPUT_FILE}")
EOF
}

# Description: regrid TROPOMI restart file (regridded from 47 to 72 layers) to GCHP restart
# Usage: Usage: regrid_tropomi-BC-restart_gcc2gchp {TROPOMI-BC} {TemplatePrefix} {FILE-PREFIX} {CS-RESOLUTION} {STRETCH_GRID} [stretch_factor target_lat target_lon]
regrid_tropomi-BC-restart_gcc2gchp() {
    if [ "$#" -lt 5 ]; then
        echo "Usage: regrid_tropomi-BC-restart_gcc2gchp {TROPOMI-BC} {TemplatePrefix} {FILE-PREFIX} {CS-RESOLUTION} {STRETCH_GRID} [stretch_factor target_lat target_lon]" >&2
        return 1
    fi

    local tropomi_bc=$1
    local template_prefix=$2
    local prefix=$3
    local CS_RES=$4
    local STRETCH_GRID=$5
    local STRETCH_FACTOR="${6:-1.0}"
    local TARGET_LAT="${7:--90.0}"
    local TARGET_LON="${8:-170.0}"

    echo "Regrid restart file for SPC_CH4"

    ncrename -v SpeciesBC_CH4,SpeciesRst_CH4 "$tropomi_bc"
    local template=${template_prefix}.c48.nc4
    local restart_ch4=$prefix.c${CS_RES}.ch4.nc4
    local src_grid="regular_lat_lon_91x144.nc"
    local dst_prefix=$(get_GridSpec_prefix "$CS_RES" "$STRETCH_GRID" "$STRETCH_FACTOR" "$TARGET_LAT" "$TARGET_LON")
    local dst_grid="${dst_prefix}_gridspec.nc"
    if [ ! -f "$src_grid" ]; then
        gridspec-create latlon -b -180 -90 180 90 -pc -hp -dc 91 144 > /dev/null 2>&1
    fi

    if "$STRETCH_GRID"; then
        if [ ! -f "$dst_grid" ]; then
            gridspec-create sgcs -s "${STRETCH_FACTOR}" -t "${TARGET_LAT}" "${TARGET_LON}" "$CS_RES" > /dev/null 2>&1
        fi
        local regrid_weights_ch4="./regrid_weights_latlon_91x144_to_c${CS_RES}_s${STRETCH_FACTOR}_${TARGET_LAT}N_${TARGET_LON}E_conserve.nc"
        if [ ! -f "$regrid_weights_ch4" ]; then
            local regridding_method="conserve"
            ESMF_RegridWeightGen -s "$src_grid" -d "$dst_grid" -m "$regridding_method" -w "$regrid_weights_ch4" > /dev/null 2>&1
        fi
        python -m gcpy.regrid_restart_file       \
            --stretched-grid                        \
            --stretch-factor "$STRETCH_FACTOR"     \
            --target-latitude "$TARGET_LAT"        \
            --target-longitude "$TARGET_LON"       \
            "$tropomi_bc"                          \
            "$regrid_weights_ch4"                  \
            "$template" > /dev/null 2>&1
        mv new_restart_file.nc "$restart_ch4"
    else
        if [ ! -f "$dst_grid" ]; then
            gridspec-create gcs "$CS_RES" > /dev/null 2>&1
        fi
        local regrid_weights_ch4="./regrid_weights_latlon_91x144_to_c${CS_RES}_conserve.nc"
        if [ ! -f "$regrid_weights_ch4" ]; then
            local regridding_method="conserve"
            ESMF_RegridWeightGen -s "$src_grid" -d "$dst_grid" -m "$regridding_method" -w "$regrid_weights_ch4" > /dev/null 2>&1
        fi
        python -m gcpy.regrid_restart_file       \
            "$tropomi_bc"                          \
            "$regrid_weights_ch4"                  \
            "$template" > /dev/null 2>&1
        mv new_restart_file.nc "$restart_ch4"
    fi

    # convert to float and rename final output
    ncap2 -O -s 'SPC_CH4=float(SPC_CH4)' "$restart_ch4" "$restart_ch4"
    mv "$restart_ch4" "${prefix}.c${CS_RES}.nc4"

    # global attributes for STRETCH_GRID
    if "$STRETCH_GRID"; then
        ncatted -O -h -a ,global,d,, "${prefix}.c${CS_RES}.nc4"
        ncatted -O \
            -a STRETCH_FACTOR,global,o,f,$STRETCH_FACTOR \
            -a TARGET_LAT,global,o,f,$TARGET_LAT \
            -a TARGET_LON,global,o,f,$TARGET_LON \
            "${prefix}.c${CS_RES}.nc4"
    fi
    # compress
    nccopy -d1 "${prefix}.c${CS_RES}.nc4" tmp.nc
    mv tmp.nc "${prefix}.c${CS_RES}.nc4"
    # Remove redundant files
    rm -rf conservative_*.nc "$tropomi_bc" PET*.Log

    return 0
}

# Description: Generate gridded global OH scaling factor
# Usage:
#   gridded_optimized_OH <NH_scale> <SH_scale> <Hemis_mask_fpath> <Output_fpath> <OptimizeNorth> <OptimizeSouth> <STRETCH_GRID> [STRETCH_FACTOR TARGET_LAT TARGET_LON]
gridded_optimized_OH() {
    if [ "$#" -lt 7 ]; then
        echo "Usage: gridded_optimized_OH <NH_scale> <SH_scale> <Hemis_mask_fpath> <Output_fpath> <OptimizeNorth> <OptimizeSouth> <STRETCH_GRID> [STRETCH_FACTOR TARGET_LAT TARGET_LON]"
        return 1
    fi

    local NH_scale="$1"
    local SH_scale="$2"
    local Hemis_mask_fpath="$3"
    local Output_fpath="$4"
    local OptimizeNorth="$5"
    local OptimizeSouth="$6"
    local STRETCH_GRID="$7"
    local STRETCH_FACTOR="${8:-1.0}"
    local TARGET_LAT="${9:--90.0}"
    local TARGET_LON="${10:-170.0}"

    python - "$NH_scale" "$SH_scale" "$Hemis_mask_fpath" "$Output_fpath" "$OptimizeNorth" "$OptimizeSouth" "$STRETCH_GRID" "$STRETCH_FACTOR" "$TARGET_LAT" "$TARGET_LON" <<EOF
import sys
import xarray as xr
import numpy as np

N_HEMIS = float(sys.argv[1])
S_HEMIS = float(sys.argv[2])
maskfpath = sys.argv[3]
outfpath = sys.argv[4]
OptimizeNorth = sys.argv[5]
OptimizeSouth = sys.argv[6]
STRETCH_GRID = sys.argv[7].lower() == "true"
STRETCH_FACTOR = float(sys.argv[8])
TARGET_LAT = float(sys.argv[9])
TARGET_LON = float(sys.argv[10])

maskds = xr.open_dataset(maskfpath)
hemis = maskds['Hemisphere']

# Define masks using approximate equality (tolerance 1e-6)
n_mask = abs(hemis - 1.0) < 1e-6
s_mask = abs(hemis - 2.0) < 1e-6

# Initialize oh_scale as all ones with the same dims and coords as hemis
oh_scale = xr.ones_like(hemis)

if OptimizeNorth.lower() == 'true':
    oh_scale = oh_scale.where(~n_mask, other=N_HEMIS)
if OptimizeSouth.lower() == 'true':
    oh_scale = oh_scale.where(~s_mask, other=S_HEMIS)

# Drop Hemisphere and add oh_scale with attributes
scaleds = maskds.drop_vars('Hemisphere').assign(
    oh_scale=oh_scale
)

scaleds['oh_scale'].attrs = dict(long_name='Scaling factor for OH', units='1')

if outfpath is not None:
    print(f"Saving file {outfpath}")
    scaleds.to_netcdf(
        outfpath,
        encoding={v: {"zlib": True, "complevel": 1} for v in scaleds.data_vars},
    )
EOF
}

# Calculate RunDuration (YYYYMMDD) between two dates (YYYYMMDD)
get_run_duration() {
  local start_date=$1
  local end_date=$2

  # Extract components
  local y1=$(date -d "$start_date" +%Y)
  local m1=$(date -d "$start_date" +%m)
  local d1=$(date -d "$start_date" +%d)

  local y2=$(date -d "$end_date" +%Y)
  local m2=$(date -d "$end_date" +%m)
  local d2=$(date -d "$end_date" +%d)

  local years=$((10#$y2 - 10#$y1))
  local months=$((10#$m2 - 10#$m1))
  local days=$((10#$d2 - 10#$d1))

  # Adjust negative days
  if (( days < 0 )); then
    months=$((months - 1))
    # Days in previous month of end_date
    local prev_month_days=$(cal $(date -d "$end_date -1 month" +%m) \
                                 $(date -d "$end_date -1 month" +%Y) | awk 'NF {DAYS=$NF}; END{print DAYS}')
    days=$((days + prev_month_days))
  fi

  # Adjust negative months
  if (( months < 0 )); then
    years=$((years - 1))
    months=$((months + 12))
  fi

  printf "%04d%02d%02d\n" $years $months $days
}
