"""Parse CLI message resources and download only into a private temporary directory."""
from __future__ import annotations

import html
import json
import mimetypes
import re
import tempfile
from pathlib import Path

_IMAGE = re.compile(r'\[Image:\s*(img_[\w-]+)\]|!\[[^\]]*\]\((img_[\w-]+)\)')
_FILE = re.compile(r'<(file|audio)\s+([^<>]+?)/?>')
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def content(message_type: str, raw) -> tuple[str, list[dict], str]:
    text = str(raw or '') if not isinstance(raw, dict) else ''
    if message_type not in {'text', 'post', 'image', 'file', 'audio'}:
        return '', [], '当前暂不支持这种消息类型，本次未执行操作。请发送文字、图片或文件。'
    try:
        parsed = raw if isinstance(raw, dict) else json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    attachments = []
    invalid_resource = False

    def add(kind: str, key, name=''):
        nonlocal invalid_resource
        if isinstance(key, str) and re.fullmatch(r'(?:img|file)_[\w-]{1,240}', key):
            attachments.append({'kind': kind, 'file_key': key, 'name': str(name or '')[:200]})
        else:
            invalid_resource = True

    if isinstance(parsed, dict):
        if message_type == 'text':
            text = str(parsed.get('text') or '')
        elif message_type == 'post':
            body = parsed if isinstance(parsed.get('content'), list) else next(
                (value for value in parsed.values() if isinstance(value, dict) and isinstance(value.get('content'), list)), {})
            lines = [str(body.get('title') or '')]
            for paragraph in body.get('content') or []:
                parts = []
                for element in paragraph if isinstance(paragraph, list) else []:
                    if not isinstance(element, dict):
                        continue
                    tag = element.get('tag')
                    if tag in {'text', 'md', 'a', 'at'}:
                        parts.append(str(element.get('text') or element.get('user_name') or ''))
                        if tag == 'a' and element.get('href'):
                            parts.append(str(element['href']))
                    elif tag == 'img':
                        add('image', element.get('image_key'))
                    elif tag == 'media':
                        add('file', element.get('file_key'), element.get('file_name'))
                lines.append(''.join(parts))
            for item in parsed.get('files') or []:
                if isinstance(item, dict):
                    if item.get('is_folder'):
                        return '', [], '文件夹暂不支持，请分别发送文件；本次未执行操作。'
                    add('file', item.get('file_key'), item.get('file_name'))
            text = '\n'.join(lines).strip()
        else:
            kind = message_type
            add(kind, parsed.get('image_key' if kind == 'image' else 'file_key'), parsed.get('file_name'))
            text = ''
    elif message_type != 'text':
        for match in _IMAGE.finditer(text):
            add('image', match.group(1) or match.group(2))
        for match in _FILE.finditer(text):
            attributes = {key: html.unescape(value) for key, value in _ATTR.findall(match.group(2))}
            add('audio' if match.group(1) == 'audio' else 'file', attributes.get('key'), attributes.get('name'))
        if '<folder ' in text:
            return '', [], '文件夹暂不支持，请分别发送文件；本次未执行操作。'
        text = _FILE.sub('', _IMAGE.sub('', text)).strip()
    unique = {(item['kind'], item['file_key']): item for item in attachments}
    attachments = list(unique.values())
    if invalid_resource or (message_type == 'post' and '[Image]' in text):
        return text, [], '消息中的部分附件缺少可读取的资源引用；本次未执行操作，请重新发送。'
    if len(attachments) > 4:
        return text, [], '一条消息最多处理 4 个附件，请分批发送；本次未执行操作。'
    if message_type in {'image', 'file', 'audio'} and not attachments:
        return text, [], '消息没有提供可读取的附件引用，请重新发送；本次未执行操作。'
    return text[:12000], attachments, ''


def resource(message_id: str, item: dict) -> tuple[bytes, str]:
    from mino_scout.plugins import feishu_auth
    from mino_scout.plugins.im_attachments import MAX_BYTES

    key, kind = str(item.get('file_key') or ''), str(item.get('kind') or '')
    if not re.fullmatch(r'om_[\w-]+', message_id) or not re.fullmatch(r'(?:img|file)_[\w-]{1,240}', key):
        raise ValueError('附件缺少可信的消息与资源引用')
    if kind not in {'image', 'file', 'audio'}:
        raise ValueError('不支持的附件类型')
    with tempfile.TemporaryDirectory(prefix='mino-feishu-media-') as directory:
        target = Path(directory) / 'attachment.bin'
        # The CLI only writes under trusted roots (cwd, /tmp, ~/files). Its own
        # cwd is always trusted, so run the download from the private temp dir
        # instead of relying on the platform default temp path.
        data = feishu_auth.run_json(['im', '+messages-resources-download', '--message-id', message_id,
                                    '--file-key', key, '--type', 'image' if kind == 'image' else 'file',
                                    '--output', str(target), '--as', 'bot'], cwd=Path(directory))
        saved = Path(str(data.get('saved_path') or target))
        if saved.is_symlink() or saved.resolve().parent != Path(directory).resolve() or not saved.is_file():
            raise ValueError('附件下载结果不在临时目录内')
        if not 0 < saved.stat().st_size <= MAX_BYTES:
            raise ValueError('附件为空或超过 8 MB，请发送较小的附件')
        return saved.read_bytes(), mimetypes.guess_type(str(item.get('name') or ''))[0] or 'application/octet-stream'
