"""Versioned, inspectable controls for the installed YuE2 JSON protocol.

CFG acts on style AND lyrics/voice conditioning, not a guaranteed timbre transfer.
Reference score is unchanged. Free mode explicitly omits it (cot=off).
"""
import json
import math
from pathlib import Path

RELEASE = json.loads(Path(__file__).with_name('release.json').read_text(encoding='utf-8'))
VERSION = RELEASE['version']
CONTROL_VERSION = 1
DEFAULT_STRENGTH = 75
LEGACY_INSTRUCTIONS = '温暖的电钢琴，松弛的节奏，保留原曲旋律与情绪。'
STYLE_TAGS = {
    'R&B': 'R&B, syncopated groove, electric piano, deep round bass, soulful phrasing',
    'Lo-fi': 'lo-fi hip hop, dusty drums, tape texture, muted keys, intimate mellow arrangement',
    'Jazz': 'jazz, swinging rhythm, acoustic piano trio, upright bass, brushed drums, improvised fills',
    'Acoustic': 'acoustic folk, fingerpicked acoustic guitar, organic percussion, sparse unplugged arrangement',
    'Remix': 'electronic dance remix, four on the floor kick, driving synth bass, energetic synthesizers, dance arrangement',
    'Phonk': 'phonk, gritty Memphis rap atmosphere, distorted 808 bass, cowbell melody, punchy trap drums, dark lo-fi texture',
    'Hardstyle': 'hardstyle, hard distorted pitched kick, reverse bass, driving four on the floor rhythm, euphoric supersaw lead, dramatic builds and drops',
    'Hardtekk': 'hardtekk, rapid pounding four on the floor kicks, clipped percussive bass, raw repetitive rave groove, minimal abrasive synth stabs',
}
GENDERS = {'auto': '', 'male': 'male vocals, solo male singer', 'female': 'female vocals, solo female singer'}
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

def build_caption(style, instructions='', variant_instructions=''):
    # Genre presets supply defaults only. Explicit requirements replace their
    # instrumentation, avoiding "piano trio" fighting "guitar only" requests.
    preset = next((tags for name, tags in STYLE_TAGS.items() if name.casefold() == style.strip().casefold()), style)
    base = style if instructions.strip() or variant_instructions.strip() else preset
    return ', '.join(filter(None, [base, instructions.strip(), variant_instructions.strip()]))

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
    caption = config['caption']
    if preserve:
        caption += ', instrumental only, no singing or vocals, retain reference melody and timing'
    else:
        caption = ', '.join(filter(None, [caption, GENDERS.get(config.get('gender', 'auto'), ''),
            TONES.get(config.get('tone', 'natural'), ''), 'expressive vocal phrasing, fresh performance']))
    reference = controls['melody_mode'] == 'reference'
    if reference and not score.strip():
        raise ValueError('参考旋律模式需要有效乐谱。')
    return {'style': caption, 'lyrics': '[Instrumental]' if preserve else lyrics,
        'abc': score if reference else '', 'cot': 'melody' if reference else 'off',
        'duration': config['duration'], 'lm_seed': config['seed'], 'seed': config['seed'],
        'steps': steps, 'cfg_scale': controls['cfg_scale'], 'output_format': 'wav24', 'peak_clip': 0,
        'semantic_sampling': {'max_tokens': int(config['duration'] * 25), 'min_tokens': int(config['duration'] * 25)}}
