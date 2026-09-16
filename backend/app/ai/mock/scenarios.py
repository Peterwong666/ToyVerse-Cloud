"""离线模拟引擎的内置素材库与规则。

设计取向
--------
这是一份**内置内容库**（故事 / 儿歌 / 天气话术），不是占位符字符串：

* 演示与验收要在**零密钥、零外网**的前提下跑通「讲故事、唱歌、问天气」，
  并让前端流式渲染真的有内容可渲染；
* 素材以「关键词 → 候选列表」组织，因此规则对话的命中是可解释的
  （命中哪个关键词、取到哪条素材都能在响应里说明），
  而不是黑盒模型输出——这符合 ADR-04「AI 业务标准以 ai-toy 原型与 PRD-03 为准」。

内容安全
--------
:data:`SAFETY_KEYWORDS` 与 :func:`detect_safety` 提供最基础的关键词级拦截，
让 P7 就能验证「命中写 safety_flag」这条链路；完整的敏感词库与 LLM 复核
在 P8 落地（``ai_configs.safety_*`` 三个开关）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 内容素材
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ContentSnippet:
    """一条内容素材（故事或儿歌）。"""

    title: str
    keywords: tuple[str, ...]
    body: str
    kind: str  # story / song


#: 内置故事库。``keywords`` 同时用于「用户说什么」与「知识库检索」的匹配。
STORIES: tuple[ContentSnippet, ...] = (
    ContentSnippet(
        title="小狐狸的月亮灯",
        keywords=("月亮", "狐狸", "夜晚", "灯"),
        kind="story",
        body=(
            "从前有只小狐狸，住在山脚下的树洞里。它最喜欢做的事情，"
            "就是在夜里抬头看月亮。可是有一天，月亮不见了。"
            "小狐狸提着一盏小灯笼，沿着小溪一路找啊找，"
            "在蒲公英田里遇见了打着呵欠的月亮。原来月亮只是困了，"
            "躲进云朵被子里睡着了。小狐狸轻轻说："
            "「晚安，月亮。」然后把灯笼挂在洞口，"
            "从那天起，山脚下每晚都有一盏小小的月亮灯。"
        ),
    ),
    ContentSnippet(
        title="会飞的小乌龟",
        keywords=("乌龟", "飞", "梦想", "朋友"),
        kind="story",
        body=(
            "小乌龟慢慢有一个愿望——它想飞。小鱼说：「你没有翅膀呀。」"
            "小乌龟不生气，它每天练习伸长脖子、用力蹬腿。"
            "秋天到了，一阵大风把落叶卷上天空，小乌龟趴在最大的那片叶子上，"
            "真的飞了起来。它飞过金色的稻田，飞过白色的云，"
            "最后轻轻落回池塘边。慢慢明白了：飞一次的快乐，"
            "足够它慢慢慢慢地回味一整个冬天。"
        ),
    ),
    ContentSnippet(
        title="不肯睡觉的小星星",
        keywords=("星星", "睡觉", "天空", "晚安"),
        kind="story",
        body=(
            "天空里有一颗最小的小星星，它总是不肯睡觉。"
            "别的星星都盖好云被子闭上了眼睛，它还在东张西望。"
            "它看见森林里的小兔子在刷牙，看见小熊在数羊，"
            "看见小朋友抱着玩偶打哈欠。小星星也打了一个哈欠，"
            "又一个哈欠……等它再睁开眼睛的时候，天已经亮了。"
            "它红着脸躲了起来，心里想：今天晚上，我一定早点睡。"
        ),
    ),
)

#: 内置儿歌库。真实产品里歌词应来自内容库（``content_items``，P9 落地）。
SONGS: tuple[ContentSnippet, ...] = (
    ContentSnippet(
        title="小星星",
        keywords=("星星", "闪", "儿歌"),
        kind="song",
        body="一闪一闪亮晶晶，满天都是小星星。挂在天空放光明，好像许多小眼睛。",
    ),
    ContentSnippet(
        title="小兔子乖乖",
        keywords=("兔子", "乖乖", "门"),
        kind="song",
        body="小兔子乖乖，把门儿开开，快点儿开开，我要进来。不开不开我不开，妈妈没回来。",
    ),
    ContentSnippet(
        title="数鸭子",
        keywords=("鸭子", "数", "桥"),
        kind="song",
        body="门前大桥下，游过一群鸭，快来快来数一数，二四六七八。",
    ),
)

#: 故事类意图的关键词
STORY_KEYWORDS: tuple[str, ...] = ("故事", "讲故事", "童话", "睡前", "讲一个", "讲个")
#: 儿歌类意图的关键词
SONG_KEYWORDS: tuple[str, ...] = ("歌", "唱歌", "儿歌", "唱首", "唱一", "唱个", "童谣")
#: 天气类意图的关键词
WEATHER_KEYWORDS: tuple[str, ...] = ("天气", "下雨", "下雪", "气温", "冷", "热", "风")
#: 寒暄类意图的关键词
GREETING_KEYWORDS: tuple[str, ...] = ("你好", "您好", "早上好", "晚上好", "嗨", "hello", "hi", "在吗")
#: 追问类（走知识库检索）
QUESTION_KEYWORDS: tuple[str, ...] = ("为什么", "是什么", "怎么", "知道", "告诉我", "什么是")

#: 天气话术（模拟引擎不联网，故按关键词给出稳定的拟人化回答）
WEATHER_REPLIES: tuple[str, ...] = (
    "今天的天气有点害羞，躲在云朵后面不肯出来。出去玩儿记得带上小外套哦。",
    "外面有风，树叶在沙沙地唱歌。我们可以在院子里放风筝呀。",
    "看起来要下雨啦，雨点会给小草洗澡。记得穿上小雨靴，别踩水坑太深哦。",
    "今天太阳暖暖的，最适合去公园晒太阳、看小蚂蚁搬家。",
)

#: 兜底话术（未命中任何规则时使用）
FALLBACK_REPLIES: tuple[str, ...] = (
    "这个问题有点难住我啦，我们换个话题吧——你想听故事，还是听儿歌呢？",
    "嗯……我还在学习怎么回答这个。要不要我先给你讲一个小故事？",
    "我听懂啦，不过我还想多知道一点点。你可以再说一遍，或者让我唱首歌吗？",
)

#: 寒暄话术
GREETING_REPLIES: tuple[str, ...] = (
    "你好呀！我是你的小玩具伙伴，今天想听故事还是听儿歌？",
    "嗨！我一直在这儿等你说话呢。要不要我唱首歌给你听？",
    "你好你好！我们一起来玩吧，你想听小狐狸的故事吗？",
)


# ---------------------------------------------------------------------------
# 角色预设（内置）
# ---------------------------------------------------------------------------

#: 内置角色预设。与 ``role_presets`` 表的 ``is_builtin=True`` 记录对应，
#: 但内置在代码里的好处是：**数据库还没种子数据时也能演示角色切换**。
BUILTIN_ROLE_PRESETS: dict[str, dict[str, Any]] = {
    "gentle_sister": {
        "name": "温柔姐姐",
        "greeting": "宝贝，姐姐在这儿呢，我们慢慢聊。",
        "prefix": "姐姐轻声说：",
        "suffix": "慢慢来，姐姐陪着你。",
    },
    "funny_brother": {
        "name": "搞笑哥哥",
        "greeting": "嘿嘿，哥哥来啦！今天想玩点什么好玩的？",
        "prefix": "哥哥挤挤眼睛说：",
        "suffix": "哈哈哈，是不是很好玩！",
    },
    "little_dinosaur": {
        "name": "小恐龙",
        "greeting": "嗷呜——小恐龙醒啦！",
        "prefix": "小恐龙吼了一声说：",
        "suffix": "嗷呜，我们下次再聊！",
    },
    "teacher": {
        "name": "知识小博士",
        "greeting": "你好，我是知识小博士，我们一起来发现答案吧。",
        "prefix": "小博士推了推眼镜说：",
        "suffix": "你记住啦吗？我们下次继续探索。",
    },
}

#: 默认角色（未指定角色预设时使用）
DEFAULT_ROLE_PRESET = "gentle_sister"


# ---------------------------------------------------------------------------
# 内容安全（最基础的关键词级拦截）
# ---------------------------------------------------------------------------

#: 演示用敏感词。真实产品应可配置（``ai_configs.safety_sensitive_words``）。
SAFETY_KEYWORDS: tuple[str, ...] = ("暴力", "自杀", "赌博", "毒品", "恐怖袭击", "黄色")


def detect_safety(text: str) -> str | None:
    """检测文本命中的敏感词。

    Returns:
        命中的敏感词；未命中返回 ``None``。
    """
    for word in SAFETY_KEYWORDS:
        if word in text:
            return word
    return None


def safety_reply(word: str) -> str:
    """敏感词命中后的统一话术（不重复敏感词本身，避免二次传播）。"""
    return "这个话题不太适合我们聊哦，我们来说点开心的吧——你想听故事还是听儿歌？"


# ---------------------------------------------------------------------------
# 意图识别与素材挑选
# ---------------------------------------------------------------------------

INTENT_STORY = "story"
INTENT_SONG = "song"
INTENT_WEATHER = "weather"
INTENT_GREETING = "greeting"
INTENT_QUESTION = "question"
INTENT_FALLBACK = "fallback"


@dataclass(slots=True)
class IntentMatch:
    """意图识别结果。

    把「命中的关键词」一起返回，是为了让调试台能解释「为什么这样回答」，
    也让单元测试可以断言规则本身而不是整段文案。
    """

    intent: str
    matched_keyword: str | None = None


def detect_intent(text: str) -> IntentMatch:
    """按关键词识别意图。

    判定顺序即优先级：先具体（故事 / 儿歌 / 天气），再泛化（寒暄 / 追问），
    最后兜底。为什么把寒暄排在追问之前？因为「你好，为什么……」这类
    混合句在玩具场景里更像打招呼，先回应情绪体验更好。
    """
    cleaned = text.strip().lower()

    for keyword in STORY_KEYWORDS:
        if keyword in cleaned:
            return IntentMatch(INTENT_STORY, keyword)
    for keyword in SONG_KEYWORDS:
        if keyword in cleaned:
            return IntentMatch(INTENT_SONG, keyword)
    for keyword in WEATHER_KEYWORDS:
        if keyword in cleaned:
            return IntentMatch(INTENT_WEATHER, keyword)
    for keyword in GREETING_KEYWORDS:
        if keyword in cleaned:
            return IntentMatch(INTENT_GREETING, keyword)
    for keyword in QUESTION_KEYWORDS:
        if keyword in cleaned:
            return IntentMatch(INTENT_QUESTION, keyword)
    return IntentMatch(INTENT_FALLBACK, None)


def match_snippet(text: str, pool: tuple[ContentSnippet, ...]) -> ContentSnippet | None:
    """在素材池里按关键词找最匹配的一条。

    打分规则：命中关键词数多者优先，同分取先出现的（顺序稳定 → 结果可复现）。
    这是知识库检索的「无向量版本」：P9 接入真实切块检索后，此函数被替换，
    但接口保持「给一段话，返回最相关的素材」不变。
    """
    best: ContentSnippet | None = None
    best_score = 0
    for snippet in pool:
        score = sum(1 for keyword in snippet.keywords if keyword in text)
        if score > best_score:
            best = snippet
            best_score = score
    return best


def search_knowledge(text: str, snippets: tuple[ContentSnippet, ...]) -> ContentSnippet | None:
    """知识库关键词检索（最小实现）。

    与 :func:`match_snippet` 复用同一套打分，只是语义上是「知识库命中」。
    命中时回复会引用素材标题，从而在响应里体现「知识库影响了回复」。
    """
    return match_snippet(text, snippets)


def pick(pool: tuple[Any, ...], rng: random.Random) -> Any:
    """从候选池中按固定随机源取一条，保证同种子同结果。"""
    return pool[rng.randrange(len(pool))]
