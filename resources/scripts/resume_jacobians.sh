#!/bin/bash

# Check if input directory is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <jacobian_root_directory>"
    exit 1
fi

# Root directory containing all run directories
ROOT_DIR="$1"

# Check if the root directory exists
if [ ! -d "$ROOT_DIR" ]; then
    echo "Error: Directory $ROOT_DIR does not exist."
    exit 1
fi

# Get the list of run directories dynamically and sort them, skipping the root directory
RUN_DIRS=($(find "$ROOT_DIR" -maxdepth 1 -type d | tail -n +2 | sort))

if [ ${#RUN_DIRS[@]} -eq 0 ]; then
    echo "Error: No run directories found in $ROOT_DIR."
    exit 1
fi

# Loop through each run directory
for RUN_DIR in "${RUN_DIRS[@]}"; do
    echo "Checking run directory: $RUN_DIR"
    
    # Check if the log file contains "End of GEOS-Chem" indicating success
    LOG_FILE=$(ls -t "$RUN_DIR"/*.log 2>/dev/null | head -n 1)
    if [ -z "$LOG_FILE" ]; then
        echo "No log file found in $RUN_DIR."
        continue
    fi

    if grep -q "E N D" "$LOG_FILE"; then
        echo "Simulation in $RUN_DIR completed successfully."
        continue
    fi

    echo "Simulation in $RUN_DIR did not complete successfully. Searching for the last successful date..."
    
    # Identify the last successful netCDF output file
    OUTPUT_DIR="$RUN_DIR/OutputDir/"
    LAST_SUCCESSFUL_FILE=$(ls -v "$OUTPUT_DIR" | grep -E "^.*\.nc4$" | tail -n 1)
    
    if [ -z "$LAST_SUCCESSFUL_FILE" ]; then
        echo "No successful output files found in $OUTPUT_DIR."
        echo "Skipping $RUN_DIR, this will start from the beginning."
        continue
    fi
    echo "Last output file is $LAST_SUCCESSFUL_FILE, finding corresponding restart file"
    
    # Find the corresponding restart file
    RESTART_DIR="$RUN_DIR/Restarts/"
    LAST_SAVED_RESTART=$(ls -v "$RESTART_DIR" | grep -E "^.*\.nc4$" | tail -n 1)
    if [ -z "$LAST_SAVED_RESTART" ]; then
        echo "No restart file found in $RUN_DIR. Starting from the beginning."
        continue
    fi
    echo "Now using $LAST_SAVED_RESTART as the restart file"

    # Extract the date from the netCDF file name (assuming a naming convention)
    LAST_DATE=$(echo "$LAST_SAVED_RESTART" | grep -oE "[0-9]{8}")
    if [ -z "$LAST_DATE" ]; then
        echo "Could not extract the date from the file name: $LAST_SAVED_RESTART"
        continue
    fi
    echo "Last successful date for $RUN_DIR is $LAST_DATE."

    # Update the geoschem_config.yml file with the new start date
    CONFIG_FILE="$RUN_DIR/geoschem_config.yml"
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "Configuration file not found in $RUN_DIR!"
        continue
    fi
    echo "Updating start date in $CONFIG_FILE to $LAST_DATE..."
    sed -i "s/^  start_date: \[.*\]/  start_date: [$LAST_DATE, 000000]/" "$CONFIG_FILE"

    # Update the HEMCO_Config.rc file with the new restart inputs
    HEMCO_FILE="$RUN_DIR/HEMCO_Config.rc"
    if [ ! -f "$HEMCO_FILE" ]; then
        echo "HEMCO file not found in $RUN_DIR!"
        continue
    fi
    echo "Updating tracer inputs in $HEMCO_FILE..."

    # Define the new restart file path and variable template
    RESTART_PATH="./Restarts/GEOSChem.Restart.\$YYYY\$MM\$DD_\$HH\$MNz.nc4"

    # Loop through all SPC_CH4 tracers dynamically
    sed -n '/(((GC_RESTART/,/)))GC_RESTART/p' "$HEMCO_FILE" | grep "SPC_CH4_" | while read -r line; do
        # Extract the tracer name, e.g., SPC_CH4_0002
        TRACER=$(echo "$line" | awk '{print $2}')

        # Extract the tracer suffix, e.g., 0002
        TRACER_SUFFIX=$(echo "$TRACER" | sed 's/SPC_CH4_//')

        # Construct the new variable name, e.g., SpeciesRst_CH4_0002
        VARIABLE="SpeciesRst_CH4_$TRACER_SUFFIX"

        # Replace the line dynamically in HEMCO_Config.rc
        sed -i "/\* $TRACER/c\* $TRACER      $RESTART_PATH $VARIABLE    \$YYYY/\$MM/\$DD/\$HH EFYO xyz 1  CH4_$TRACER_SUFFIX - 1 1" "$HEMCO_FILE"
    done

    echo "Tracers updated in $HEMCO_FILE"
    
    echo "Run directory $RUN_DIR: Updated to start at $LAST_DATE. Ready to rerun Jacobian simulations."
done

echo "Script completed, run the IMI Jacobian simulations again to restart from the last dates"
