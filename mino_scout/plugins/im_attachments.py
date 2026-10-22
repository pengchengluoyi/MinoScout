"""附件读取只在 Scout。拒绝本地网络 URL、重定向和超限内容。"""
from __future__ import annotations
import base64
import ipaddress
import mimetypes
import socket
import urllib.request
from urllib.parse import urlparse
from mino_scout.plugins import im_store

MAX_BYTES = 8 * 1024 * 1024
MIMES = {'image/png', 'image/jpeg', 'image/webp', 'audio/wav', 'audio/mpeg', 'audio/ogg', 'audio/mp4'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def save(origin: dict, item: dict) -> dict:
    kind = str(item.get('kind') or '')
    if kind not in {'image', 'audio', 'file'}:
        raise ValueError('不支持的附件类型')
    mime = str(item.get('mime') or '')
    encoded = str(item.get('base64') or '')
    data = b''
    if encoded:
        if len(encoded) > MAX_BYTES * 4 // 3 + 256:
            raise ValueError('附件超过大小限制')
        if encoded.startswith('data:'):
            header, encoded = encoded.split(',', 1)
            mime = header[5:].split(';', 1)[0]
        data = base64.b64decode(encoded, validate=True)
    elif item.get('url'):
        parsed = urlparse(str(item['url']))
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError('不安全的附件地址')
        addresses = {row[4][0] for row in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError('附件地址不是公开网络地址')
        # DNS 不随请求再次解析，阻挡重绑定；TLS 仍用原主机名。
        import http.client
        import ssl
        address = next(iter(addresses))
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=20, context=ssl.create_default_context())
        connection.sock = ssl.create_default_context().wrap_socket(socket.create_connection((address, 443), timeout=20), server_hostname=parsed.hostname)
        try:
            connection.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''), headers={'Host': parsed.hostname})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError('附件暂时无法下载，请重新发送或改用文字')
            mime = response.getheader('Content-Type', '').split(';')[0]
            data = response.read(MAX_BYTES + 1)
        finally:
            connection.close()
    if data and mime not in MIMES:
        if kind == 'image':
            from PIL import Image
            from io import BytesIO
            try:
                with Image.open(BytesIO(data)) as image:
                    mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(image.format, '')
            except Exception as exc:
                raise ValueError('无法读取图片格式') from exc
        elif kind == 'audio':
            mime = 'audio/wav' if data[:4] == b'RIFF' and data[8:12] == b'WAVE' else 'audio/ogg' if data[:4] == b'OggS' else 'audio/mpeg' if data[:3] == b'ID3' or data[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2') else 'audio/mp4' if data[4:8] == b'ftyp' else ''
    if kind == 'file':
        mime = mimetypes.guess_type(str(item.get('name') or ''))[0] or 'application/octet-stream'
    if not data or len(data) > MAX_BYTES or (kind != 'file' and mime not in MIMES):
        raise ValueError('附件为空、格式不支持或超过 8 MB')
    if kind == 'image' and not mime.startswith('image/') or kind == 'audio' and not mime.startswith('audio/'):
        raise ValueError('附件类型不一致')
    ref = im_store.save_attachment(origin, data, mime)
    return {'kind': kind, 'ref': ref, 'mime': mime, 'size': len(data), 'name': str(item.get('name') or '')[:200]}
