#!/usr/bin/env python3
"""Split a video wherever a sustained full-screen marker appears.

Marker frames are omitted.  Output clips are re-encoded for exact
frame boundaries; the source video is never changed.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


SAMPLE_WIDTH = 32
SAMPLE_HEIGHT = 18


def format_timestamp(seconds):
    """Format a timestamp as HH:MM:SS.mmm for people reading the report."""
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def command_output(command):
    return subprocess.check_output(command, text=True, stderr=subprocess.PIPE)


def probe(video):
    result = json.loads(command_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,duration:format=duration",
        "-of", "json", str(video),
    ]))
    stream = result["streams"][0]
    numerator, denominator = stream["avg_frame_rate"].split("/")
    fps = float(numerator) / float(denominator)
    duration = float(stream.get("duration") or result["format"]["duration"])
    if fps <= 0 or duration <= 0:
        raise ValueError("could not determine the video's frame rate or duration")
    return fps, duration


def mean_rgb_from_image(image):
    raw = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(image), "-frames:v", "1",
        "-vf", f"scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT},format=rgb24",
        "-f", "rawvideo", "-",
    ])
    pixels = len(raw) // 3
    return tuple(sum(raw[offset::3]) / pixels for offset in range(3))


def frame_matches_marker(frame, marker_rgb):
    """Return True when most sampled pixels match a solid-color marker."""
    marker_peak = max(marker_rgb)
    dominant = marker_rgb.index(marker_peak)
    other_channels = [value for index, value in enumerate(marker_rgb) if index != dominant]
    color_separation = marker_peak - max(other_channels)
    if color_separation < 20:
        raise ValueError("marker images must have a clearly dominant color")
    brightness_floor = max(50, marker_peak * 0.40)
    separation_floor = max(25, color_separation * 0.20)
    pixels = len(frame) // 3
    matching_pixels = 0
    for index in range(0, len(frame), 3):
        channels = frame[index:index + 3]
        if (
            channels[dominant] >= brightness_floor
            and channels[dominant] - max(channels[(dominant + 1) % 3], channels[(dominant + 2) % 3]) >= separation_floor
        ):
            matching_pixels += 1
    return matching_pixels / pixels >= 0.82


def marker_runs(video, fps, marker_rgbs, minimum_seconds):
    process = subprocess.Popen([
        "ffmpeg", "-v", "error", "-i", str(video),
        "-vf", f"fps={fps},scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT},format=rgb24",
        "-an", "-f", "rawvideo", "-",
    ], stdout=subprocess.PIPE)
    frame_bytes = SAMPLE_WIDTH * SAMPLE_HEIGHT * 3
    run_start = None
    runs = []
    frame_number = 0

    while True:
        frame = process.stdout.read(frame_bytes)
        if len(frame) != frame_bytes:
            break
        is_marker = any(frame_matches_marker(frame, marker_rgb) for marker_rgb in marker_rgbs)
        timestamp = frame_number / fps
        if is_marker and run_start is None:
            run_start = timestamp
        elif not is_marker and run_start is not None:
            if timestamp - run_start >= minimum_seconds:
                runs.append((run_start, timestamp))
            run_start = None
        frame_number += 1

    if run_start is not None and frame_number / fps - run_start >= minimum_seconds:
        runs.append((run_start, frame_number / fps))
    if process.wait() != 0:
        raise RuntimeError("FFmpeg could not decode the video")
    return runs


def write_clip(source, start, end, destination, crf):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
        "-preset", "medium", "-crf", str(crf), "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(destination),
    ], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--marker", type=Path, action="append", default=[],
                        help="reference marker image; repeat for multiple markers")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--minimum-blue-seconds", type=float, default=0.5)
    parser.add_argument("--crf", type=float, default=15,
                        help="H.264 video quality: lower is higher quality (default: 15)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report markers and clips, but create no videos")
    args = parser.parse_args()

    marker_paths = args.marker or [Path("Blue marker.png")]
    if not args.video.is_file() or any(not marker.is_file() for marker in marker_paths):
        parser.error("the video and all marker images must exist")
    if args.minimum_blue_seconds <= 0:
        parser.error("--minimum-blue-seconds must be positive")
    if not 0 <= args.crf <= 51:
        parser.error("--crf must be between 0 and 51")

    fps, duration = probe(args.video)
    marker_rgbs = [mean_rgb_from_image(marker) for marker in marker_paths]
    print(f"Video: {duration:.1f}s at {fps:.3f} fps")
    for marker, marker_rgb in zip(marker_paths, marker_rgbs):
        print(f"Marker {marker}: RGB " + ", ".join(f"{value:.0f}" for value in marker_rgb))
    markers = marker_runs(args.video, fps, marker_rgbs, args.minimum_blue_seconds)
    print(f"Found {len(markers)} marker(s):")
    for start, end in markers:
        print(f"  {format_timestamp(start)} to {format_timestamp(end)} ({end - start:.3f}s)")

    clips = []
    cursor = 0.0
    for start, end in markers:
        if start - cursor >= 0.25:
            clips.append((cursor, start))
        cursor = end
    if duration - cursor >= 0.25:
        clips.append((cursor, duration))
    print(f"Will create {len(clips)} clip(s).")
    if args.dry_run:
        return

    output_dir = args.output_dir or args.video.with_name(args.video.stem + " - segments")
    output_dir.mkdir(parents=True, exist_ok=True)
    for index, (start, end) in enumerate(clips, 1):
        output = output_dir / f"{args.video.stem} - part {index:02d}.mp4"
        print(f"Creating {output.name}: {format_timestamp(start)} to {format_timestamp(end)}")
        write_clip(args.video, start, end, output, args.crf)
    print(f"Done. Clips are in: {output_dir}")


if __name__ == "__main__":
    main()
