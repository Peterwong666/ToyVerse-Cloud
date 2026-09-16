"""离线语音合成（TTS）模拟。

``text`` 模式为什么是默认
-------------------------
默认返回 ``audio=None``：诚实地表示「本次没有真实音频」，
让调用方（前端 / 固件）自行回退到文本播报，而不是收到一段**听起来是静音**
的假音频却不知道哪里出了问题。这延续了 ADR-07「不伪造」的原则。

``wav`` 模式生成的是**结构合法但内容静音**的 PCM WAV：

* 结构合法 —— 让前端 ``<audio>`` 与固件的解码器能真正跑通播放路径，
  从而验证「音频帧 → 播放」这一段链路（含 base64 编解码、大小上限等）；
* 内容静音 —— 并在 :class:`TtsResult` 里以 ``simulated=True`` 明确标注，
  不会被误当作真实合成结果交付。

音频长度由文本长度决定，因此**同一文本必然得到同样的字节数**（可复现）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from app.ai.base import TtsResult
from app.core.config import settings

#: 采样率（Hz）。8k 是玩具设备语音链路的常见下限，足以验证链路，体积也小。
SAMPLE_RATE = 8000
#: 位深
BITS_PER_SAMPLE = 16
#: 声道数
CHANNELS = 1


@dataclass(slots=True, frozen=True)
class _WavSpec:
    """WAV 头参数（把常量收在一起，避免魔法数字散落在 struct.pack 里）。"""

    sample_rate: int = SAMPLE_RATE
    bits: int = BITS_PER_SAMPLE
    channels: int = CHANNELS

    @property
    def block_align(self) -> int:
        return self.channels * self.bits // 8

    @property
    def byte_rate(self) -> int:
        return self.sample_rate * self.block_align


def _duration_ms(text: str) -> int:
    """由文本长度推算「合理」的音频时长。

    取 200ms 起步 + 每字 250ms，上限 8 秒。数字本身不重要，
    重要的是它**只依赖文本**——这样两次调用必然得到相同结果。
    """
    return min(8000, 200 + len(text) * 250)


def build_silent_wav(duration_ms: int, *, spec: _WavSpec | None = None) -> bytes:
    """生成一段静音 PCM WAV（16bit / 单声道）。"""
    wav = spec or _WavSpec()
    frame_count = int(wav.sample_rate * duration_ms / 1000)
    payload = b"\x00" * (frame_count * wav.block_align)

    header = b"RIFF"
    header += struct.pack("<I", 36 + len(payload))
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<I", 16)  # PCM fmt 块长度
    header += struct.pack("<H", 1)  # audio format = PCM
    header += struct.pack("<H", wav.channels)
    header += struct.pack("<I", wav.sample_rate)
    header += struct.pack("<I", wav.byte_rate)
    header += struct.pack("<H", wav.block_align)
    header += struct.pack("<H", wav.bits)
    header += b"data"
    header += struct.pack("<I", len(payload))
    return header + payload


class MockTtsEngine:
    """规则化 TTS 引擎。"""

    def __init__(
        self,
        *,
        mode: str | None = None,
        silent_wav: bool = True,
        voice_id: str = "mock-voice-default",
    ) -> None:
        """初始化。

        Args:
            mode: ``text`` 或 ``wav``；留空则读 ``MOCK_TTS_MODE``。
            silent_wav: ``wav`` 模式下是否生成静音音频（关闭则只回报时长）。
            voice_id: 默认音色标识。
        """
        self.mode = (mode or settings.MOCK_TTS_MODE).lower()
        self.silent_wav = silent_wav
        self.voice_id = voice_id

    def synthesize(self, text: str, *, voice_id: str | None = None) -> TtsResult:
        """合成语音。

        Returns:
            :class:`TtsResult`；``text`` 模式下 ``audio`` 为 ``None``。
        """
        duration = _duration_ms(text)
        chosen_voice = voice_id or self.voice_id

        if self.mode == "wav":
            audio = build_silent_wav(duration) if self.silent_wav else None
            return TtsResult(
                text=text,
                content_type="audio/wav",
                audio=audio,
                duration_ms=duration,
                simulated=True,
                voice_id=chosen_voice,
                raw={
                    "mode": "wav",
                    "sampleRate": SAMPLE_RATE,
                    "bits": BITS_PER_SAMPLE,
                    "channels": CHANNELS,
                    "note": "离线模拟：结构合法的静音音频，非真实合成结果",
                },
            )

        return TtsResult(
            text=text,
            content_type="text/plain; charset=utf-8",
            audio=None,
            duration_ms=duration,
            simulated=True,
            voice_id=chosen_voice,
            raw={"mode": "text", "note": "离线模拟：未产出音频，调用方应回退为文本播报"},
        )


__all__ = ["BITS_PER_SAMPLE", "CHANNELS", "SAMPLE_RATE", "MockTtsEngine", "build_silent_wav"]
