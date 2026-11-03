"""Bounded, escaped-by-template text comparison for private site review."""
import re
from difflib import SequenceMatcher


# Keep CJK text useful without requiring spaces; preserve every input character.
_EAST_ASIAN = r'\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af'
_TOKEN = re.compile(rf'\s+|[{_EAST_ASIAN}]|(?:(?![{_EAST_ASIAN}])\w)+|[^\w\s]', re.UNICODE)
MAX_TOKENS_PER_SIDE = 600
MAX_TOTAL_CHARACTERS = 20000


def compare_page_text(before, after):
    """Return reconstructable parts, or full unmarked text when limits are exceeded.

    Call only with validated plain strings. A changed part is data, never HTML.
    """
    if before == after:
        return None
    if len(before) + len(after) > MAX_TOTAL_CHARACTERS:
        return {'highlighted':False, 'before':[{'text':before,'changed':False}],
                'after':[{'text':after,'changed':False}]}
    old, new = _TOKEN.findall(before), _TOKEN.findall(after)
    if len(old) > MAX_TOKENS_PER_SIDE or len(new) > MAX_TOKENS_PER_SIDE:
        return {'highlighted':False, 'before':[{'text':before,'changed':False}],
                'after':[{'text':after,'changed':False}]}
    result = {'highlighted':True, 'before':[], 'after':[]}

    def append(side, tokens, changed):
        value = ''.join(tokens)
        if not value:
            return
        parts = result[side]
        if parts and parts[-1]['changed'] == changed:
            parts[-1]['text'] += value
        else:
            parts.append({'text':value, 'changed':changed})

    for operation, a, b, c, d in SequenceMatcher(None, old, new, autojunk=True).get_opcodes():
        if operation == 'equal':
            append('before', old[a:b], False)
            append('after', new[c:d], False)
        else:
            append('before', old[a:b], True)
            append('after', new[c:d], True)
    return result
