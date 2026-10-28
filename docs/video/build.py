#!/usr/bin/env python3
"""One command from docs/VIDEO-SCRIPT.md + docs/video/record.spec.ts to the finished review cut.

    python3 docs/video/build.py [--name llm-gateway] [--deliver DIR] [--skip-record]

Steps (all local, nothing is downloaded):
  1. narration  : docs/video/narration.txt (written for listening; one `## <scene id>` section per scene) is spoken by Kokoro, a local
                  neural voice (videos/tts/narrate.py, voice bf_emma, respellings in videos/tts/pronounce.json + docs/video/pronounce.json);
                  out/audio/scene-<id>.wav and narration.json (scene durations + per-sentence cues). The NARRATION column of
                  docs/VIDEO-SCRIPT.md is regenerated from narration.txt on every build, so script and voice cannot diverge.
  2. record     : `npx playwright test` with SCENE_DURATIONS, so every scene lasts at least (audio + 0.7 s); the recorder writes
                  out/timings.json (scene start times) next to the webm
  3. assemble   : video trimmed to the title card, narration placed at the scene starts, H.264 yuv420p + AAC, +faststart -> out/<name>.mp4
  4. captions   : out/<name>.srt, timed from the measured per-sentence audio

Environment (optional):
  BUILD_OUT                 output directory (default: docs/video/out)
  TTS_DIR                   directory holding narrate.py and its .venv (default: ../videos/tts next to the project)
  BUILD_TMP                 scratch directory for Playwright's ffmpeg shim and the throwaway Postgres (default: docs/video/out/tmp)
  PLAYWRIGHT_BROWSERS_PATH  use a ready browsers directory instead of the generated shim
  E2E_HARNESS, E2E_PG_BIN   see record.spec.ts
  LLMGW_PORT_RANGE          "lo-hi" (default 41200-41299): every port the recorder opens is taken from this range
"""
import argparse, json, os, re, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SCRIPT = ROOT / "docs" / "VIDEO-SCRIPT.md"
OUT = Path(os.environ.get("BUILD_OUT") or HERE / "out")
NARRATION = HERE / "narration.txt"
TTS = Path(os.environ.get("TTS_DIR") or ROOT.parent / "videos" / "tts")
VOICE = "bf_emma"
MARGIN = 0.7          # a scene lasts at least audio + MARGIN seconds
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def duration(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       check=True, capture_output=True, text=True)
    return float(r.stdout.strip())


def read_narration():
    """{scene id: [paragraph text, ...]} from narration.txt (pause markers kept as `[pause x]` / `...` lines)."""
    parts = re.split(r"(?m)^##\s+(\S+)(?:[ \t]+\S+)?[ \t]*$", NARRATION.read_text())
    return {sid: body.strip() for sid, body in zip(parts[1::2], parts[2::2])}


def spoken_cell(body):
    """The script-table form of a scene's narration: sentences in order, deliberate pauses as [pause x], beats dropped."""
    text = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s == "...":
            continue
        text.append(s)
    return " ".join(text)


def mirror_script():
    """Rewrite the NARRATION column of docs/VIDEO-SCRIPT.md from narration.txt (table format kept)."""
    cells = {sid: spoken_cell(b) for sid, b in read_narration().items()}
    out, seen = [], set()
    for line in SCRIPT.read_text().splitlines():
        m = re.match(r"^\|\s*(\d+[a-z]?)\s*\|", line)
        if m and m.group(1) in cells:
            c = re.split(r"(?<!\\)\|", line)
            if len(c) >= 7:
                c[4] = ' "' + cells[m.group(1)].replace("|", "\\|") + '" '
                line = "|".join(c)
                seen.add(m.group(1))
        out.append(line)
    missing = set(cells) - seen
    if missing:
        sys.exit(f"narration.txt scenes missing from the script table: {sorted(missing)}")
    SCRIPT.write_text("\n".join(out) + "\n")


def narration():
    mirror_script()
    audio = OUT / "audio"
    shutil.rmtree(audio, ignore_errors=True)
    audio.mkdir(parents=True)
    py = TTS / ".venv" / "bin" / "python"
    run([py, TTS / "narrate.py", "scenes", NARRATION, audio, "--voice", VOICE, "--pronounce", HERE / "pronounce.json"])
    raw = json.loads((audio / "narration.json").read_text())
    info = {}
    for sid, v in raw.items():
        info[sid] = {"audio": v["duration"], "offsets": [c["start"] for c in v["cues"]], "cues": v["cues"]}
        print(f"scene {sid}: {v['duration']:.1f} s, {len(v['cues'])} sentences")
    (audio / "durations.json").write_text(json.dumps(info, indent=1))
    # the recorder reads a flat {scene: seconds the scene must last at least}
    (audio / "scene-min.json").write_text(json.dumps({k: round(v["audio"] + MARGIN, 3) for k, v in info.items()}))
    return info


def pw_env():
    env = dict(os.environ)
    tmp = Path(env.get("BUILD_TMP") or OUT / "tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    env.setdefault("E2E_SCRATCH", str(tmp))
    env.setdefault("LLMGW_PORT_RANGE", "41200-41299")
    if "PLAYWRIGHT_BROWSERS_PATH" not in env:
        # Playwright's own ffmpeg build may be missing: point the revision it wants at the system ffmpeg (no download)
        spec = json.loads((HERE / "node_modules/playwright-core/browsers.json").read_text())
        rev = next(b["revision"] for b in spec["browsers"] if b["name"] == "ffmpeg")
        cache = Path.home() / "Library/Caches/ms-playwright"
        if cache.exists() and not (cache / f"ffmpeg-{rev}" / "INSTALLATION_COMPLETE").exists():
            shim = tmp / "browsers"
            (shim / f"ffmpeg-{rev}").mkdir(parents=True, exist_ok=True)
            for d in cache.iterdir():
                if d.is_dir() and not d.name.startswith(".") and not (shim / d.name).exists():
                    (shim / d.name).symlink_to(d)
            link = shim / f"ffmpeg-{rev}" / "ffmpeg-mac"
            if not link.exists():
                link.symlink_to(FFMPEG)
            for f in ("INSTALLATION_COMPLETE", "DEPENDENCIES_VALIDATED"):
                (shim / f"ffmpeg-{rev}" / f).touch()
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(shim)
    return env


def record():
    env = pw_env()
    env["SCENE_DURATIONS"] = str(OUT / "audio" / "scene-min.json")
    env["SCENE_INFO"] = str(OUT / "audio" / "durations.json")
    env["TIMINGS_OUT"] = str(OUT / "timings.json")
    env.pop("PACE", None)
    env.pop("NOVIDEO", None)
    shutil.rmtree(OUT / "pw", ignore_errors=True)
    (OUT / "timings.json").unlink(missing_ok=True)
    run(["npx", "playwright", "test", "-c", "playwright.config.ts", "--output", OUT / "pw"], cwd=HERE, env=env)
    return last_webm()


def last_webm():
    webms = sorted((OUT / "pw").rglob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not webms:
        sys.exit("no video produced")
    return webms[-1]


def first_dark(webm, thresh=60):
    """Video time of the first frame whose average luma is below thresh (the dark title card = scene 1 start)."""
    out = subprocess.run([FFMPEG, "-v", "info", "-i", str(webm), "-vf",
                          "fps=25,scale=64:36,format=gray,signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    t = None
    for line in out.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
        m = re.search(r"YAVG=([\d.]+)", line)
        if m and t is not None and float(m.group(1)) < thresh:
            return t
    sys.exit("title card not found in video")


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def chunks(text, limit=84):
    """Split a long sentence into caption-sized pieces at word boundaries."""
    if len(text) <= limit:
        return [text]
    n = -(-len(text) // limit)
    target = len(text) / n
    out, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > target * 1.1:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out


def update_times(rel, total):
    """Refresh the time column of the script table from the measured scene starts."""
    ids = sorted(rel)
    fmt = lambda t: f"{int(t) // 60}:{int(t) % 60:02d}"
    out = []
    for line in SCRIPT.read_text().splitlines():
        m = re.match(r"^\|\s*(\d+)\s*\|", line)
        if m and int(m.group(1)) in rel:
            n = int(m.group(1))
            end = rel[n + 1] if n + 1 in rel else total
            c = re.split(r"(?<!\\)\|", line)
            c[2] = f" {fmt(rel[n])} - {fmt(end)} "
            line = "|".join(c)
        out.append(line)
    SCRIPT.write_text("\n".join(out) + "\n")


def assemble(webm, info, name):
    timing = json.loads((OUT / "timings.json").read_text())
    starts = {int(k): v for k, v in timing["scene_start_ms"].items()}
    card_wall = starts[1]
    v0 = first_dark(webm)               # video time of the title card = scene 1 start
    rel = {n: (t - card_wall) / 1000 for n, t in starts.items()}
    total = (timing["end_ms"] - card_wall) / 1000
    print(f"video offset {v0:.2f} s, total {total:.1f} s")
    update_times(rel, total)
    cues, idx = [], 1
    for n in sorted(info, key=int):
        t = rel[int(n)]
        for c in info[n]["cues"]:
            pieces = chunks(c["text"])
            tot = sum(len(p) for p in pieces)
            u, dur = t + c["start"], c["end"] - c["start"]
            for p in pieces:
                d = dur * len(p) / tot
                cues.append(f"{idx}\n{srt_time(u)} --> {srt_time(u + d)}\n{p}\n")
                idx += 1
                u += d
    srt = OUT / f"{name}.srt"
    srt.write_text("\n".join(cues))
    inputs, filt = [], []
    for i, n in enumerate(sorted(info, key=int)):
        inputs += ["-i", OUT / "audio" / f"scene-{n}.wav"]
        ms = int(rel[int(n)] * 1000)
        filt.append(f"[{i + 1}:a]adelay={ms}|{ms}[a{i}]")
    k = len(info)
    mix = "".join(f"[a{i}]" for i in range(k)) + f"amix=inputs={k}:normalize=0:dropout_transition=0,apad,atrim=0:{total:.3f}[aout]"
    mp4 = OUT / f"{name}.mp4"
    run([FFMPEG, "-v", "error", "-y", "-ss", f"{v0:.3f}", "-t", f"{total:.3f}", "-i", webm, *inputs,
         "-i", srt,
         "-filter_complex", ";".join(filt) + ";" + mix + ";[0:v]fps=25,scale=out_range=tv,format=yuv420p[v]",
         "-map", "[v]", "-map", "[aout]", "-map", f"{len(info) + 1}:s", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", "-t", f"{total:.3f}", mp4])
    print("wrote", mp4, f"({duration(mp4):.1f} s)")
    return mp4, srt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="llm-gateway")
    ap.add_argument("--deliver", help="also copy the mp4 and srt into this directory")
    ap.add_argument("--skip-record", action="store_true", help="re-assemble from the last recording in out/pw")
    ap.add_argument("--narration-only", action="store_true", help="only synthesise audio and print durations")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if a.skip_record:
        info = {k: v for k, v in json.loads((OUT / "audio" / "durations.json").read_text()).items()}
        webm = last_webm()
    else:
        info = narration()
        if a.narration_only:
            return
        webm = record()
    mp4, srt = assemble(webm, info, a.name)
    if a.deliver:
        d = Path(a.deliver)
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(mp4, d / mp4.name)
        shutil.copy(srt, d / srt.name)
        print("delivered to", d)


if __name__ == "__main__":
    main()
