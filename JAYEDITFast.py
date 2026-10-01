"""
Random Video Generator - GUI Edition
=====================================

A Tkinter front-end around the original random-clip-picking + FFmpeg
rendering pipeline. The core algorithm (random clip lengths, no two
consecutive clips from the same source, never reuse overlapping footage
from the same source) is preserved from the original command-line script.
Everything that used to require editing the .py file directly (source
folder, output folder, aspect ratio, quality, audio, clip length, final
duration, filename) is now exposed as GUI controls.

Requirements:
    - Python 3.8+
    - ffmpeg and ffprobe available on the Windows PATH
    - An NVIDIA GPU that supports av1_nvenc

No third-party packages are required; only the standard library is used.
"""

import json
import os
import queue
import random
import shutil
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# =====================================================================
# STATIC CONFIGURATION
# =====================================================================

APP_TITLE = "JAYEDITFast"

# Source type -> set of file extensions to scan for.
SOURCE_TYPES = {
    "Auto / All Videos": {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"},
    "MP4": {".mp4"},
    "MOV": {".mov"},
    "MKV": {".mkv"},
    "WEBM": {".webm"},
    "AVI": {".avi"},
    "M4V": {".m4v"},
}

# Aspect ratio label -> (output width, output height)
ASPECT_RATIOS = {
    "16:9 - YouTube Landscape": (3840, 2160),
    "9:16 - Shorts / Reels": (2160, 3840),
    "1:1 - Square": (2160, 2160),
    "4:5 - Portrait": (2160, 2700),
    "4:3 - Classic": (2880, 2160),
    "3:2": (3240, 2160),
    "21:9 - Ultrawide": (3840, 1646),
}

# Quality label -> NVENC constant-quality value (lower = better quality)
QUALITY_CQ = {
    "High": 25,
    "Very High": 20,
    "Maximum": 17,
}
NVENC_PRESET = "p5"

# How many clips go into one FFmpeg filter_complex/encode pass. Rendering
# everything in a single pass with hundreds of clips produces a filter
# graph long enough to blow past Windows' ~32K character command-line
# limit (and some FFmpeg builds don't support -filter_complex_script as a
# workaround). Instead, clips are rendered in small batches, each batch is
# encoded once (full quality, no re-encoding later), and the resulting
# segment files are joined losslessly with the concat demuxer (-c copy).
BATCH_SIZE = 18

DEFAULT_FINAL_DURATION = "10:30"   # MM:SS -> 630 seconds
DEFAULT_MIN_CLIP = "2.0"
DEFAULT_MAX_CLIP = "7.8"
DEFAULT_FPS = "25"

CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# =====================================================================
# HELPERS: duration parsing / formatting
# =====================================================================

def parse_duration_to_seconds(text):
    """Accepts 'SS', 'MM:SS' or 'HH:MM:SS' and returns float seconds."""
    text = text.strip()
    if not text:
        raise ValueError("Duration is empty.")
    parts = text.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Invalid duration '{text}'. Use MM:SS, e.g. 10:30")

    if len(parts) == 1:
        seconds = parts[0]
    elif len(parts) == 2:
        seconds = parts[0] * 60 + parts[1]
    elif len(parts) == 3:
        seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
    else:
        raise ValueError(f"Invalid duration '{text}'. Use MM:SS, e.g. 10:30")

    if seconds <= 0:
        raise ValueError("Duration must be greater than zero.")
    return seconds


def format_seconds(total_seconds):
    total_seconds = max(0, int(round(total_seconds)))
    m, s = divmod(total_seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def safe_filename_part(text):
    text = text.strip()
    if not text:
        return "random_video"
    keep = "".join(c if (c.isalnum() or c in " _-") else "_" for c in text)
    return keep.strip().replace(" ", "_") or "random_video"


# =====================================================================
# FFPROBE / FFMPEG HELPERS
# =====================================================================

def probe_video(file_path):
    """Return (duration_seconds, width, height, has_audio) for a file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(file_path),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=CREATIONFLAGS
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "ffprobe failed").strip().splitlines()[-1]
                            if result.stderr else "ffprobe failed")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        raise RuntimeError("no video stream found")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)

    duration = video_stream.get("duration") or fmt.get("duration")
    if duration is None:
        raise RuntimeError("could not determine duration")
    duration = float(duration)

    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    return duration, width, height, has_audio


def find_free_position(duration, clip_duration, used, attempts=1000):
    """Find a random [start,end) section of `duration` seconds long that
    does not overlap any interval already in `used`."""
    if duration <= clip_duration:
        return None

    for _ in range(attempts):
        start = random.uniform(0, duration - clip_duration)
        end = start + clip_duration

        overlap = False
        for old_start, old_end in used:
            if start < old_end and end > old_start:
                overlap = True
                break

        if not overlap:
            return start, end

    return None


def distribute_frames(num_clips, target_frames, min_frames, max_frames):
    """Split target_frames into exactly num_clips random lengths, each
    between min_frames and max_frames. Raises ValueError if that's
    impossible given the bounds."""

    if num_clips * min_frames > target_frames:
        raise ValueError(
            f"{num_clips} clips at the minimum clip length would already "
            f"exceed the final duration. Lower the minimum clip length, "
            f"raise the final duration, or use fewer clips."
        )
    if num_clips * max_frames < target_frames:
        raise ValueError(
            f"{num_clips} clips can't add up to the final duration even at "
            f"the maximum clip length. Raise the maximum clip length, "
            f"shorten the final duration, or use more clips."
        )

    frames = [min_frames] * num_clips
    remaining = target_frames - min_frames * num_clips
    indices = list(range(num_clips))

    while remaining > 0:
        random.shuffle(indices)
        progress = False
        for idx in indices:
            if remaining <= 0:
                break
            capacity = max_frames - frames[idx]
            if capacity <= 0:
                continue
            add = random.randint(1, min(capacity, remaining))
            frames[idx] += add
            remaining -= add
            progress = True
        if not progress:
            break  # shouldn't happen given the checks above

    random.shuffle(frames)
    return frames


def build_clip_plan(videos, target_frames, min_frames, max_frames, fps, msg_queue, stop_event, num_clips=None):
    """Randomly plan clip lengths and pick non-overlapping, non-consecutive-
    source sections for each. Returns a list of clip dicts, or None if the
    plan could not be completed (an error/stop message will already have
    been queued).

    If num_clips is given, exactly that many clips are used (their lengths
    are randomly distributed between min_frames and max_frames). Otherwise
    the number of clips emerges naturally from target_frames divided by
    random lengths in [min_frames, max_frames]."""

    if num_clips:
        try:
            clip_lengths = distribute_frames(num_clips, target_frames, min_frames, max_frames)
        except ValueError as e:
            msg_queue.put(("error", str(e)))
            return None
    else:
        remaining_frames = target_frames
        clip_lengths = []

        while remaining_frames > max_frames:
            maximum = min(max_frames, remaining_frames - min_frames)
            if maximum < min_frames:
                break
            frames = random.randint(min_frames, maximum)
            clip_lengths.append(frames)
            remaining_frames -= frames

        if remaining_frames > 0:
            clip_lengths.append(remaining_frames)

    clips = []
    previous_source = None

    for frames in clip_lengths:
        if stop_event.is_set():
            msg_queue.put(("stopped", None))
            return None

        clip_duration = frames / fps

        candidates = [
            v for v in videos
            if v["file"] != previous_source and v["duration"] >= clip_duration
        ]
        random.shuffle(candidates)

        selected = None
        start = end = None
        for video in candidates:
            position = find_free_position(video["duration"], clip_duration, video["used"])
            if position:
                selected = video
                start, end = position
                break

        if selected is None:
            # Fallback: allow same source as previous if nothing else fits.
            candidates = [v for v in videos if v["duration"] >= clip_duration]
            random.shuffle(candidates)
            for video in candidates:
                position = find_free_position(video["duration"], clip_duration, video["used"])
                if position:
                    selected = video
                    start, end = position
                    break

        if selected is None:
            msg_queue.put((
                "error",
                "Not enough unused footage to build the requested duration.\n"
                "Try a shorter final duration, a smaller max clip length, "
                "or add more/longer source videos."
            ))
            return None

        selected["used"].append((start, end))
        clips.append({
            "file": selected["file"],
            "start": start,
            "duration": clip_duration,
            "has_audio": selected["has_audio"],
        })
        previous_source = selected["file"]

    return clips


def build_batch_command(batch_clips, width, height, fps, audio_on, cq, segment_path, batch_duration):
    """Build one FFmpeg command that renders a small batch of clips (scale
    + crop + concat, encoded once at full quality) into a single segment
    file. Kept small on purpose so the filter graph / command line never
    gets anywhere near Windows' command-line length limit."""

    inputs = []
    filters = []
    concat_parts = []
    input_index = 0

    for i, clip in enumerate(batch_clips):
        inputs.extend([
            "-ss", f"{clip['start']:.3f}",
            "-t", f"{clip['duration']:.3f}",
            "-i", str(clip["file"]),
        ])
        video_audio_idx = input_index
        input_index += 1

        filters.append(
            f"[{video_audio_idx}:v]"
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},"
            f"fps={fps},"
            f"setsar=1,"
            f"setpts=PTS-STARTPTS"
            f"[v{i}]"
        )

        if audio_on:
            if clip["has_audio"]:
                audio_src = f"[{video_audio_idx}:a]"
            else:
                # Source clip has no audio track: insert silence of the
                # same duration instead of failing the whole render.
                inputs.extend([
                    "-f", "lavfi",
                    "-t", f"{clip['duration']:.3f}",
                    "-i", "anullsrc=r=48000:cl=stereo",
                ])
                silent_idx = input_index
                input_index += 1
                audio_src = f"[{silent_idx}:a]"

            filters.append(
                f"{audio_src}aformat=sample_rates=48000:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
            concat_parts.append(f"[v{i}][a{i}]")
        else:
            concat_parts.append(f"[v{i}]")

    n = len(batch_clips)
    if audio_on:
        filters.append("".join(concat_parts) + f"concat=n={n}:v=1:a=1[outv][outa]")
    else:
        filters.append("".join(concat_parts) + f"concat=n={n}:v=1:a=0[outv]")

    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-nostats", "-loglevel", "error",
        "-progress", "pipe:1",
    ]
    cmd.extend(inputs)
    cmd.extend(["-filter_complex", filter_complex, "-map", "[outv]"])

    if audio_on:
        cmd.extend(["-map", "[outa]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
    else:
        cmd.append("-an")

    cmd.extend([
        "-c:v", "av1_nvenc",
        "-preset", NVENC_PRESET,
        "-cq", str(cq),
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-t", f"{batch_duration:.3f}",
        "-movflags", "+faststart",
        str(segment_path),
    ])
    return cmd


def write_concat_list(segment_paths, list_path):
    """Write an FFmpeg concat-demuxer list file referencing each segment."""
    lines = []
    for p in segment_paths:
        # Concat demuxer paths are single-quoted; escape any literal quotes.
        safe = str(p).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_concat_command(list_path, output_path):
    """Losslessly join the already-encoded segment files (no re-encode)."""
    return [
        "ffmpeg", "-hide_banner", "-y",
        "-nostats", "-loglevel", "error",
        "-progress", "pipe:1",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]


def _run_ffmpeg_with_progress(cmd, msg_queue, stop_event, progress_offset, total_duration, log_prefix=""):
    """Run one FFmpeg command, streaming its -progress output into
    msg_queue as ('progress', current, total) updates, offset by
    `progress_offset` seconds so a multi-batch render shows continuous
    overall progress. Returns (returncode, stopped)."""

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
        creationflags=CREATIONFLAGS,
    )
    msg_queue.put(("proc", process))

    stopped = False
    for line in process.stdout:
        line = line.strip()
        if line:
            if line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                    current = progress_offset + us / 1_000_000
                    msg_queue.put(("progress", current, total_duration))
                except ValueError:
                    pass
            elif line.startswith(("out_time=", "progress=", "frame=", "fps=",
                                  "bitrate=", "total_size=", "speed=",
                                  "stream_", "dup_frames=", "drop_frames=")):
                pass  # routine progress fields we don't need to log
            else:
                msg_queue.put(("log", f"  {log_prefix}ffmpeg: {line}"))

        if stop_event.is_set():
            process.terminate()
            stopped = True
            break

    process.wait()
    return process.returncode, stopped


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# =====================================================================
# BACKGROUND WORKER (runs in its own thread)
# =====================================================================

def run_generation(params, msg_queue, stop_event):
    temp_dir = None
    try:
        source_dir = Path(params["source_dir"])
        output_dir = Path(params["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        ext_set = params["extensions"]

        if not source_dir.is_dir():
            msg_queue.put(("error", f"Source folder does not exist:\n{source_dir}"))
            return

        files = sorted(
            f for f in source_dir.iterdir()
            if f.is_file() and f.suffix.lower() in ext_set
        )

        if not files:
            msg_queue.put(("error", "No source videos found matching the selected type."))
            return

        msg_queue.put(("log", f"Found {len(files)} candidate file(s). Probing with ffprobe..."))

        videos = []
        for f in files:
            if stop_event.is_set():
                msg_queue.put(("stopped", None))
                return
            try:
                duration, width, height, has_audio = probe_video(f)
                if duration < params["min_clip"]:
                    msg_queue.put(("log", f"  Skipping {f.name}: shorter than minimum clip length."))
                    continue
                videos.append({
                    "file": f,
                    "duration": duration,
                    "width": width,
                    "height": height,
                    "has_audio": has_audio,
                    "used": [],
                })
                msg_queue.put((
                    "log",
                    f"  {f.name} | {duration:.1f}s | {width}x{height} | "
                    f"audio={'yes' if has_audio else 'no'}"
                ))
            except Exception as e:
                msg_queue.put(("log", f"  Skipping {f.name}: {e}"))

        if not videos:
            msg_queue.put(("error", "No usable videos remained after probing."))
            return

        fps = params["fps"]
        total_duration = params["total_duration"]

        target_frames = int(round(total_duration * fps))
        min_frames = max(1, int(round(params["min_clip"] * fps)))
        max_frames = max(min_frames, int(round(params["max_clip"] * fps)))

        msg_queue.put(("log", ""))
        if params.get("num_clips"):
            msg_queue.put(("log", f"Planning {params['num_clips']} clips (fixed count)..."))
        else:
            msg_queue.put(("log", "Planning random clip arrangement..."))

        clips = build_clip_plan(
            videos, target_frames, min_frames, max_frames, fps, msg_queue, stop_event,
            num_clips=params.get("num_clips"),
        )
        if clips is None:
            return  # error or stop already reported

        msg_queue.put(("log", f"Planned {len(clips)} clips covering {format_seconds(total_duration)}."))

        width, height = params["resolution"]
        audio_on = params["audio_on"]
        cq = params["cq"]

        batches = list(_chunk(clips, BATCH_SIZE))
        msg_queue.put(("log", f"Rendering in {len(batches)} batch(es) of up to {BATCH_SIZE} clips each..."))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        temp_dir = output_dir / f"_render_tmp_{timestamp}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        msg_queue.put(("status", "Rendering"))

        segment_paths = []
        progress_offset = 0.0

        for batch_idx, batch_clips in enumerate(batches):
            if stop_event.is_set():
                msg_queue.put(("stopped", None))
                return

            batch_duration = sum(c["duration"] for c in batch_clips)
            segment_path = temp_dir / f"segment_{batch_idx:04d}.mp4"

            msg_queue.put((
                "log",
                f"  Batch {batch_idx + 1}/{len(batches)}: "
                f"{len(batch_clips)} clips, {batch_duration:.1f}s..."
            ))

            cmd = build_batch_command(
                batch_clips, width, height, fps, audio_on, cq, segment_path, batch_duration
            )

            returncode, stopped = _run_ffmpeg_with_progress(
                cmd, msg_queue, stop_event, progress_offset, total_duration,
                log_prefix=f"batch {batch_idx + 1}: "
            )

            if stopped or stop_event.is_set():
                msg_queue.put(("stopped", None))
                return

            if returncode != 0:
                msg_queue.put(("error", f"FFmpeg failed on batch {batch_idx + 1} (exit code {returncode})."))
                return

            segment_paths.append(segment_path)
            progress_offset += batch_duration

        msg_queue.put(("log", ""))
        msg_queue.put(("log", "Joining rendered segments..."))
        msg_queue.put(("status", "Finalizing"))

        list_path = temp_dir / "concat_list.txt"
        write_concat_list(segment_paths, list_path)

        concat_cmd = build_concat_command(list_path, params["output_path"])
        returncode, stopped = _run_ffmpeg_with_progress(
            concat_cmd, msg_queue, stop_event, total_duration, total_duration, log_prefix="merge: "
        )

        if stopped or stop_event.is_set():
            _remove_partial(params["output_path"])
            msg_queue.put(("stopped", None))
            return

        if returncode != 0:
            _remove_partial(params["output_path"])
            msg_queue.put(("error", f"FFmpeg failed while joining segments (exit code {returncode})."))
            return

        msg_queue.put(("done", str(params["output_path"])))

    except Exception as e:
        msg_queue.put(("error", f"Unexpected error: {e}"))

    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _remove_partial(path):
    try:
        path = Path(path)
        if path.exists():
            path.unlink()
    except Exception:
        pass


# =====================================================================
# GUI
# =====================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(760, 720)

        self.msg_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.current_process = None
        self.render_start_time = None

        self._build_ui()
        self._check_dependencies()
        self.after(100, self._poll_queue)

    # -----------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        root_frame = ttk.Frame(self, padding=10)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(1, weight=1)

        row = 0

        # ---------------- Source ----------------
        source_box = ttk.LabelFrame(root_frame, text="Source", padding=8)
        source_box.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        source_box.columnconfigure(1, weight=1)

        ttk.Label(source_box, text="Source Folder:").grid(row=0, column=0, sticky="w")
        self.source_dir_var = tk.StringVar()
        ttk.Entry(source_box, textvariable=self.source_dir_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(source_box, text="Browse...", command=self._browse_source).grid(row=0, column=2)

        ttk.Label(source_box, text="Source Type:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.source_type_var = tk.StringVar(value="Auto / All Videos")
        ttk.Combobox(
            source_box, textvariable=self.source_type_var,
            values=list(SOURCE_TYPES.keys()), state="readonly", width=25
        ).grid(row=1, column=1, sticky="w", pady=(6, 0))

        row += 1

        # ---------------- Output ----------------
        output_box = ttk.LabelFrame(root_frame, text="Output", padding=8)
        output_box.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        output_box.columnconfigure(1, weight=1)

        ttk.Label(output_box, text="Output Folder:").grid(row=0, column=0, sticky="w")
        self.output_dir_var = tk.StringVar()
        ttk.Entry(output_box, textvariable=self.output_dir_var).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(output_box, text="Browse...", command=self._browse_output).grid(row=0, column=2)

        ttk.Label(output_box, text="Filename / Project Name:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.output_name_var = tk.StringVar(value="random_video")
        ttk.Entry(output_box, textvariable=self.output_name_var).grid(row=1, column=1, sticky="ew", padx=4, pady=(6, 0))

        row += 1

        # ---------------- Video Settings ----------------
        video_box = ttk.LabelFrame(root_frame, text="Video Settings", padding=8)
        video_box.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        for c in range(4):
            video_box.columnconfigure(c, weight=1)

        ttk.Label(video_box, text="Aspect Ratio:").grid(row=0, column=0, sticky="w")
        self.aspect_var = tk.StringVar(value="16:9 - YouTube Landscape")
        ttk.Combobox(
            video_box, textvariable=self.aspect_var,
            values=list(ASPECT_RATIOS.keys()), state="readonly", width=26
        ).grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(video_box, text="Quality:").grid(row=0, column=2, sticky="w")
        self.quality_var = tk.StringVar(value="High")
        ttk.Combobox(
            video_box, textvariable=self.quality_var,
            values=list(QUALITY_CQ.keys()), state="readonly", width=14
        ).grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(video_box, text="Output FPS:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.fps_var = tk.StringVar(value=DEFAULT_FPS)
        ttk.Entry(video_box, textvariable=self.fps_var, width=10).grid(row=1, column=1, sticky="w", padx=4, pady=(6, 0))

        row += 1

        # ---------------- Audio ----------------
        audio_box = ttk.LabelFrame(root_frame, text="Audio", padding=8)
        audio_box.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)

        self.audio_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            audio_box, text="Audio ON (keep each clip's original audio, "
                             "48kHz / stereo / AAC ~192kbps; silence inserted if a clip has none)",
            variable=self.audio_var
        ).grid(row=0, column=0, sticky="w")

        row += 1

        # ---------------- Clip / Duration Settings ----------------
        clip_box = ttk.LabelFrame(root_frame, text="Clip & Final Duration", padding=8)
        clip_box.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)

        ttk.Label(clip_box, text="Min Clip (sec):").grid(row=0, column=0, sticky="w")
        self.min_clip_var = tk.StringVar(value=DEFAULT_MIN_CLIP)
        ttk.Entry(clip_box, textvariable=self.min_clip_var, width=8).grid(row=0, column=1, sticky="w", padx=4)

        ttk.Label(clip_box, text="Max Clip (sec):").grid(row=0, column=2, sticky="w")
        self.max_clip_var = tk.StringVar(value=DEFAULT_MAX_CLIP)
        ttk.Entry(clip_box, textvariable=self.max_clip_var, width=8).grid(row=0, column=3, sticky="w", padx=4)

        ttk.Label(clip_box, text="Final Duration (MM:SS):").grid(row=0, column=4, sticky="w")
        self.duration_var = tk.StringVar(value=DEFAULT_FINAL_DURATION)
        ttk.Entry(clip_box, textvariable=self.duration_var, width=10).grid(row=0, column=5, sticky="w", padx=4)

        ttk.Label(clip_box, text="Number of Clips (1-100, blank = auto):").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        self.num_clips_var = tk.StringVar(value="")
        ttk.Entry(clip_box, textvariable=self.num_clips_var, width=8).grid(
            row=1, column=3, sticky="w", padx=4, pady=(6, 0)
        )

        row += 1

        # ---------------- Buttons ----------------
        button_box = ttk.Frame(root_frame)
        button_box.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)

        self.generate_btn = ttk.Button(button_box, text="GENERATE", command=self._on_generate)
        self.generate_btn.pack(side="left", padx=4)

        self.stop_btn = ttk.Button(button_box, text="STOP", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        row += 1

        # ---------------- Progress ----------------
        progress_box = ttk.LabelFrame(root_frame, text="Progress", padding=8)
        progress_box.grid(row=row, column=0, columnspan=3, sticky="ew", **pad)
        progress_box.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(progress_box, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

        self.progress_bar = ttk.Progressbar(progress_box, orient="horizontal", mode="determinate", maximum=100)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        self.progress_text_var = tk.StringVar(value="")
        ttk.Label(progress_box, textvariable=self.progress_text_var).grid(row=2, column=0, sticky="w", pady=(2, 0))

        row += 1

        # ---------------- Log ----------------
        log_box = ttk.LabelFrame(root_frame, text="Log", padding=8)
        log_box.grid(row=row, column=0, columnspan=3, sticky="nsew", **pad)
        root_frame.rowconfigure(row, weight=1)
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_box, height=16, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    # -----------------------------------------------------------------
    # Dependency check
    # -----------------------------------------------------------------
    def _check_dependencies(self):
        missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
        if missing:
            self._log(
                "WARNING: could not find "
                + " and ".join(missing)
                + " on PATH. Install FFmpeg (with ffprobe) and add it to your "
                  "Windows PATH before generating."
            )

    # -----------------------------------------------------------------
    # Browse buttons
    # -----------------------------------------------------------------
    def _browse_source(self):
        path = filedialog.askdirectory(title="Select Source Folder")
        if path:
            self.source_dir_var.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select Output Folder")
        if path:
            self.output_dir_var.set(path)

    # -----------------------------------------------------------------
    # Generate / Stop
    # -----------------------------------------------------------------
    def _on_generate(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return

        try:
            params = self._collect_params()
        except ValueError as e:
            messagebox.showerror("Invalid settings", str(e))
            return

        self._clear_log()
        self._log(f"Output will be saved to:\n  {params['output_path']}")
        self.status_var.set("Scanning source folder...")
        self.progress_bar["value"] = 0
        self.progress_text_var.set("")

        self.stop_event.clear()
        self.current_process = None

        self.generate_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        self.worker_thread = threading.Thread(
            target=run_generation, args=(params, self.msg_queue, self.stop_event), daemon=True
        )
        self.worker_thread.start()

    def _on_stop(self):
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        self._log("Stop requested by user...")
        self.status_var.set("Stopping...")
        self.stop_event.set()
        if self.current_process is not None:
            try:
                self.current_process.terminate()
            except Exception:
                pass
        self.stop_btn.configure(state="disabled")

    def _collect_params(self):
        source_dir = self.source_dir_var.get().strip()
        output_dir = self.output_dir_var.get().strip()

        if not source_dir:
            raise ValueError("Please select a source folder.")
        if not output_dir:
            raise ValueError("Please select an output folder.")

        source_type = self.source_type_var.get()
        extensions = SOURCE_TYPES[source_type]

        aspect_label = self.aspect_var.get()
        resolution = ASPECT_RATIOS[aspect_label]

        quality_label = self.quality_var.get()
        cq = QUALITY_CQ[quality_label]

        try:
            fps = float(self.fps_var.get().strip())
            if fps <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("Output FPS must be a positive number.")

        try:
            min_clip = float(self.min_clip_var.get().strip())
            max_clip = float(self.max_clip_var.get().strip())
            if min_clip <= 0 or max_clip <= 0:
                raise ValueError
            if min_clip > max_clip:
                raise ValueError("Min Clip must be less than or equal to Max Clip.")
        except ValueError as e:
            msg = str(e) if str(e) else "Clip durations must be positive numbers."
            raise ValueError(msg)

        total_duration = parse_duration_to_seconds(self.duration_var.get())

        num_clips_text = self.num_clips_var.get().strip()
        num_clips = None
        if num_clips_text:
            try:
                num_clips = int(num_clips_text)
            except ValueError:
                raise ValueError("Number of Clips must be a whole number.")
            if not (1 <= num_clips <= 100):
                raise ValueError("Number of Clips must be between 1 and 100.")

        base_name = safe_filename_part(self.output_name_var.get())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{base_name}_{timestamp}.mp4"
        output_path = Path(output_dir) / filename

        return {
            "source_dir": source_dir,
            "output_dir": output_dir,
            "extensions": extensions,
            "resolution": resolution,
            "cq": cq,
            "audio_on": bool(self.audio_var.get()),
            "fps": fps,
            "num_clips": num_clips,
            "min_clip": min_clip,
            "max_clip": max_clip,
            "total_duration": total_duration,
            "output_path": output_path,
        }

    # -----------------------------------------------------------------
    # Queue polling / logging
    # -----------------------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]

                if kind == "log":
                    self._log(item[1])
                elif kind == "status":
                    self.status_var.set(item[1])
                    if item[1] == "Rendering":
                        self.render_start_time = time.time()
                elif kind == "proc":
                    self.current_process = item[1]
                elif kind == "progress":
                    current, total = item[1], item[2]
                    pct = 0 if total <= 0 else min(100.0, (current / total) * 100.0)
                    self.progress_bar["value"] = pct
                    self.progress_text_var.set(
                        f"{self.status_var.get()}... {current:.1f}s / {total:.1f}s   ({pct:.0f}%)"
                    )
                elif kind == "error":
                    self._log(f"ERROR: {item[1]}")
                    self.status_var.set("Error")
                    self._reset_buttons()
                    messagebox.showerror("Generation failed", item[1])
                elif kind == "stopped":
                    self._log("Generation stopped.")
                    self.status_var.set("Ready")
                    self.progress_bar["value"] = 0
                    self.progress_text_var.set("")
                    self._reset_buttons()
                elif kind == "done":
                    self.progress_bar["value"] = 100
                    self.status_var.set("Done")
                    self._log(f"DONE! Output saved to:\n  {item[1]}")
                    self._reset_buttons()
                    messagebox.showinfo("Generation complete", f"Video saved to:\n{item[1]}")
        except queue.Empty:
            pass

        self.after(150, self._poll_queue)

    def _reset_buttons(self):
        self.generate_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.current_process = None

    def _log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
