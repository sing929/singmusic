"""Release packs for music distributors (Ditto Music, OFFstep): an EP or album made from finished works.

A pack is one local folder with store-ready WAVs (44.1 kHz / 16-bit stereo), a generated 3000x3000
cover that shows only the release title and artist, lyrics in Ditto's line format, the AI-use answers
for every track and a filled-in upload checklist. Nothing is uploaded from here: the pack is entered
on the distributor's own website (by hand, or by Claude in Chrome) and the person submits it there.
Rules follow Ditto's help centre as read on 2026-10-24; OFFstep's upload form is not verified yet."""
import datetime, hashlib, json, os, random, re, shutil, subprocess
from pathlib import Path

import audio_adjust
import dj_tools
from generation_controls import CATALOG

DISTRIBUTORS = {
    'ditto': {'name': 'Ditto Music', 'url': 'https://dittomusic.com/'},
    'offstep': {'name': 'OFFstep', 'url': 'https://offstep.com/'},
}
LANGUAGES = {'zh': 'Chinese (Mandarin) · 中文', 'yue': 'Cantonese · 粤语', 'en': 'English · 英语', 'ms': 'Malay · 马来语',
    'id': 'Indonesian · 印尼语', 'vi': 'Vietnamese · 越南语', 'th': 'Thai · 泰语', 'ko': 'Korean · 韩语', 'ja': 'Japanese · 日语',
    'ta': 'Tamil · 泰米尔语', 'instrumental': 'Instrumental · 纯音乐'}
GENRES = ['Pop', 'Hip-Hop/Rap', 'R&B/Soul', 'Dance', 'Electronic', 'Rock', 'Alternative', 'Metal', 'Jazz', 'Blues', 'Classical',
    'Soundtrack', 'Ambient', 'Folk', 'Country', 'Reggae', 'Latin', 'World', 'K-Pop', 'J-Pop', 'Mandopop', 'Cantopop',
    "Children's Music", 'Gospel', 'Easy Listening']
CREDITS = {'none': 'No AI', 'partly': 'Partly AI', 'fully': 'Fully AI'}
RELEASE_AI = {'none': 'No AI', 'partially': 'Partially AI', 'entirely': 'Entirely AI'}
MAX_TRACKS = 20
# Ditto: "an EP typically features between 4-6 tracks"; many versions of one song can read as spam.
EP_TRACKS = 6
LEAD_DAYS = 14
STORE_RATE = 44100
COVER_SIZE = 3000
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
SPEED_NAMES = {'Speed Up': 'Sped Up'}
SMALL_WORDS = {'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'nor', 'of', 'on', 'or', 'so', 'the', 'to', 'up', 'with', 'yet'}
GROUP_OF = {style: group['id'] for group in CATALOG['groups'] for style in group['styles']}
REMIX_GROUPS = ('asian-dj', 'edm')
# Background top, background bottom, accent, text.
PALETTES = [((22, 46, 44), (39, 75, 54), (217, 120, 84), (245, 240, 226)),
    ((245, 240, 226), (213, 231, 182), (55, 91, 66), (24, 51, 49)),
    ((40, 30, 70), (217, 120, 84), (255, 214, 170), (255, 248, 240)),
    ((12, 40, 70), (30, 110, 130), (213, 231, 182), (240, 248, 245)),
    ((15, 15, 20), (70, 32, 66), (240, 96, 122), (250, 245, 250)),
    ((236, 214, 180), (196, 132, 92), (62, 38, 28), (40, 25, 20))]


def text(payload, key, limit, required=False, label=''):
    value = payload.get(key, '')
    if not isinstance(value, str):
        raise ValueError(f'{label}须为文字。')
    value = re.sub(r'\s+', ' ', value).strip()
    if len(value) > limit or (required and not value):
        raise ValueError(f'{label}须为 {1 if required else 0}–{limit} 字。')
    return value


def title_case(value):
    """Ditto title case for Latin words written all in lower case (or a whole title in capitals);
    words with their own capitals (DJ, iPhone) and Chinese stay as written."""
    letters = [c for c in value if c.isalpha() and c.isascii()]
    shouting = len(letters) > 3 and all(c.isupper() for c in letters)
    words = value.split(' ')
    result = []
    for index, word in enumerate(words):
        plain = word.lower() if shouting else word
        core = re.sub(r'^\W+|\W+$', '', plain)
        after_break = index > 0 and re.search(r'[:\-/–—]$', words[index - 1])
        if core.isascii() and core.isalpha() and core == core.lower():
            small = core in SMALL_WORDS and 0 < index < len(words) - 1 and not after_break
            plain = plain if small else plain.replace(core, core[:1].upper() + core[1:], 1)
        result.append(plain)
    return ' '.join(result)


def song_title(title):
    """The song's own title from an imported file name: no track number, store title case."""
    title = re.sub(r'^\s*\d{1,3}\s*[-._)]\s*', '', str(title or '')).strip() or str(title or '').strip()
    return title_case(re.sub(r'\s+', ' ', title))[:150]


def style_version(style):
    """'House Remix', 'Jazz Version', 'Manyao Remix': the store mix-version text for a catalog style."""
    style = str(style or '').strip()
    latin = re.sub(r'\s+', ' ', re.sub(r'[^\x20-\x7e]', ' ', style)).strip()
    name = latin or style
    if not name:
        return ''
    if re.search(r'\b(remix|mix)\b', name, re.I):
        return name
    group = GROUP_OF.get(style) or next((g for s, g in GROUP_OF.items() if s.casefold() == style.casefold()), '')
    return f'{name} Remix' if group in REMIX_GROUPS else f'{name} Version'


def version_text(job):
    """Mix version of a finished work: its style, suite part, kid singer and later key/tempo or DJ edits."""
    if job.get('kind') == 'mix':
        return 'DJ Mix'
    if job.get('kind') == 'suite':
        return 'Orchestral Suite'
    parts = [style_version(job.get('style'))]
    if job.get('suite_label'):
        parts.append(job['suite_label'])
    if job.get('kid'):
        parts.append('Kid Vocal')
    history = job.get('adjust_history') or ([job['adjust']] if job.get('adjust') else [])
    for adjust in history:
        name = audio_adjust.label(adjust)
        parts.append(SPEED_NAMES.get(name, name))
    if job.get('dj_history'):
        parts.append('DJ Edit')
    return ', '.join(p for p in parts if p)[:80]


def credits(roots):
    """Ditto's per-track AI contribution answers, from the generated works this track is made of.
    The songs are the person's own, so writing starts as No AI; YuE2 always makes the arrangement."""
    if not roots:
        return {'composer': 'none', 'lyricist': 'none', 'performer': 'none', 'producer': 'fully'}
    rank = {'none': 0, 'partly': 1, 'fully': 2}
    worst = lambda values: max(values, key=rank.get) if values else None
    composer, performer = [], []
    vocal = False
    for config in roots:
        composer.append('partly' if config.get('melody_mode') == 'free' and config.get('mode') != 'preserve' else 'none')
        if config.get('instrumental'):
            continue
        vocal = True
        if config.get('mode') == 'preserve':
            performer.append('none')
        elif config.get('voice'):
            performer.append('partly')
        else:
            performer.append('fully')
    return {'composer': worst(composer), 'lyricist': 'none' if vocal else None, 'performer': worst(performer), 'producer': 'fully'}


def release_ai(per_track):
    values = [v for c in per_track for v in c.values() if v]
    if values and all(v == 'fully' for v in values):
        return 'entirely'
    return 'none' if all(v == 'none' for v in values) else 'partially'


def format_lyrics(value):
    """Lyrics in Ditto's format: one line per lyric line, a blank line between sections, no section
    tags or time stamps, no punctuation at line ends, each line starting with a capital, repeats written out."""
    notes = []
    lines = []
    for line in str(value or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = re.sub(r'\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]', '', line).strip()
        # Square-bracket lines and bare section names are tags; a line in round brackets is a tag only
        # when it names a section, so sung ad-libs such as "(Oh oh)" stay.
        section = r'(?i)(verse|chorus|pre-chorus|bridge|intro|outro|hook|refrain|主歌|副歌|间奏|桥段)'
        if (re.fullmatch(r'[\[【].{0,40}[\]】]', line) or re.fullmatch(r'[\(（].{0,40}[\)）]', line) and re.search(section, line)
                or re.fullmatch(section + r'\s*\d*\s*[:：]?', line)):
            if re.search(r'[x×]\s*\d', line, re.I):
                notes.append(f'“{line}”表示重复段落，已删除这一行；请把重复的歌词完整写出。')
            if lines and lines[-1]:
                lines.append('')
            continue
        repeat = re.search(r'\s*[\(（]?\s*[x×]\s*(\d)\s*[\)）]?$', line, re.I)
        count = int(repeat[1]) if repeat else 1
        if repeat:
            line = line[:repeat.start()].rstrip()
        line = re.sub(r'[\s,，.。;；:：、]+$', '', line)
        if not line:
            if lines and lines[-1]:
                lines.append('')
            continue
        latin = [c for c in line if c.isascii() and c.isalpha()]
        if len(latin) > 3 and all(c.isupper() for c in latin):
            line = line.lower()
        line = re.sub(r'^([^A-Za-z]*)([a-z])', lambda m: m[1] + m[2].upper(), line, count=1)
        lines.extend([line] * count)
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines), notes


def release_type(count, seconds):
    """Store release type (Spotify's rule): 1–3 tracks under 30 minutes a single, 4–6 an EP, else an album."""
    if count <= 3 and seconds < 1800:
        return 'Single'
    return 'EP' if count <= 6 and seconds < 1800 else 'Album'


def display_title(track):
    return track['title'] + (f' ({track["version"]})' if track.get('version') else '')


def safe_name(value, limit=120):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')[:limit].rstrip(' .') or 'track'
    return '_' + value if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I) else value


def clean_tracks(value):
    if not isinstance(value, list) or len(value) > MAX_TRACKS:
        raise ValueError(f'每个发行最多 {MAX_TRACKS} 首。')
    tracks = []
    for item in value:
        if not isinstance(item, dict) or not re.fullmatch('[0-9a-f]{32}', str(item.get('job_id', ''))):
            raise ValueError('曲目无效。')
        isrc = text(item, 'isrc', 15, label='ISRC').replace('-', '').upper()
        if isrc and not re.fullmatch('[A-Z]{2}[A-Z0-9]{3}[0-9]{7}', isrc):
            raise ValueError('ISRC 须为 12 位，如 GBLFP2600001；留空由平台生成。')
        snippet = item.get('snippet')
        if snippet is not None and (not isinstance(snippet, (int, float)) or not 0 <= snippet <= 1200):
            raise ValueError('试听片段开始时间无效。')
        if not isinstance(item.get('explicit', False), bool):
            raise ValueError('露骨内容设置无效。')
        tracks.append({'job_id': item['job_id'], 'title': text(item, 'title', 150, label='曲目名称'), 'version': text(item, 'version', 80, label='版本说明'),
            'explicit': item.get('explicit', False), 'isrc': isrc, 'snippet': None if snippet is None else round(float(snippet))})
    if len({t['job_id'] for t in tracks}) != len(tracks):
        raise ValueError('同一个作品在这个发行里只能放一次。')
    return tracks


def apply(release, payload):
    """Validated edits of a release; the caller decides which releases may still change."""
    value = dict(release)
    if 'distributor' in payload:
        if payload['distributor'] not in DISTRIBUTORS:
            raise ValueError('请选择发行平台。')
        value['distributor'] = payload['distributor']
    for key, limit, label in (('title', 150, '发行名称'), ('artist', 100, '艺人名'), ('credit_name', 100, '署名真名'), ('label', 100, '厂牌'),
            ('genre', 60, '主风格'), ('subgenre', 60, '副风格'), ('copyright_holder', 100, '版权所有人'), ('notes', 2000, '备注')):
        if key in payload:
            value[key] = text(payload, key, limit, label=label)
    if 'language' in payload:
        if payload['language'] not in LANGUAGES:
            raise ValueError('请选择歌词语言。')
        value['language'] = payload['language']
    if 'copyright_year' in payload:
        year = payload['copyright_year']
        if not isinstance(year, int) or isinstance(year, bool) or not 1900 <= year <= 2100:
            raise ValueError('版权年份无效。')
        value['copyright_year'] = year
    if 'release_date' in payload:
        date = payload['release_date']
        if date != '':
            try:
                datetime.date.fromisoformat(str(date))
            except ValueError:
                raise ValueError('发行日期无效。') from None
        value['release_date'] = date
    if 'upc' in payload:
        upc = text(payload, 'upc', 14, label='UPC')
        if upc and not re.fullmatch(r'\d{12,14}', upc):
            raise ValueError('UPC 须为 12–14 位数字；留空由平台生成。')
        value['upc'] = upc
    if 'tracks' in payload:
        value['tracks'] = clean_tracks(payload['tracks'])
    return value


def review(release, entries, others, today=None):
    """Problems that stop a pack, warnings worth reading, and the computed release facts.
    `entries` holds one dict per track: track, job (None when missing), roots, lyrics {text, source}, wav;
    `others` are the person's other releases that are not deleted."""
    today = today or datetime.date.today()
    problems, warnings = [], []
    name = DISTRIBUTORS.get(release.get('distributor'), {}).get('name', '平台')
    for key, label in (('title', '发行名称'), ('artist', '艺人名'), ('genre', '主风格'), ('copyright_holder', '版权所有人')):
        if not release.get(key):
            problems.append(f'请填写{label}。')
    if release.get('language') not in LANGUAGES:
        problems.append('请选择歌词语言。')
    if not release.get('release_date'):
        problems.append('请选择发行日期。')
    else:
        date = datetime.date.fromisoformat(release['release_date'])
        if date <= today:
            problems.append('发行日期须在今天之后。')
        elif (date - today).days < LEAD_DAYS:
            warnings.append(f'发行日期距今不足 {LEAD_DAYS} 天：{name} 建议至少提前两周上传，平台审核和 Spotify 推歌都需要时间。')
    tracks = release.get('tracks', [])
    if not tracks:
        problems.append('请至少加入一首作品。')
    taken = {}
    for other in others:
        # A work belongs to the release that took it first, or to one already submitted; the later draft yields.
        if other['id'] == release['id'] or (other.get('status') != 'submitted' and other.get('created_at', 0) >= release.get('created_at', 0)):
            continue
        for t in other.get('tracks', []):
            taken[t['job_id']] = other
    names = {}
    per_song = {}
    seconds = 0.
    per_track = []
    for number, entry in enumerate(entries, 1):
        t, job = entry['track'], entry['job']
        label = f'第 {number} 首'
        if job is None:
            problems.append(f'{label}的作品已不存在或已删除，请移出这个发行。')
            continue
        seconds += entry.get('seconds') or 0.
        per_track.append(credits(entry['roots']))
        if not t['title']:
            problems.append(f'{label}请填写曲目名称。')
        key = (t['title'].casefold(), t['version'].casefold())
        if key in names:
            problems.append(f'第 {names[key]} 首和{label}的名称与版本完全相同，请修改版本说明。')
        names.setdefault(key, number)
        if t['job_id'] in taken:
            other = taken[t['job_id']]
            problems.append(f'{label}《{display_title(t)}》已在发行《{other.get("title") or "未命名"}》（{DISTRIBUTORS[other["distributor"]]["name"]}）里；同一录音只能经一个平台发行一次，否则会被判为重复内容下架。')
        for parent in entry.get('parents', []):
            if parent in taken and taken[parent]['distributor'] != release.get('distributor'):
                warnings.append(f'{label}由发行《{taken[parent].get("title") or "未命名"}》里的作品变调变速或加效果而来，两个平台各发一份可能被 YouTube Content ID 判为同一录音。')
                break
        per_song.setdefault(job.get('track_id') or job['id'], []).append(number)
        roots = entry['roots']
        vocal = any(not r.get('instrumental') for r in roots)
        if vocal and credits(roots)['performer'] == 'fully':
            warnings.append(f'{label}的歌声由 YuE2 生成，不是你的声音；声明里演唱者为 Fully AI。如果只想发行你本人演唱的版本，请换成“保留原唱”或换成原唱音色的作品。')
        lyrics = entry.get('lyrics') or {}
        if vocal and job.get('kind') != 'mix':
            if not lyrics.get('text', '').strip():
                warnings.append(f'{label}没有歌词：可以先发行，之后在平台补上。')
            elif lyrics.get('source', '').startswith('whisper'):
                warnings.append(f'{label}的歌词由本机自动识别，可能有错字，请在发行包的歌词文件里核对后再上传。')
    for song, numbers in per_song.items():
        if len(numbers) > EP_TRACKS:
            warnings.append(f'同一首歌放了 {len(numbers)} 个版本：Ditto 不接受为刷量而做的重复内容，建议每个发行同一首歌不超过 {EP_TRACKS} 个版本。')
    if len(tracks) > EP_TRACKS:
        warnings.append(f'共 {len(tracks)} 首，会按专辑发行（EP 一般 4–6 首）。')
    if release.get('distributor') == 'offstep':
        warnings.append('OFFstep 的上传页面还没有逐项核对，清单按通用字段列出，第一次上传时请对照。')
    return {'problems': problems, 'warnings': list(dict.fromkeys(warnings)), 'release_type': release_type(len(tracks), seconds),
        'seconds': seconds, 'ai_use': release_ai(per_track) if per_track else None, 'explicit': any(t.get('explicit') for t in tracks)}


def cover_key(release):
    raw = json.dumps([release.get('title', ''), release.get('artist', ''), release.get('cover_seed', 0)], ensure_ascii=False)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]


def font(size, bold=True):
    from PIL import ImageFont
    folder = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
    for name in (('msyhbd.ttc', 'arialbd.ttf') if bold else ('msyh.ttc', 'arial.ttf')):
        try:
            return ImageFont.truetype(str(folder / name), size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def wrap(draw, value, face, width):
    """Up to two lines that fit `width`; words for Latin text, characters for Chinese."""
    tokens = re.findall(r'\S+\s*', value) if ' ' in value else list(value)
    lines, line = [], ''
    for token in tokens:
        if line and draw.textlength(line + token, font=face) > width:
            lines.append(line.rstrip())
            line = token
        else:
            line += token
    lines.append(line.rstrip())
    return lines


def make_cover(path, title, artist, seed):
    """A 3000x3000 RGB JPEG: gradient, sound rings and a waveform, with only the release title and artist
    as text (Ditto rejects any other text, and the title must match the metadata exactly)."""
    import numpy as np
    from PIL import Image, ImageDraw
    rng = random.Random(seed)
    top, bottom, accent, ink = PALETTES[rng.randrange(len(PALETTES))]
    size = COVER_SIZE
    mix = np.linspace(0, 1, size)[:, None, None]
    gradient = np.array(top)[None, None] * (1 - mix) + np.array(bottom)[None, None] * mix
    image = Image.fromarray(np.repeat(gradient, size, axis=1).astype('uint8'), 'RGB').convert('RGBA')
    measure = ImageDraw.Draw(image)
    # Text first: the largest title size that fits in two lines (three for very long titles).
    width, margin = size - 440, 220
    for title_size in range(300, 100, -20):
        face = font(title_size)
        lines = wrap(measure, title, face, width)
        if len(lines) <= (2 if title_size > 160 else 3) and all(measure.textlength(l, font=face) <= width for l in lines):
            break
    step_y, artist_face = int(title_size * 1.18), font(140, bold=False)
    text_top = size - margin - 170 - len(lines) * step_y
    layer = Image.new('RGBA', (size, size))
    draw = ImageDraw.Draw(layer)
    cx, cy = rng.randint(size // 2, size - 400), rng.randint(300, max(301, text_top // 2))
    for ring in range(rng.randint(7, 12)):
        radius = 260 + ring * rng.randint(150, 210)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=accent + (max(30, 150 - ring * 14),), width=rng.randint(8, 18))
    # The waveform sits above the text with clear space, so the title never crosses it.
    bars, tallest = 72, min(320, max(120, (text_top - 500) // 2))
    base = text_top - 140 - tallest
    heights = [abs(rng.gauss(0, 1)) for _ in range(bars)]
    heights = [(heights[max(0, i - 1)] + heights[i] * 2 + heights[min(bars - 1, i + 1)]) / 4 for i in range(bars)]
    step = width / bars
    for i, h in enumerate(heights):
        envelope = .35 + .65 * np.sin(np.pi * (i + .5) / bars)
        half = max(14, min(tallest, int(h * tallest * envelope)))
        x = margin + i * step
        draw.rounded_rectangle((x, base - half, x + step * .55, base + half), radius=int(step * .27), fill=accent + (215,))
    image = Image.alpha_composite(image, layer).convert('RGB')
    draw = ImageDraw.Draw(image)
    y = text_top
    for line in lines:
        draw.text((margin, y), line, font=face, fill=ink)
        y += step_y
    draw.text((margin + 4, y + 40), artist, font=artist_face, fill=ink)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.new.jpg')
    image.save(temporary, 'JPEG', quality=92, dpi=(300, 300), optimize=True)
    temporary.replace(path)
    return path


def export_wav(ffmpeg, source, target, seconds):
    """Store master: 44.1 kHz / 16-bit stereo PCM with triangular dither and no tags; checked after writing."""
    import numpy as np
    import soundfile as sf
    subprocess.run([str(ffmpeg), '-nostdin', '-y', '-v', 'error', '-i', str(source), '-map', '0:a:0', '-map_metadata', '-1',
        '-af', f'aresample={STORE_RATE}:dither_method=triangular', '-ac', '2', '-c:a', 'pcm_s16le', str(target)],
        check=True, capture_output=True, creationflags=FLAGS, timeout=max(120, int(seconds)))
    info = sf.info(str(target))
    duration = info.frames / info.samplerate
    if info.samplerate != STORE_RATE or info.channels != 2 or info.subtype != 'PCM_16' or abs(duration - seconds) > .5:
        raise RuntimeError(f'导出的 WAV 未通过检查：{target.name}')
    levels, peak = [], 0.
    for block in sf.blocks(str(target), blocksize=STORE_RATE, always_2d=True, dtype='float32'):
        levels.append(float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.)
        peak = max(peak, float(np.max(np.abs(block))) if block.size else 0.)
    window = min(30, len(levels))
    sums = np.convolve(np.square(levels), np.ones(window), 'valid') if levels else np.zeros(1)
    silent = longest = 0
    for level in levels:
        silent = silent + 1 if level < 1e-3 else 0
        longest = max(longest, silent)
    return {'seconds': round(duration, 2), 'sample_rate': info.samplerate, 'bits': 16, 'channels': info.channels,
        'peak_dbfs': round(20 * np.log10(max(peak, 1e-9)), 1), 'longest_silence': longest, 'snippet': int(np.argmax(sums)),
        'bytes': target.stat().st_size}


def checklist(meta):
    """The filled-in upload checklist: Ditto's Release Builder order, English field names, local values."""
    r = meta['release']
    dist = meta['distributor']
    lines = [f'# 上传清单 · {r["title"]}', '',
        f'平台：{dist["name"]}（{dist["url"]}）· 类型：{r["release_type"]} · {len(meta["tracks"])} 首 · 发行日期 {r["release_date"]}', '',
        '本文件夹由 Sing Studio 生成，只在本机。登录、验证码、同意条款、付款和最后的 Submit 都由你本人在平台网站完成。', '']
    if dist['id'] == 'offstep':
        lines += ['> OFFstep 的上传页面尚未逐项核对：以下按 Ditto 的顺序列出相同字段，请对照网站填写。', '']
    lines += ['## 1. 发行信息（Release details）', '',
        f'- Release title: {r["title"]}', f'- Primary artist: {r["artist"]}', f'- Record label: {r["label"] or r["artist"]}',
        f'- Primary genre: {r["genre"]}' + (f' · Secondary genre: {r["subgenre"]}' if r['subgenre'] else ' · Secondary genre: 可留空'),
        f'- Language: {r["language_name"]}', f'- Copyright (© / ℗): {r["copyright_year"]} {r["copyright_holder"]}',
        f'- Previously released: No', f'- UPC: {r["upc"] or "留空，由平台生成"}', '',
        '## 2. 曲目（Tracklist）', '',
        f'AI 声明（Release 级别）：**{RELEASE_AI[r["ai_use"]]}**。选 Partially AI 后，每首在 Edit Metadata → Track Credits 里按下表设置 AI Contribution Type，署名填 {r["credit_name"] or r["artist"]}（你本人，不填 AI 工具）。', '']
    for t in meta['tracks']:
        c = t['ai_credits']
        lines += [f'### {t["number"]:02d}. {t["display_title"]}', '',
            f'- Audio file: `{t["file"]}` · {t["seconds"]:.0f} 秒 · 44.1 kHz / 16-bit', f'- Track title: {t["title"]}',
            f'- Track mix version: {t["version"] or "留空"}', f'- Explicit: {"Yes" if t["explicit"] else "No"}',
            f'- ISRC: {t["isrc"] or "留空，由平台生成"}', f'- TikTok / SmartLink snippet start: {t["snippet_start"] // 60}:{t["snippet_start"] % 60:02d}',
            '- Credits / AI Contribution Type: ' + ' · '.join(f'{role.title()} {CREDITS[v]}' for role, v in c.items() if v)]
        lines.append(f'- Lyrics: 打开 `{t["lyrics_file"]}` 全部复制，粘贴到 “Add lyrics?”' if t['lyrics_file'] else '- Lyrics: 无')
        lines.append('')
    lines += ['## 3. 封面（Artwork）', '', '- 上传 `cover.jpg`（3000 × 3000 · RGB · JPG）。封面只有发行名称和艺人名，与上面的资料一致。', '',
        '## 4. 平台与日期（Stores & release date）', '', f'- 选择全部商店（或按需要取消），发行日期选 {r["release_date"]}。', '',
        '## 5. 提交前', '', '- 逐项核对后再按 Submit；提交后回到 Sing Studio 把这个发行标记为“已提交”，以免同一录音再发到另一个平台。', '']
    return '\n'.join(lines)


def build(release, entries, facts, directory, ffmpeg, version):
    """Write the pack into `directory` (replaced as a whole) and return its record."""
    temporary = directory.with_name(directory.name + '.new')
    shutil.rmtree(temporary, ignore_errors=True)
    (temporary / 'lyrics').mkdir(parents=True)
    tracks = []
    for number, entry in enumerate(entries, 1):
        t = entry['track']
        stem = safe_name(f'{number:02d} - {display_title(t)}')
        stats = export_wav(ffmpeg, entry['wav'], temporary / (stem + '.wav'), entry['seconds'])
        lyrics_file = None
        roots = entry['roots']
        lyrics = (entry.get('lyrics') or {}).get('text', '')
        if lyrics.strip() and any(not r.get('instrumental') for r in roots):
            body, _ = format_lyrics(lyrics)
            lyrics_file = f'lyrics/{stem}.txt'
            (temporary / lyrics_file).write_text(body + '\n', encoding='utf-8')
        tracks.append({'number': number, 'title': t['title'], 'version': t['version'], 'display_title': display_title(t), 'file': stem + '.wav',
            'seconds': stats['seconds'], 'explicit': t['explicit'], 'isrc': t['isrc'],
            'snippet_start': t['snippet'] if t['snippet'] is not None else stats['snippet'], 'lyrics_file': lyrics_file,
            'lyrics_source': (entry.get('lyrics') or {}).get('source', ''), 'ai_credits': credits(roots), 'checks': stats,
            'source': {'job_id': t['job_id'], 'style': entry['job'].get('style', ''), 'app_version': entry['job'].get('config', {}).get('app_version')}})
    make_cover(temporary / 'cover.jpg', release['title'], release['artist'], release.get('cover_seed', 0))
    dist = {'id': release['distributor'], **DISTRIBUTORS[release['distributor']]}
    meta = {'release_id': release['id'], 'distributor': dist, 'made_with': {'app': 'Sing Studio', 'version': version},
        'release': {k: release.get(k, '') for k in ('title', 'artist', 'credit_name', 'label', 'genre', 'subgenre', 'language', 'copyright_holder', 'copyright_year', 'release_date', 'upc')}
            | {'language_name': LANGUAGES[release['language']], 'release_type': facts['release_type'], 'explicit': facts['explicit'], 'ai_use': facts['ai_use']},
        'cover': 'cover.jpg', 'tracks': tracks, 'created_at': datetime.datetime.now().isoformat(timespec='seconds')}
    (temporary / 'metadata.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    (temporary / '上传清单 Upload checklist.md').write_text(checklist(meta), encoding='utf-8')
    shutil.rmtree(directory, ignore_errors=True)
    temporary.replace(directory)
    warnings = [f'第 {t["number"]} 首峰值达到 {t["checks"]["peak_dbfs"]} dBFS，可能有削波。' for t in tracks if t['checks']['peak_dbfs'] >= -.1]
    warnings += [f'第 {t["number"]} 首有 {t["checks"]["longest_silence"]} 秒连续静音，平台不建议长段静音。' for t in tracks if t['checks']['longest_silence'] >= 10]
    return {'files': ['cover.jpg', 'metadata.json', '上传清单 Upload checklist.md'] + [t['file'] for t in tracks] + [t['lyrics_file'] for t in tracks if t['lyrics_file']],
        'tracks': [{'number': t['number'], 'file': t['file'], **t['checks']} for t in tracks], 'warnings': warnings}


def owner(folder):
    try:
        return json.loads((folder / 'metadata.json').read_text(encoding='utf-8')).get('release_id')
    except (OSError, ValueError, AttributeError):
        return None


def export(pack, root, title, release_id, previous=None):
    """Copy a finished pack to <root>/<EP title>/ for automation such as n8n. The copy appears whole or
    not at all (written as a hidden .partial folder, then renamed). A folder of the same name that holds
    another release keeps its place and this one gets a numbered name; this release's own earlier copy
    under an older title is removed so only the current pack is offered."""
    root = Path(root)
    base = safe_name(title or 'EP', 80)
    for number in range(1, 100):
        target = root / (base if number == 1 else f'{base} ({number})')
        if not target.exists() or owner(target) == release_id:
            break
    else:
        raise RuntimeError('导出文件夹里同名的 EP 太多，请换一个发行名称。')
    temporary = root / f'.{target.name}.partial'
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(pack, temporary)
    if target.exists():
        shutil.rmtree(target)
    temporary.replace(target)
    if previous:
        old = Path(previous)
        if old != target and old.parent == root and old.is_dir() and owner(old) == release_id:
            shutil.rmtree(old, ignore_errors=True)
    return target
