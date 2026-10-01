# Random Video Generator - GUI Edition

A Tkinter GUI wrapped around your existing random-clip / FFmpeg pipeline, so
you never have to edit paths or settings inside the Python file again.

## Files

- `random_video_generator_gui.py` - the GUI application
- `START_RANDOM_VIDEO_GUI.bat` - double-click launcher (Windows)

## Requirements

1. **Python 3.8+** installed and on your Windows PATH (so the `python`
   command works from any folder).
2. **FFmpeg and FFprobe** installed and on your Windows PATH.
   - Test by opening Command Prompt and running `ffmpeg -version` and
     `ffprobe -version`. Both must work from any folder.
3. An **NVIDIA GPU with AV1 NVENC support** (RTX 40-series or newer, or any
   GPU whose driver exposes `av1_nvenc`), with a recent NVIDIA driver
   installed.
4. No third-party Python packages are required - only the standard
   library (`tkinter`, `subprocess`, `threading`, etc.).

## Usage

1. Put `random_video_generator_gui.py` and `START_RANDOM_VIDEO_GUI.bat` in
   the same folder.
2. Double-click `START_RANDOM_VIDEO_GUI.bat`.
3. In the GUI:
   - **Source Folder** - browse to the folder containing your source videos.
   - **Source Type** - `Auto / All Videos` or a specific extension.
   - **Output Folder** - where the finished MP4 should be saved.
   - **Filename / Project Name** - a base name; a timestamp is always
     appended automatically so previous renders are never overwritten
     (e.g. `myproject_20260926_083015.mp4`).
   - **Aspect Ratio** - choose the target frame shape. Video is scaled and
     center-cropped to fill it exactly - it is never stretched or distorted.
   - **Quality** - `High` / `Very High` / `Maximum` (maps to NVENC CQ
     25 / 20 / 17, preset `p5`).
   - **Output FPS** - defaults to 25.
   - **Audio ON/OFF** - when ON, each clip keeps its own source audio
     (converted to 48kHz stereo AAC ~192kbps); clips with no audio track
     get silence inserted automatically instead of failing the render.
     When OFF, the output has no audio track at all.
   - **Min/Max Clip (sec)** - length range for each randomly chosen segment
     (defaults 2.0-7.8 seconds).
   - **Final Duration (MM:SS)** - total output length (default `10:30`).
4. Click **GENERATE**. The log area shows every detected source file, the
   planned clip arrangement, and live FFmpeg render progress
   (`Rendering... 384.2s / 630.0s`) with a progress bar.
5. Click **STOP** at any time to safely cancel - FFmpeg is terminated and
   any incomplete output file is deleted. The GUI returns to `Ready`.

## Behavior preserved from the original script

- Any number of source videos is supported (not hardcoded).
- Random clip lengths are generated to exactly total the requested final
  duration, using whole frames at the chosen FPS.
- No two consecutive clips come from the same source video.
- The same portion of a source video is **never** reused - a per-video list
  of used time ranges is tracked, and overlaps are rejected.
- One single FFmpeg `filter_complex` graph does all scaling/cropping/
  concatenation - no intermediate files are written.

## Troubleshooting

- **"could not find ffmpeg/ffprobe on PATH"** warning in the log: install
  FFmpeg and make sure its `bin` folder is in your Windows PATH environment
  variable, then restart the GUI.
- **"Not enough unused footage..."** error: your source folder doesn't have
  enough total unique footage for the requested final duration at the
  chosen clip lengths. Add more/longer source videos, shorten the final
  duration, or reduce the max clip length.
- **FFmpeg exits with an error code**: check your GPU driver supports
  `av1_nvenc`, and that FFmpeg was built with NVENC support
  (`ffmpeg -encoders | findstr nvenc`).
