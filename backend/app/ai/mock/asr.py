"""离线语音识别（ASR）模拟。

为什么要有 ``echo`` 模式
------------------------
ASR 的输入端是**二进制音频**。在离线环境里无法真实解码，于是提供两种模式：

* ``echo`` —— 把调用方给的 ``hint_text``（调试台里就是「模拟用户说话」的输入框）
  原样复读。它让「语音 → 文本 → 对话 → 回复」这条链路可以在**不引入任何音频
  处理**的前提下端到端跑通，是 P8 WebSocket 对话协议联调的主力模式。
* ``fixed`` —— 恒定返回同一句文本。用于需要**输出完全固定**的基准测试，
  排除用户输入变量对延迟测量的干扰。

两种模式都不产生随机性，因此天然可复现。
"""

from __future__ import annotations

from app.ai.base import AsrResult
from app.core.config import settings

#: ``fixed`` 模式的固定识别结果
FIXED_TEXT = "你好，请给我讲一个小故事吧。"

#: ``echo`` 模式无输入时的占位文本（不能返回空串——空串会让下游误判为「没说话」）
ECHO_FALLBACK_TEXT = "你好呀。"


class MockAsrEngine:
    """规则化 ASR 引擎。"""

    def __init__(self, *, mode: str | None = None, fixed_text: str = FIXED_TEXT) -> None:
        """初始化。

        Args:
            mode: ``echo`` 或 ``fixed``；留空则读 ``MOCK_ASR_MODE``。
            fixed_text: ``fixed`` 模式的返回文本。
        """
        self.mode = (mode or settings.MOCK_ASR_MODE).lower()
        self.fixed_text = fixed_text

    @property
    def is_echo(self) -> bool:
        return self.mode == "echo"

    def transcribe(
        self,
        *,
        hint_text: str | None = None,
        audio_size: int = 0,
        duration_ms: int | None = None,
    ) -> AsrResult:
        """把「音频」转成文本。

        Args:
            hint_text: 调试台提供的模拟输入（``echo`` 模式下直接复读）。
            audio_size: 收到音频的字节数，仅用于回填到 ``raw`` 便于排查。
            duration_ms: 音频时长，仅透传。

        Returns:
            :class:`AsrResult`。
        """
        if self.is_echo:
            text = (hint_text or "").strip() or ECHO_FALLBACK_TEXT
            return AsrResult(
                text=text,
                is_final=True,
                confidence=0.99,
                duration_ms=duration_ms,
                simulated=True,
                raw={
                    "mode": "echo",
                    "audioSize": audio_size,
                    "note": "离线模拟：未对音频做真实解码",
                },
            )

        return AsrResult(
            text=self.fixed_text,
            is_final=True,
            confidence=1.0,
            duration_ms=duration_ms,
            simulated=True,
            raw={"mode": "fixed", "audioSize": audio_size},
        )


__all__ = ["ECHO_FALLBACK_TEXT", "FIXED_TEXT", "MockAsrEngine"]
