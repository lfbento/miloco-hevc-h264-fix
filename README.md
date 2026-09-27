# miloco-hevc-h264-fix

Play **HEVC-only Mi Home cameras** (Xiaomi Smart Camera CW700S / `MJSXJ06HL`, model `isa.camera.700sa`, and likely other H.265-only models) in **[Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco)** on browsers that cannot decode HEVC — e.g. Chrome/Firefox on Linux without VAAPI (nouveau drivers), or any browser without hardware HEVC.

Tested with the `ghcr.nju.edu.cn/miiot/miloco:main` image, Chrome 154 on Linux (GTX 1080, nouveau — no VAAPI), camera firmware streaming **HEVC-only 2560x1440@25fps** (the camera ignores every codec/quality setting).

> **PT-BR:** este patch faz as câmeras da Xiaomi que só transmitem HEVC (ex.: CW700S) rodarem ao vivo no Miloco em qualquer navegador, transcodificando H.265 → H.264 no backend. Resumo em português no fim do arquivo.

---

## Symptoms

* Toast **“video decode failed”** / **“falha na codificação do vídeo”** when opening the live view.
* **Infinite loading spinner** with no error at all in the console (when the stream is already H.264).
* Green bands / half-the-frame-missing corruption if you feed the WebCodecs player multi-slice frames.

## Root causes (4 independent bugs)

| # | Where | Bug |
|---|-------|-----|
| 1 | Camera | CW700S streams **HEVC only** — codec/quality settings are ignored. Chrome on Linux without VAAPI has *no* HEVC decoder at all (`VideoDecoder.isConfigSupported({codec:'hev1.1.6.L93.B0'})` → `hw:false, sw:false`). |
| 2 | Frontend player | `configure()` is called at mount with a **hardcoded `hev1.1.6.L93.B0` + `prefer-hardware`** before any codec sniffing — guaranteed failure on non-HEVC browsers. |
| 3 | Frontend player | The keyframe check `I()` for the H.264 branch **only inspects the first NAL** of each message. A keyframe arriving as `[SPS][PPS][SEI][IDR]` never passes (first NAL is SPS, type 7), so **every frame is silently dropped** → eternal spinner, zero errors. (The H.265 branch scans all NALs — the asymmetry hides the bug.) |
| 4 | Transcoding (if you roll your own) | x264 `-tune zerolatency` enables **sliced-threads** (~8 slices/frame): sending one WS message per slice makes the player render 1/8 of each frame → green garbage. Also: `await drain()` in the MIoT frame callback backpressures the camera library → **dropped input frames** (`Could not find ref with POC xx` in the ffmpeg log). |

## Solution

Transcode HEVC → H.264 inside the Miloco backend (ffmpeg), so the browser gets a codec it can always decode, and fix the player bugs so it actually plays it:

```
Camera (HEVC 2560x1440@25)
   └─ MIoT raw-stream callback
        └─ H264Transcoder  ── ffmpeg: hevc → libx264 Constrained Baseline 1280x720
        │     • syncs on the 1st HEVC keyframe (no green warm-up artifacts)
        │     • sliced-threads=0 (ONE slice per frame)
        │     • SPS/PPS/SEI attached to the same message as the IDR
        └─ WebSocket (H.264 Annex-B, ~20–25 fps)
             └─ WebCodecs player
                   • codec sniffed from the 1st frame (avc1.42C01F)
                   • keyframe check scans ALL NALs of the message
```

Latency cost: ~200–500 ms. CPU: ~¼ of one core per 720p stream (x264 `ultrafast`, ~14× realtime on a 2017-era desktop).

## Install

### 1. Backend — drop in the transcoder

```bash
# extract the pristine files from your image
IMG=ghcr.nju.edu.cn/miiot/miloco:main
mkdir -p patched
docker run --rm --entrypoint cat $IMG /app/miloco_server/service/miot_service.py > patched/miot_service.py

# apply the patch (wraps the stream callback with the transcoder)
patch patched/miot_service.py < patches/miot_service.patch
cp patches/h264_transcoder.py patched/
```

### 2. Frontend — patch the player bundle

```bash
# the JS bundle name has a content hash that changes per release:
docker run --rm --entrypoint sh $IMG -c 'ls /app/miloco_server/static/assets/js/'
docker run --rm --entrypoint cat $IMG /app/miloco_server/static/assets/js/index-<hash>.js > frontend/index-original.js

cd frontend
python3 build_patch.py index-original.js      # writes dist/index-original.js
# (asserts fail loudly if upstream changed too much — re-port the snippets below)
```

The player patch is small and portable — see `frontend/build_patch.py` steps **4–4f** for the exact string replacements (deferred `configure()`, codec sniffer, `I()` keyframe fix).

### 3. docker-compose

See `docker-compose.example.yml`. You need:

* `patches/h264_transcoder.py` → `/app/miloco_server/utils/h264_transcoder.py`
* the patched `miot_service.py` → `/app/miloco_server/service/miot_service.py`
* the patched bundle → `/app/miloco_server/static/assets/js/index-<hash>.js`
* a **static ffmpeg** inside the container (any build with libx264; `johnvansickle.com/ffmpeg` static builds work)

Then `docker compose up -d --force-recreate miloco`, hard-refresh the browser (Ctrl+Shift+R) and hit play.

### Optional: Portuguese UI

`frontend/translation-pt.json` + step **1–3** of `build_patch.py` add a full pt-BR locale to the web UI (the patcher adds `Português` to the language switcher and sets it as default). Unrelated to video — included as a bonus.

## Verification

1. `ffmpeg` log inside the container shows `H264 transcoder started` / `synced on keyframe`, no `Could not find ref` spam.
2. The WebSocket stream is H.264: first message starts with `00 00 00 01 67` (SPS), followed by PPS + IDR; deltas are single slice NALs.
3. The canvas in the live view sizes to 1280×720 and paints — no spinner, no toast.
4. `chrome://gpu` / `vainfo` absence does not matter anymore: decoding is `avc1.42C01F` (Constrained Baseline 3.1), supported everywhere.

## Known limitations

* Some input frames may still be lost over the camera’s Wi-Fi/P2P link (you’ll see occasional `Could not find ref with POC xx`); ffmpeg conceals them and playback stays watchable.
* The output is fixed at 1280×720 (`-vf scale=1280:-2`) to keep CPU low — edit `_FFMPEG_ARGS` for more.

## Files

| Path | What |
|------|------|
| `patches/h264_transcoder.py` | drop-in module: HEVC→H.264 stream transcoder (asyncio + ffmpeg) |
| `patches/miot_service.patch` | wraps `start_video_stream`/`stop_video_stream` around the transcoder |
| `patches/auth_schema.patch` | adds `pt` to `UserLanguage` (only needed for the pt-BR UI) |
| `frontend/build_patch.py` | regenerates the JS bundle: player fixes + pt-BR locale |
| `frontend/translation-pt.json` | pt-BR dictionary (363 keys) |
| `docker-compose.example.yml` | volume mounts |

## Credits

Debugged and built by **Bento** with **Hermes Agent** (Nous Research) — September 2026. Root-caused with frame-level capture of both transcoder ends, offline ffmpeg reproduction and Playwright/Chrome WebCodecs tests.

Upstream: [XiaoMi/xiaomi-miloco](https://github.com/XiaoMi/xiaomi-miloco). Patches are diffs against that code — upstream license applies to it; the contents of `patches/h264_transcoder.py` and `frontend/` are MIT (see LICENSE).

---

## Resumo em português (PT-BR)

Câmeras como a **CW700S** (`isa.camera.700sa`) só transmitem **HEVC**, e o Chrome no Linux (sem VAAPI/driver novo) **não decodifica HEVC** — por isso o “falha na codificação do vídeo” ou o loading infinito.

A solução: **transcodificar H.265 → H.264 no backend** (ffmpeg, 720p, ~¼ de core) antes dos frames irem pro navegador, e corrigir dois bugs do player do Miloco:

1. `configure()` era chamado com codec fixo `hev1` antes de inspecionar o stream — agora o codec é farejado do 1º frame (`avc1.42C01F`);
2. a detecção de keyframe do H.264 só olhava o 1º NAL da mensagem — o keyframe vem como `[SPS][PPS][SEI][IDR]` e era descartado em silêncio (loading eterno). Agora varre todos os NALs.

Dois pegadinhas de quem for fazer o próprio transcoder: **`sliced-threads=0`** no x264 (senão cada frame vira ~8 mensagens e a imagem sai verde) e **não usar `await drain()`** no callback da câmera (backpressure faz a lib descartar frames).
