"""A project's icon: one emoji, the way Notion's page icon is one emoji.

`normalize_icon` is the one rule: None or "" clears it, one emoji (a flag, a skin tone or a ZWJ
sequence included) is kept as it was sent, and anything else raises ValueError. It reads the
characters themselves against the Unicode emoji grammar, so a letter, a digit, whitespace, markup
or two emoji side by side are refused. Every string here is shown to a person.
"""
from __future__ import annotations

# A value longer than this is not one emoji: the longest common sequences (a couple with two skin
# tones, a subdivision flag) are about ten code points.
ICON_MAX_CODEPOINTS = 16
NOT_ONE_EMOJI = "an icon is one emoji, for example a rocket"

_ZWJ = 0x200D
_VS16 = 0xFE0F
_KEYCAP = 0x20E3
_KEYCAP_BASES = {ord(c) for c in "0123456789#*"}
_BLACK_FLAG, _TAG_END = 0x1F3F4, 0xE007F

# The code points with the Unicode Emoji property that are pictures: the plain ASCII ones (digits,
# # and *) and the letter-like signs (the copyright and trademark signs) are left out on purpose.
_EMOJI_RANGES = (
    (0x203C, 0x203C), (0x2049, 0x2049), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328), (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA),
    (0x24C2, 0x24C2), (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE),
    (0x2600, 0x2604), (0x260E, 0x260E), (0x2611, 0x2611), (0x2614, 0x2615), (0x2618, 0x2618),
    (0x261D, 0x261D), (0x2620, 0x2620), (0x2622, 0x2623), (0x2626, 0x2626), (0x262A, 0x262A),
    (0x262E, 0x262F), (0x2638, 0x263A), (0x2640, 0x2640), (0x2642, 0x2642), (0x2648, 0x2653),
    (0x265F, 0x2660), (0x2663, 0x2663), (0x2665, 0x2666), (0x2668, 0x2668), (0x267B, 0x267B),
    (0x267E, 0x267F), (0x2692, 0x2697), (0x2699, 0x2699), (0x269B, 0x269C), (0x26A0, 0x26A1),
    (0x26A7, 0x26A7), (0x26AA, 0x26AB), (0x26B0, 0x26B1), (0x26BD, 0x26BE), (0x26C4, 0x26C5),
    (0x26C8, 0x26C8), (0x26CE, 0x26CF), (0x26D1, 0x26D1), (0x26D3, 0x26D4), (0x26E9, 0x26EA),
    (0x26F0, 0x26F5), (0x26F7, 0x26FA), (0x26FD, 0x26FD), (0x2702, 0x2702), (0x2705, 0x2705),
    (0x2708, 0x270D), (0x270F, 0x270F), (0x2712, 0x2712), (0x2714, 0x2714), (0x2716, 0x2716),
    (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744),
    (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2764), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
    (0x1F004, 0x1F004), (0x1F0CF, 0x1F0CF), (0x1F170, 0x1F171), (0x1F17E, 0x1F17F),
    (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F201, 0x1F202), (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F250, 0x1F251), (0x1F300, 0x1F64F),
    (0x1F680, 0x1F6FF), (0x1F7E0, 0x1F7EB), (0x1F7F0, 0x1F7F0), (0x1F90C, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
)


def _is_emoji(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def _is_modifier(cp: int) -> bool:   # a skin tone
    return 0x1F3FB <= cp <= 0x1F3FF


def _is_flag_letter(cp: int) -> bool:   # a regional indicator: two of them make a country flag
    return 0x1F1E6 <= cp <= 0x1F1FF


def _is_tag(cp: int) -> bool:   # the invisible letters of a subdivision flag such as Scotland's
    return 0xE0020 <= cp <= 0xE007E


def _element(cps: list[int], i: int) -> int | None:
    """The end of the one picture that starts at `i` (with its variation selector or skin tone),
    or None when `i` does not start one."""
    if i >= len(cps) or not _is_emoji(cps[i]):
        return None
    i += 1
    if i < len(cps) and (cps[i] == _VS16 or _is_modifier(cps[i])):
        i += 1
    return i


def _one_emoji(cps: list[int]) -> bool:
    """True when the code points are exactly one emoji."""
    if len(cps) == 2 and _is_flag_letter(cps[0]) and _is_flag_letter(cps[1]):
        return True
    if cps[0] in _KEYCAP_BASES:   # 1️⃣: a digit, the optional selector, the keycap
        return cps[1:] in ([_KEYCAP], [_VS16, _KEYCAP])
    if cps[0] == _BLACK_FLAG and len(cps) > 2 and cps[-1] == _TAG_END:
        return all(_is_tag(c) for c in cps[1:-1])
    i = _element(cps, 0)
    while i is not None and i < len(cps):   # a ZWJ sequence: pictures joined by the joiner
        if cps[i] != _ZWJ:
            return False
        i = _element(cps, i + 1)
    return i == len(cps)


def normalize_icon(icon) -> str | None:
    """One emoji as sent, or None for no icon (None or ""). Raises ValueError for anything else."""
    if icon is None or icon == "":
        return None
    if not isinstance(icon, str):
        raise ValueError(NOT_ONE_EMOJI)
    cps = [ord(c) for c in icon]
    if len(cps) > ICON_MAX_CODEPOINTS or not _one_emoji(cps):
        raise ValueError(NOT_ONE_EMOJI)
    return icon
