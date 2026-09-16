"""离线模拟引擎（零密钥可跑通全流程）。

* :mod:`app.ai.mock.scenarios` —— 内置内容库（故事 / 儿歌 / 天气话术）、角色预设、安全关键词
* :mod:`app.ai.mock.dialogue` —— 规则对话：意图识别 → 素材选择 → 角色改写 → 分块流式
* :mod:`app.ai.mock.asr` —— 遵循 ``MOCK_ASR_MODE=echo|fixed``
* :mod:`app.ai.mock.tts` —— 遵循 ``MOCK_TTS_MODE=text|wav``
* :mod:`app.ai.mock.engine` —— 把上述能力组装成 ``MockProvider``
"""

from __future__ import annotations

from app.ai.mock.engine import MOCK_PROVIDER_CODE, MockProvider, build_mock_provider

__all__ = ["MOCK_PROVIDER_CODE", "MockProvider", "build_mock_provider"]
