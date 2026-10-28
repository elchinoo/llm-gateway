# Tutorial video: build and review cut

`docs/VIDEO-SCRIPT.md` is the script, `record.spec.ts` the recorder (real Chrome, 1920x1080, Playwright video), `build.py` turns both into the
finished file with narration and captions.

```bash
cd docs/video
npm install                                   # the project's own devDependencies (@playwright/test); needs Google Chrome
python3 build.py --deliver ../../../videos/review   # script + recorder -> out/llm-gateway.mp4 and .srt, copied to the review folder
```

Needs: the local Kokoro text-to-speech tool `../../../videos/tts` (`narrate.py` and its `.venv`; override with `TTS_DIR`), `ffmpeg`/`ffprobe` on PATH (Homebrew is fine), Node, the sibling `ecosystem/e2e` harness and a PostgreSQL 17
with pgvector (see `record.spec.ts`), and `../../.venv` with the gateway installed. Nothing is downloaded; no live service or real credential is used.

What `build.py` does:

1. **Narration.** `narration.txt` is the spoken text, written for listening (short sentences, one `## <scene id>` section per scene of
   VIDEO-SCRIPT.md). Blank line = pause, a line `...` = short beat, `[pause 1.5]` = deliberate long pause over something happening on screen.
   `videos/tts/narrate.py scenes` speaks it with Kokoro (local neural voices; nothing leaves the machine; the voice is set per scene in the section header, e.g. `## 3 bm_george`, default `bf_emma`; one voice per topic, alternating female and male) and writes
   `out/audio/scene-<id>.wav` plus `narration.json` (scene durations and per-sentence cues). Pronunciation respellings live in the shared
   `videos/tts/pronounce.json` and the project's `pronounce.json` (spoken text only; captions keep the written form). Every build also refreshes the time column and rewrites
   the NARRATION column of VIDEO-SCRIPT.md from `narration.txt`, so the script and the voice never diverge: edit `narration.txt`, not the table.
2. **Recording.** `npx playwright test` runs with `SCENE_INFO` (cue start offsets) and `SCENE_DURATIONS` (audio + 0.7 s per scene). The recorder
   starts each on-screen action when the narration sentence it illustrates begins (`at(k)` = the k-th sentence of the scene, counted from 0 across paragraphs), holds callouts until the next sentence, and never ends a scene before
   its audio plus 0.7 s. A fresh throwaway Postgres, gateway and fake provider are created for every take (ports 41200-41299, `LLMGW_PORT_RANGE`).
   Gateway keys and tokens (`lg_...`, `lgt_...`) are blurred the moment they are rendered; they are never legible in the video.
3. **Assembly.** The recorder writes `out/timings.json` (scene start times); the build finds the title card in the webm, trims the lead-in, places each
   scene's audio at its start time and encodes H.264 yuv420p + AAC + a soft subtitle track (mov_text) with `+faststart` to `out/llm-gateway.mp4`.
4. **Captions.** `out/llm-gateway.srt` is timed from the measured cue times (long sentences are split in two cues).

Options: `--skip-record` re-assembles from the last recording in `out/pw`, `--narration-only` only synthesises audio and prints durations; `BUILD_OUT` moves `out/`,
`--name` changes the output name. Environment: `BUILD_TMP` (scratch for the throwaway Postgres and the ffmpeg shim), `PLAYWRIGHT_BROWSERS_PATH`,
`E2E_HARNESS`, `E2E_PG_BIN`, `LLMGW_PORT_RANGE`.

If Playwright's own ffmpeg build is not installed (`npx playwright install ffmpeg` not run), `build.py` creates a private browsers directory under
`BUILD_TMP` (default `out/tmp`) that links the existing browsers and points Playwright's ffmpeg slot at the system ffmpeg.
If the script text changes, just run the build again: scene timing follows the audio. If a scene gains, loses or reorders sentences, adjust the `at(k)` anchors
in `record.spec.ts` (the recorder fails loudly when it asks for a sentence that does not exist).

`out/` is git-ignored; do not commit media.

Rehearsal without narration or video: `PACE=0.05 NOVIDEO=1 npx playwright test`.
