"""Verified HTTPS shared by model downloads and the optional installer."""
import importlib.util
import socket
import ssl
import sys
import urllib.error
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def certificate_bundles():
    paths = []
    if sys.platform == 'darwin':
        paths.append(Path('/etc/ssl/cert.pem'))
    spec = importlib.util.find_spec('certifi')
    if spec and spec.origin:
        paths.append(Path(spec.origin).parent / 'cacert.pem')
    paths.extend(sorted((ROOT / '.ai-env' / 'lib').glob('python*/site-packages/certifi/cacert.pem')))
    paths.append(ROOT / '.ai-env' / 'Lib' / 'site-packages' / 'certifi' / 'cacert.pem')
    return list(dict.fromkeys(p for p in paths if p.is_file()))

def https_context():
    context = ssl.create_default_context()
    for path in certificate_bundles():
        context.load_verify_locations(cafile=str(path))
    return context

def download_error(error, url):
    host = urllib.parse.urlsplit(url).hostname or 'сервер моделей'
    reason = getattr(error, 'reason', error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (f'{host}: не удалось проверить HTTPS-сертификат. На Mac повторите '
                'bash setup-ai-mac.sh; для Python с python.org можно также запустить '
                'Install Certificates.command из папки Python в Applications. Проверка TLS включена.')
    if isinstance(error, urllib.error.HTTPError):
        return f'{host}: HTTP {error.code}. Проверьте доступ к сайту и повторите скачивание.'
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return f'{host}: истекло время ожидания. Проверьте интернет и повторите скачивание.'
    if isinstance(reason, socket.gaierror):
        return f'{host}: не удалось найти сервер (DNS). Проверьте интернет, DNS или VPN.'
    if isinstance(error, PermissionError):
        return 'Нет прав на запись в data/models. Переместите проект в доступную папку пользователя.'
    if isinstance(error, OSError) and error.errno == 28:
        return 'Недостаточно свободного места для модели. Освободите место и повторите скачивание.'
    return f'{host}: ошибка скачивания ({type(reason).__name__}). Проверьте интернет и доступ к сайту.'
