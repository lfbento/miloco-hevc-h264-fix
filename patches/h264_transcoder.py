"""Transcodificador HEVC -> H.264 para o stream de vídeo (WebSocket).

O Chrome no Linux sem VAAPI (driver nouveau) não decodifica HEVC e câmeras
como a isa.camera.700sa (CW700S/MJSXJ06HL) só transmitem H.265. Este módulo
reencoda cada stream em H.264 Annex-B (Constrained Baseline 1280x720) via
ffmpeg antes dos frames irem para o WebSocket — codec que todo browser
decodifica.

Detalhes que importam (custaram caro descobrir):
- Sincroniza só no 1º keyframe HEVC (IDR_W_RADL/IDR_N_LP/CRA): frames capturados
  no meio do GOP não têm referências e viram artefato verde no cliente.
- `sliced-threads=0`: sem isso o x264 fatia cada frame em ~8 slices e cada slice
  vira uma mensagem — o player recebe 1/8 de cada frame (imagem corrompida).
- Sem `await drain()` no callback: backpressure aqui faz a lib MIoT descartar
  frames da câmera (erros de referência no decoder).
- NALs de cabeçalho (SEI/SPS/PPS/AUD) são acumulados e enviados JUNTOS com o
  primeiro slice seguinte — o decoder precisa dos parameter sets no mesmo
  chunk que o IDR. Deltas (slice não-IDR) vão um por mensagem.
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

FFMPEG_BIN = os.environ.get("H264_FFMPEG_BIN", "/usr/local/bin/ffmpeg")

_FFMPEG_ARGS = [
    FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
    "-fflags", "nobuffer", "-flags", "low_delay",
    "-f", "hevc", "-i", "pipe:0",
    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
    "-profile:v", "baseline", "-g", "50", "-bf", "0",
    "-x264-params", "repeat-headers=1:sliced-threads=0",
    "-vf", "scale=1280:-2",
    "-an", "-f", "h264", "pipe:1",
]


def _is_vcl(nal_type: int) -> bool:
    """H.264: tipos 1-5 são slices (dados de vídeo)."""
    return 1 <= nal_type <= 5


# HEVC: IDR_W_RADL(19), IDR_N_LP(20), CRA(21) — inícios de GOP válidos
_HEVC_KEY_TYPES = {19, 20, 21}


def _start_offsets(buf) -> list:
    """Offsets de todos os start codes Annex-B (00 00 01 ou 00 00 00 01)."""
    res = []
    i = 0
    n = len(buf)
    while i < n - 3:
        if buf[i] == 0 and buf[i + 1] == 0:
            if buf[i + 2] == 0 and buf[i + 3] == 1:
                res.append(i)
                i += 4
                continue
            if buf[i + 2] == 1:
                res.append(i)
                i += 3
                continue
        i += 1
    return res


def _contains_hevc_key(data) -> bool:
    """Se o buffer contém um frame-chave HEVC (início de GOP válido)."""
    n = len(data)
    for off in _start_offsets(data):
        if off + 4 >= n:
            continue
        hdr = 4 if data[off + 2] == 0 else 3
        nal_type = (data[off + hdr] >> 1) & 0x3F
        if nal_type in _HEVC_KEY_TYPES:
            return True
    return False


class H264Transcoder:
    """Envolve o callback de frames: escreve HEVC no ffmpeg e repassa H.264."""

    def __init__(self, inner, did: str, channel: int):
        self._inner = inner
        self._did = did
        self._channel = channel
        self._proc = None
        self._synced = False     # só alimenta o ffmpeg a partir do 1º keyframe
        self._pre = bytearray()   # NALs de cabeçalho aguardando o próximo slice
        self._tail = bytearray()  # bytes incompletos vindos do stdout
        self._reader_task = None
        self._stderr_task = None

    async def start(self):
        self._proc = await asyncio.create_subprocess_exec(
            *_FFMPEG_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        logger.info("H264 transcoder started: %s.%s (pid=%s)",
                    self._did, self._channel, self._proc.pid)

    async def on_frame(self, did: str, data: bytes, ts: int, seq: int, channel: int) -> None:
        """Callback no formato do miot: alimenta o ffmpeg com o HEVC bruto."""
        proc = self._proc
        if proc is None or proc.stdin.is_closing():
            return
        if not self._synced:
            # Frames capturados no meio do GOP não têm referências: se forem
            # reencodificados viram artefato verde no cliente. Espera o 1º keyframe.
            if not _contains_hevc_key(data):
                return
            self._synced = True
            logger.info("H264 transcoder synced on keyframe: %s.%s",
                        self._did, self._channel)
        try:
            proc.stdin.write(data)
            # Sem await drain(): backpressure aqui faz a lib MIoT descartar
            # frames da câmera (frames perdidos = erros de referência).
        except (ConnectionResetError, BrokenPipeError, RuntimeError) as err:
            logger.error("H264 transcoder stdin error: %s", err)

    def _on_output(self, data: bytes) -> list:
        """Reagrupa o stdout em mensagens: (cabeçalhos + slice) por mensagem."""
        self._tail.extend(data)
        starts = _start_offsets(self._tail)
        if len(starts) < 2:
            return []
        cut = starts[-1]  # o último NAL pode estar incompleto: fica no buffer
        complete = bytes(self._tail[:cut])
        del self._tail[:cut]
        bounds = _start_offsets(complete) + [len(complete)]
        msgs = []
        for a, b in zip(bounds, bounds[1:]):
            nal = complete[a:b]
            hdr = 4 if nal[2] == 0 else 3
            nal_type = nal[hdr] & 0x1F
            if _is_vcl(nal_type):
                msgs.append(bytes(self._pre) + nal)
                self._pre.clear()
            else:
                self._pre.extend(nal)
        return msgs

    async def _read_loop(self):
        try:
            while True:
                data = await self._proc.stdout.read(65536)
                if not data:
                    break
                for msg in self._on_output(data):
                    await self._inner(self._did, msg, 0, 0, self._channel)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error("H264 transcoder read error: %s", err)

    async def _stderr_loop(self):
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.warning("ffmpeg: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    async def close(self):
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        proc = self._proc
        self._proc = None
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, ProcessError):
                pass
        logger.info("H264 transcoder closed: %s.%s", self._did, self._channel)


# Registro no nível de MÓDULO (compartilhado entre instâncias do serviço).
# Chave: (camera_id, channel) — garante um único transcoder por canal.
_REGISTRY: dict = {}


async def replace_transcoder(inner, did: str, channel: int) -> H264Transcoder:
    """Cria o transcoder do canal, encerrando um eventual anterior."""
    key = (did, channel)
    old = _REGISTRY.pop(key, None)
    if old is not None:
        logger.info("H264 transcoder replaced: %s.%s", did, channel)
        await old.close()
    transcoder = H264Transcoder(inner, did, channel)
    await transcoder.start()
    _REGISTRY[key] = transcoder
    return transcoder


async def release_transcoder(did: str, channel: int) -> None:
    """Encerra e remove o transcoder do canal."""
    transcoder = _REGISTRY.pop((did, channel), None)
    if transcoder is not None:
        await transcoder.close()
