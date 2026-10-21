"""Versioned, inspectable controls for the installed YuE2 JSON protocol.

CFG acts on style AND lyrics/voice conditioning, not a guaranteed timbre transfer.
Reference score is unchanged. Free mode explicitly omits it (cot=off).
"""
import json
import math
import re
from pathlib import Path

RELEASE = json.loads(Path(__file__).with_name('release.json').read_text(encoding='utf-8'))
VERSION = RELEASE['version']
CONTROL_VERSION = 1
DEFAULT_STRENGTH = 75
LEGACY_INSTRUCTIONS = '温暖的电钢琴，松弛的节奏，保留原曲旋律与情绪。'
# One catalog for the service and the interface: style groups and tags, smart defaults, singers, suite parts.
CATALOG = json.loads(Path(__file__).with_name('styles.json').read_text(encoding='utf-8'))
STYLE_TAGS = {name: info['tags'] for name, info in CATALOG['styles'].items()}
MAX_VERSIONS = 20
# Settings saved before v1.6.0 hold 10 entries per version list; they are padded, never discarded.
LEGACY_VERSIONS = 10
LETTERS = 'ABCDEFGHIJKLMNOPQRST'
DEFAULT_STYLES = CATALOG['defaults']
GENDERS = {name: info['tags'] for name, info in CATALOG['singers'].items()}
SUITE = CATALOG['suite']
SMART_FIELDS = ('mode', 'melody_mode', 'adjust')
TONES = {
    'natural': 'natural vocal timbre', 'warm': 'warm, mellow vocals, rounded intimate vocal tone',
    'bright': 'bright, clear vocals, ringing forward vocal tone',
    'husky': 'husky, raspy vocals, textured gravelly vocal tone',
    'airy': 'soft, airy vocals, breathy delicate vocal tone',
    'powerful': 'powerful, resonant vocals, full voiced projected singing',
}

def strength_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError('风格与音色力度须为 0–100 的数值。')
    return float(value)

def validate_controls(payload, *, inherit=False):
    value = payload.get('change_strength', None if inherit else DEFAULT_STRENGTH)
    strength = None if inherit and value is None else strength_value(value)
    melody = payload.get('melody_mode', '' if inherit else 'reference')
    if melody not in (('', 'reference', 'free') if inherit else ('reference', 'free')):
        raise ValueError('旋律约束无效。')
    return strength, melody

def text_field(payload, key, limit=900):
    value = payload.get(key, '')
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f'{key} 须为不超过 {limit} 字的文字。')
    return value.strip()

def version_count(value, *, inherit=False):
    if inherit and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_VERSIONS:
        raise ValueError(f'每首版本数须为 1–{MAX_VERSIONS} 的整数。')
    return value

def text_list(value, limit, *, allow_empty, message, pad=None):
    """Exactly MAX_VERSIONS strings, one per version letter. A 10-entry list from before v1.6.0
    is completed by `pad` (or empty entries), so a stale interface never loses versions K–T."""
    if (isinstance(value, list) and len(value) == LEGACY_VERSIONS and
            all(isinstance(v, str) and len(v.strip()) <= limit and (allow_empty or v.strip()) for v in value)):
        value = pad(value) if pad else value + [''] * (MAX_VERSIONS - LEGACY_VERSIONS)
    if (not isinstance(value, list) or len(value) != MAX_VERSIONS or
            not all(isinstance(v, str) and len(v.strip()) <= limit and (allow_empty or v.strip()) for v in value)):
        raise ValueError(message)
    return [v.strip() for v in value]

def default_styles(*given):
    """The given styles first, then the unused presets, so every version starts on a different style."""
    taken = {s.strip().casefold() for s in given}
    return list(given) + [s for s in DEFAULT_STYLES if s.casefold() not in taken][:MAX_VERSIONS - len(given)]

def style_info(style):
    """The catalog entry of a preset (any letter case), or {} for a custom style."""
    return next((info for name, info in CATALOG['styles'].items() if name.casefold() == str(style).strip().casefold()), {})

def apply_smart_defaults(item, style, explicit=()):
    """One version's settings after its style's and singer's smart defaults, plus what they changed.
    A style's mode and melody choice replace the shared setting; tempo and key are only filled in
    while still unchanged. Whatever the song sets for itself (`explicit`) is never touched."""
    result, applied = dict(item), {}
    defaults = style_info(style).get('defaults', {})
    if 'mode' in defaults and 'mode' not in explicit and not result.get('instrumental') and result.get('mode', 'remix') != defaults['mode']:
        result['mode'] = applied['mode'] = defaults['mode']
    vocal = not result.get('instrumental') and result.get('mode', 'remix') == 'remix'
    # Keeping the original singer always follows the reference melody, so a melody default cannot apply there.
    if ('melody_mode' in defaults and 'melody_mode' not in explicit and (vocal or result.get('instrumental'))
            and result.get('melody_mode', 'reference') != defaults['melody_mode']):
        result['melody_mode'] = applied['melody_mode'] = defaults['melody_mode']
    shifts = {k: v for k, v in defaults.items() if k in ('tempo', 'semitones')}
    # A singer default (the kid voice's higher key) only applies where someone new sings.
    if vocal:
        shifts.update(CATALOG['singers'].get(result.get('gender', 'auto'), {}).get('defaults', {}))
    if shifts and 'adjust' not in explicit:
        adjust = dict(result.get('adjust') or {})
        unchanged = {'semitones': 0, 'tempo': 100}
        if adjust.get('speed_mode', 'independent') == 'independent':
            for key, value in shifts.items():
                if adjust.get(key, unchanged[key]) == unchanged[key]:
                    adjust[key] = applied[key] = value
            result['adjust'] = adjust
    return result, applied

def validate_suite(value, *, inherit=False):
    """The orchestral suite setting: on/off plus one style per part (soft, fast, grand)."""
    if inherit:
        if value is None or isinstance(value, bool):
            return value
        raise ValueError('管弦组曲设置无效。')
    if not isinstance(value, dict) or set(value) - {'enabled', 'styles'}:
        raise ValueError('管弦组曲设置无效。')
    enabled = value.get('enabled', False)
    styles = value.get('styles', [part['style'] for part in SUITE])
    if (not isinstance(enabled, bool) or not isinstance(styles, list) or len(styles) != len(SUITE)
            or not all(isinstance(s, str) and 1 <= len(s.strip()) <= 40 for s in styles)
            or len({s.strip().casefold() for s in styles}) != len(SUITE)):
        raise ValueError('管弦组曲需要 3 个不同的风格（每个 1–40 字）。')
    return {'enabled': enabled, 'styles': [s.strip() for s in styles]}

def build_caption(style, instructions='', variant_instructions=''):
    # Genre presets supply defaults only. Explicit requirements replace their
    # instrumentation, avoiding "piano trio" fighting "guitar only" requests.
    preset = style_info(style).get('tags', style)
    base = style if instructions.strip() or variant_instructions.strip() else preset
    return ', '.join(filter(None, [base, instructions.strip(), variant_instructions.strip()]))


def output_name(style, title, limit=200):
    """A portable user-facing filename; originals and job IDs remain untouched."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', f'{style} {title}')
    name = re.sub(r'\s+', ' ', name).strip(' .')
    # Windows counts UTF-16 code units, including surrogate pairs.
    name = name.encode('utf-16-le')[:limit * 2].decode('utf-16-le', errors='ignore').rstrip(' .') or '作品'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
        name = '_' + name
    return name

def effective_controls(config):
    if not config.get('controls_version'):
        return {'change_strength': None, 'melody_mode': 'reference', 'cfg_scale': 1.0}
    strength, melody = validate_controls(config)
    if config['mode'] == 'preserve':
        melody = 'reference'
    # Deliberately moderate bounded CFG range. >1 uses a second decoding branch.
    return {'change_strength': strength, 'melody_mode': melody, 'cfg_scale': round(1 + .008 * strength, 3)}

def build_request(config, score, lyrics, steps):
    controls = effective_controls(config)
    preserve = config['mode'] == 'preserve'
    instrumental = bool(config.get('instrumental'))
    caption = config['caption']
    if preserve or instrumental:
        caption += ', instrumental only, no singing or vocals'
        if controls['melody_mode'] == 'reference':
            caption += ', retain reference melody and timing'
    else:
        caption = ', '.join(filter(None, [caption, GENDERS.get(config.get('gender', 'auto'), ''),
            TONES.get(config.get('tone', 'natural'), ''), 'expressive vocal phrasing, fresh performance']))
    reference = controls['melody_mode'] == 'reference'
    if reference and not score.strip():
        raise ValueError('参考旋律模式需要有效乐谱。')
    return {'style': caption, 'lyrics': '[Instrumental]' if preserve or instrumental else lyrics,
        'abc': score if reference else '', 'cot': 'melody' if reference else 'off',
        'duration': config['duration'], 'lm_seed': config['seed'], 'seed': config['seed'],
        'steps': steps, 'cfg_scale': controls['cfg_scale'], 'output_format': 'wav24', 'peak_clip': 0,
        'semantic_sampling': {'max_tokens': int(config['duration'] * 25), 'min_tokens': int(config['duration'] * 25)}}
