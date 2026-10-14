#!/usr/bin/env bash

# 1. Positional Arguments (Input is arg 1, Output is arg 2)
INPUT_DIR="${1:-.}"
OUTPUT_DIR="${2:-}"

# Determine if we are doing an in-place conversion
IN_PLACE=false
if [[ -z "$OUTPUT_DIR" || "$INPUT_DIR" -ef "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="$INPUT_DIR"
    IN_PLACE=true
else
    mkdir -p "$OUTPUT_DIR"
fi

if ! command -v ffmpeg &> /dev/null; then
    echo "Error: FFmpeg is not installed or not in your PATH."
    exit 1
fi

# Determine CPU cores for multiprocessing (Works on macOS/M-chips and Linux)
CORES=$(sysctl -n hw.logicalcpu 2>/dev/null || nproc 2>/dev/null || echo 4)

echo "Source directory: $INPUT_DIR"
if [ "$IN_PLACE" = true ]; then
    echo "Mode: In-Place (Original files will be deleted after successful conversion)"
else
    echo "Output directory: $OUTPUT_DIR (Original files will be deleted after successful conversion)"
fi
echo "Concurrency: Using $CORES CPU cores"
echo "---------------------------------------------------"

# Clean up leftover temp files from a previously interrupted run so they are
# not mistaken for real sources or left to accumulate.
find "$OUTPUT_DIR" -maxdepth 1 -type f -name '.tmp_*.aiff' -delete 2>/dev/null

# Export variables so the parallel subshells can read them
export OUTPUT_DIR
export IN_PLACE

# 2. Define the conversion logic as an exported string for safe execution in xargs
export CONVERT_SCRIPT='
    track="$1"
    filename=$(basename -- "$track")
    
    # Ignore temp files
    [[ "$filename" == .tmp_* ]] && exit 0

    stem="${filename%.*}"
    tmp_track="$OUTPUT_DIR/.tmp_${stem}.aiff"
    final_track="$OUTPUT_DIR/${stem}.aiff"

    # Standard logging (Carriage returns \r break when printing in parallel)
    echo "Converting: $filename"

    # 3. Metadata Tweaks
    # -map 0 preserves ALL streams (audio, video/cover, subtitles, etc.).
    # -c:v copy keeps cover art where the codec is AIFF-compatible. AIFF
    # stores artwork in an ID3 APIC chunk, so if the source image codec
    # (e.g. mjpeg) cannot be muxed, we re-encode the cover to png below
    # instead of silently dropping it.
    stderr_output=$(ffmpeg -y -i "$track" \
        -map 0 \
        -c:a pcm_s16be -ar 44100 \
        -c:v copy \
        -map_metadata 0 -write_id3v2 1 -id3v2_version 3 \
        -loglevel error "$tmp_track" 2>&1)
    exit_code=$?

    # Retry with the cover re-encoded to png if the first pass failed for any
    # reason (unmuxable cover codec, or other transient issues). png is
    # always AIFF-muxable via the ID3 APIC chunk, so artwork is never lost.
    if [[ $exit_code -ne 0 ]]; then
        stderr_output=$(ffmpeg -y -i "$track" \
            -map 0 \
            -c:a pcm_s16be -ar 44100 \
            -c:v png \
            -map_metadata 0 -write_id3v2 1 -id3v2_version 3 \
            -loglevel error "$tmp_track" 2>&1)
        exit_code=$?
    fi

    # Attempt 2: If FFmpeg complained about UTF8, the tags are corrupt.
    if [[ "$stderr_output" == *"Invalid UTF8"* ]]; then
        if [[ "$stem" == *" - "* ]]; then
            artist="${stem%% - *}"
            title="${stem#* - }"
        else
            artist="Unknown Artist"
            title="$stem"
        fi

        # Re-encode, stripping bad text metadata but still carrying cover art
        # (re-encoded to a safe image codec) so artwork is preserved.
        ffmpeg -y -i "$track" \
            -map 0 \
            -c:a pcm_s16be -ar 44100 \
            -c:v png \
            -map_metadata -1 \
            -metadata artist="$artist" \
            -metadata title="$title" \
            -write_id3v2 1 -id3v2_version 3 \
            -loglevel error "$tmp_track" 2>/dev/null
        exit_code=$?

    elif [[ $exit_code -ne 0 ]]; then
        echo "  -> Error converting $filename"
        echo "$stderr_output"
    fi

    # Handle file replacement on success
    if [[ $exit_code -eq 0 ]]; then
        if [[ -e "$final_track" && "$track" != "$final_track" ]]; then
            echo "  -> Skipping $filename: $final_track already exists"
            rm -f "$tmp_track"
        else
            mv "$tmp_track" "$final_track"
        fi

        # The input is now converted (or already had an aiff counterpart),
        # so the original source file is no longer needed. Delete it in
        # every mode — in-place and explicit output directory alike.
        rm -f "$track"
    else
        # Cleanup the broken temp file on failure
        rm -f "$tmp_track"
    fi
'

# 4. Multiprocessing Loop
# Use `find` to grab all target formats EXCEPT .aiff/.aif (recursing into
# sub-directories), then pass to `xargs` to run in parallel.
find "$INPUT_DIR" -type f \
    \( -iname "*.wav" -o -iname "*.flac" -o -iname "*.mp3" -o -iname "*.m4a" -o -iname "*.aac" -o -iname "*.alac" \) \
    -print0 | xargs -0 -n 1 -P "$CORES" bash -c 'eval "$CONVERT_SCRIPT"' _

echo "---------------------------------------------------"
echo "Batch complete."
