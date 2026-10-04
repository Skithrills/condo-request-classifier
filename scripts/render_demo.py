"""Compose an edited demo from authentic browser captures and local narration.

Run after narrate_demo.ps1 and capturing the five documented app states.
Dependencies: Pillow, NumPy, imageio-ffmpeg. No browser control is performed here.
"""

import json
import math
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "demo"
STORY = json.loads((ROOT / "scripts/demo_storyboard.json").read_text(encoding="utf-8"))
W, H, FPS = 1920, 1080, 24
BG = "#0B202B"
PANEL = "#122E39"
MINT = "#76E1BD"
WHITE = "#F5F8F5"
MUTED = "#A7BFC4"
FONT_ROOT = Path("C:/Windows/Fonts")


def font(size, bold=False):
    return ImageFont.truetype(str(FONT_ROOT / ("seguisb.ttf" if bold else "segoeui.ttf")), size)


def wrap(text, face, width):
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if current and draw.textlength(candidate, font=face) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def text_block(draw, xy, text, size, color=WHITE, width=1600, bold=False, spacing=1.25):
    x, y = xy
    face = font(size, bold)
    for line in wrap(text, face, width):
        draw.text((x, y), line, font=face, fill=color)
        y += round(size * spacing)
    return y


def background():
    image = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(image)
    for x in range(0, W, 80):
        draw.line((x, 0, x, H), fill="#102831", width=1)
    for y in range(0, H, 80):
        draw.line((0, y, W, y), fill="#102831", width=1)
    draw.rectangle((0, 0, 12, H), fill=MINT)
    return image


def card(draw, box, label, value, accent=MINT):
    x, y, right, bottom = box
    draw.rounded_rectangle(box, radius=22, fill=PANEL, outline="#2B4E57", width=2)
    draw.rounded_rectangle((x + 24, y + 30, x + 30, bottom - 30), radius=3, fill=accent)
    text_block(draw, (x + 52, y + 26), label, 21, MUTED, width=right - x - 82)
    text_block(draw, (x + 52, y + 66), value, 34, width=right - x - 82, bold=True)


def scene_frame(scene, index):
    image = background()
    draw = ImageDraw.Draw(image)
    if scene["kind"] == "intro":
        text_block(draw, (100, 124), scene["eyebrow"], 24, MINT, bold=True)
        text_block(draw, (94, 250), scene["title"], 88, width=1000, bold=True, spacing=1.16)
        text_block(draw, (100, 498), "Condominium resident request classifier", 33, MUTED)
        draw.rounded_rectangle((100, 605, 897, 706), radius=18, fill="#163940")
        text_block(draw, (130, 635), "CLASSIFY   /   TRIAGE   /   REVIEW", 30, MINT, bold=True)
        text_block(draw, (100, 786), "Working local demo", 25, width=800, bold=True)
        text_block(draw, (100, 828), "Open-source tools. Synthetic demonstration data.", 25, MUTED)
        card(draw, (1150, 239, 1805, 394), "CATEGORY", "Maintenance")
        card(draw, (1150, 421, 1805, 576), "PRIORITY", "Normal")
        card(draw, (1150, 603, 1805, 758), "SUGGESTED TEAM", "Maintenance team")
    elif scene["kind"] == "outro":
        text_block(draw, (100, 122), scene["eyebrow"], 24, MINT, bold=True)
        text_block(draw, (95, 224), scene["title"], 76, bold=True, spacing=1.17)
        card(draw, (100, 488, 643, 699), "READY NOW", "Local classifier\nStreamlit demo")
        card(
            draw,
            (687, 488, 1230, 699),
            "INTEGRATION READY",
            "Ollama adapter\nLive test pending",
            "#F2CE86",
        )
        card(draw, (1274, 488, 1817, 699), "NEXT MILESTONE", "Real labels\nCalibrated scores")
        text_block(
            draw, (104, 777), "Streamlit  ·  scikit-learn  ·  LangChain  ·  LangGraph", 30, MUTED
        )
        text_block(
            draw, (104, 840), "Future scope: approved-document RAG, ticketing and agents", 28, MUTED
        )
    else:
        text_block(draw, (64, 39), scene["eyebrow"], 22, MINT, bold=True)
        text_block(draw, (60, 77), scene["title"], 48, bold=True)
        shot_path = OUT / "captures" / scene["capture"]
        shot = Image.open(shot_path).convert("RGB")
        if shot.size != (1280, 720):
            raise ValueError(f"Unexpected capture dimensions for {shot_path}: {shot.size}")
        shot = shot.resize((1376, 774), Image.Resampling.LANCZOS)
        draw.rounded_rectangle((60, 159, 1444, 967), radius=16, fill="#34505A")
        draw.rounded_rectangle((62, 161, 1442, 190), radius=12, fill="#1B3944")
        for x in (82, 100, 118):
            draw.ellipse((x, 170, x + 8, 178), fill="#87A5AD")
        draw.text((148, 161), "MANAGEMENT AGENT  /  LOCAL DEMONSTRATION", font=font(16), fill=MUTED)
        image.paste(shot, (64, 190))
        text_block(draw, (1490, 208), f"0{index} / 05", 24, MINT, bold=True)
        end = text_block(draw, (1486, 270), scene["callout_title"], 46, width=350, bold=True)
        line_y = end + 35
        for line in scene["callouts"]:
            draw.ellipse((1490, line_y + 12, 1500, line_y + 22), fill=MINT)
            line_y = text_block(draw, (1518, line_y), line, 27, width=315) + 28
        draw.line((1490, 737, 1817, 737), fill="#3B5962", width=2)
        text_block(draw, (1490, 771), scene["note"], 24, MUTED, width=320)
    return image


def timestamp(seconds):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def prepare_audio():
    timeline = []
    subtitles = []
    audio = bytearray()
    params = None
    total = 0.0
    for scene in STORY:
        chunks, cues = [], []
        elapsed = 0.45
        for number, sentence in enumerate(scene["narration"], 1):
            with wave.open(str(OUT / "audio" / f"{scene['id']}-{number}.wav"), "rb") as source:
                if params is None:
                    params = source.getparams()
                elif source.getparams()[:3] != params[:3]:
                    raise ValueError("Narration format changed between clips.")
                duration = source.getnframes() / source.getframerate()
                raw = source.readframes(source.getnframes())
            chunks.append((elapsed, raw))
            cues.append({"start": elapsed, "end": elapsed + duration, "text": sentence})
            subtitles.append(
                {"start": total + elapsed, "end": total + elapsed + duration, "text": sentence}
            )
            elapsed += duration + 0.18
        duration = math.ceil((elapsed + 0.50) * FPS) / FPS
        unit = params.nchannels * params.sampwidth
        raw_scene = bytearray(round(duration * params.framerate) * unit)
        for offset, raw in chunks:
            start = round(offset * params.framerate) * unit
            raw_scene[start : start + len(raw)] = raw
        audio.extend(raw_scene)
        timeline.append({**scene, "start": total, "duration": duration, "cues": cues})
        total += duration
    with wave.open(str(OUT / "narration.wav"), "wb") as output:
        output.setparams(params)
        output.writeframes(audio)
    (OUT / "captions.srt").write_text(
        "\n\n".join(
            f"{i}\n{timestamp(cue['start'])} --> {timestamp(cue['end'])}\n{cue['text']}"
            for i, cue in enumerate(subtitles, 1)
        )
        + "\n",
        encoding="utf-8",
    )
    (OUT / "timeline.json").write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    return timeline, total


def add_caption(image, text):
    if not text:
        return image
    frame = image.copy()
    draw = ImageDraw.Draw(frame)
    draw.rectangle((12, 976, W, 1075), fill="#08171F")
    face = font(29)
    lines = wrap(text, face, W - 170)
    if len(lines) > 2:
        raise ValueError(f"Caption does not fit: {text}")
    y = 992 if len(lines) == 2 else 1008
    for line in lines:
        width = draw.textlength(line, font=face)
        draw.text(((W - width) / 2, y), line, font=face, fill=WHITE)
        y += 36
    return frame


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stills").mkdir(exist_ok=True)
    timeline, total = prepare_audio()
    bases = [scene_frame(scene, i) for i, scene in enumerate(timeline)]
    for scene, base in zip(timeline, bases, strict=True):
        base.save(OUT / "stills" / f"{scene['id']}.png")
    bases[0].save(OUT / "poster.png")
    silent = OUT / "demo-silent.mp4"
    writer = imageio_ffmpeg.write_frames(
        str(silent),
        (W, H),
        fps=FPS,
        codec="libx264",
        pix_fmt_in="rgb24",
        pix_fmt_out="yuv420p",
        macro_block_size=8,
        output_params=["-crf", "18", "-preset", "fast"],
        ffmpeg_log_level="error",
    )
    writer.send(None)
    previous = None
    for i, (scene, base) in enumerate(zip(timeline, bases, strict=True)):
        caption_frames = {"": base}
        for cue in scene["cues"]:
            caption_frames[cue["text"]] = add_caption(base, cue["text"])
        for number in range(round(scene["duration"] * FPS)):
            local_time = number / FPS
            caption = next(
                (c["text"] for c in scene["cues"] if c["start"] <= local_time < c["end"]), ""
            )
            frame = caption_frames[caption].copy()
            if previous is not None and local_time < 0.4:
                frame = Image.blend(previous, frame, local_time / 0.4)
            draw = ImageDraw.Draw(frame)
            progress = (scene["start"] + local_time) / total
            draw.rectangle((12, H - 5, 12 + round((W - 12) * progress), H), fill=MINT)
            writer.send(np.asarray(frame))
        previous = base
        print(
            f"Rendered scene {i + 1}/{len(timeline)}: {scene['title'].replace(chr(10), ' ')}",
            flush=True,
        )
    writer.close()
    final = OUT / "Management_Agent_Classifier_Demo.mp4"
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(silent),
            "-i",
            str(OUT / "narration.wav"),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=7",
            "-movflags",
            "+faststart",
            "-shortest",
            str(final),
        ],
        check=True,
    )
    print(f"Created {final} ({total:.2f}s, {W}x{H}, {FPS} fps)", flush=True)


if __name__ == "__main__":
    main()
