#!/usr/bin/env python3
"""Split a video wherever a sustained full-screen blue marker appears.

The blue marker frames are omitted.  Output clips are re-encoded for exact
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


def blue_runs(video, fps, marker_rgb, minimum_seconds):
    process = subprocess.Popen([
        "ffmpeg", "-v", "error", "-i", str(video),
        "-vf", f"fps={fps},scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT},format=rgb24",
        "-an", "-f", "rawvideo", "-",
    ], stdout=subprocess.PIPE)
    frame_bytes = SAMPLE_WIDTH * SAMPLE_HEIGHT * 3
    run_start = None
    runs = []
    frame_number = 0
    marker_r, marker_g, marker_b = marker_rgb

    while True:
        frame = process.stdout.read(frame_bytes)
        if len(frame) != frame_bytes:
            break
        pixels = SAMPLE_WIDTH * SAMPLE_HEIGHT
        r = sum(frame[0::3]) / pixels
        g = sum(frame[1::3]) / pixels
        b = sum(frame[2::3]) / pixels
        blue_pixels = sum(
            1 for i in range(0, len(frame), 3)
            if frame[i + 2] >= 70
            and frame[i + 2] - max(frame[i], frame[i + 1]) >= 35
        )
        # A marker must be strongly blue across nearly the whole image.  The
        # marker image supplies a brightness floor, which helps avoid shadows.
        is_blue = (
            blue_pixels / pixels >= 0.82
            and b >= max(70, marker_b * 0.45)
            and b >= r * 1.35
            and b >= g * 1.20
        )
        timestamp = frame_number / fps
        if is_blue and run_start is None:
            run_start = timestamp
        elif not is_blue and run_start is not None:
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
    parser.add_argument("--marker", type=Path, default=Path("Blue marker.png"),
                        help="reference blue-marker image (default: Blue marker.png)")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--minimum-blue-seconds", type=float, default=0.5)
    parser.add_argument("--crf", type=float, default=15,
                        help="H.264 video quality: lower is higher quality (default: 15)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report markers and clips, but create no videos")
    args = parser.parse_args()

    if not args.video.is_file() or not args.marker.is_file():
        parser.error("the video and marker image must both exist")
    if args.minimum_blue_seconds <= 0:
        parser.error("--minimum-blue-seconds must be positive")
    if not 0 <= args.crf <= 51:
        parser.error("--crf must be between 0 and 51")

    fps, duration = probe(args.video)
    marker_rgb = mean_rgb_from_image(args.marker)
    print(f"Video: {duration:.1f}s at {fps:.3f} fps")
    print("Marker average RGB: " + ", ".join(f"{value:.0f}" for value in marker_rgb))
    markers = blue_runs(args.video, fps, marker_rgb, args.minimum_blue_seconds)
    print(f"Found {len(markers)} blue marker(s):")
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
