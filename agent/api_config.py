"""Portable OpenAI-compatible connection settings, kept outside the repository."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit, urlunsplit

from openai import OpenAI


@dataclass
class APIConfig:
    base_url: str = 'https://api.openai.com/v1'
    api_key: str = field(default='', repr=False)
    model: str = ''
    skip_setup: bool = False
    remember_key: bool = False

    @property
    def ready(self):
        return bool(self.api_key and self.model and self.base_url)


def config_path():
    override = os.environ.get('GPU_ANALYSIS_CONFIG')
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return root / 'gpu-data-analysis' / 'connection.json'


def normalize_url(value):
    url = urlsplit(value.strip())
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('请输入不带账号、查询参数的 API 基础地址，例如 https://example.com/v1')
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('远程 API 必须使用 HTTPS；本机服务可使用 http://localhost')
    try:
        url.port
    except ValueError:
        raise ValueError('API 地址的端口无效') from None
    path = url.path.rstrip('/') or '/v1'
    return urlunsplit((url.scheme, url.netloc, path, '', ''))


def load_config(path=None):
    path = Path(path) if path else config_path()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, OSError):
            raise ValueError('本机 API 配置无法读取，请检查 connection.json（不会显示密钥）') from None
    config = APIConfig(**{key: data[key] for key in APIConfig.__dataclass_fields__ if key in data})
    if not all(isinstance(getattr(config, key), str) for key in ('base_url', 'api_key', 'model')):
        raise ValueError('本机 API 配置字段类型无效')
    if not all(isinstance(getattr(config, key), bool) for key in ('skip_setup', 'remember_key')):
        raise ValueError('本机 API 提示偏好类型无效')
    # Treat credentials as an address-bound bundle: never send an old provider's key
    # to a newly selected endpoint just because it remains in the environment.
    generic_url = os.environ.get('GPU_API_BASE_URL') or os.environ.get('OPENAI_BASE_URL')
    generic_key = os.environ.get('GPU_API_KEY') or os.environ.get('OPENAI_API_KEY')
    generic_model = os.environ.get('GPU_API_MODEL') or os.environ.get('OPENAI_MODEL')
    legacy_url = os.environ.get('STEPFUN_BASE_URL', 'https://api.stepfun.com/step_plan/v1')
    if generic_url or (generic_key and not data):
        target = normalize_url(generic_url or 'https://api.openai.com/v1')
        same = target == normalize_url(config.base_url)
        config.api_key = generic_key or (config.api_key if same else '')
        config.model = generic_model or (config.model if same else '')
        config.base_url = target
    elif data:
        if generic_key:
            config.api_key = generic_key
        elif normalize_url(config.base_url) == normalize_url(legacy_url):
            config.api_key = os.environ.get('STEPFUN_API_KEY') or config.api_key
        if generic_model:
            config.model = generic_model
    elif os.environ.get('STEPFUN_API_KEY') or os.environ.get('STEPFUN_BASE_URL'):
        config.base_url = normalize_url(legacy_url)
        config.api_key = os.environ.get('STEPFUN_API_KEY', '')
        config.model = os.environ.get('STEPFUN_MODEL', 'step-3.7-flash')
    else:
        config.model = generic_model or ''
    config.base_url = normalize_url(config.base_url)
    if not all(isinstance(getattr(config, key), str) for key in ('base_url', 'api_key', 'model')):
        raise ValueError('本机 API 配置字段类型无效')
    return config


def save_config(config, path=None):
    path = Path(path) if path else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(base_url=normalize_url(config.base_url), model=config.model.strip(),
                skip_setup=bool(config.skip_setup), remember_key=bool(config.remember_key))
    if config.remember_key:
        data['api_key'] = config.api_key
    # Atomic replacement and owner-only permissions on POSIX. Windows inherits the
    # user's config-directory ACL; the UI explicitly describes this as plaintext.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.connection-', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def create_client(config):
    if not config.ready:
        raise ValueError('尚未配置 API 地址、密钥和模型；请使用 /settings')
    return OpenAI(api_key=config.api_key, base_url=normalize_url(config.base_url))


def discover_models(config):
    if not config.api_key:
        raise ValueError('请先输入 API Key；本地无鉴权服务可填 local')
    # Request only the selected address. Do not probe other providers with this key.
    with OpenAI(api_key=config.api_key, base_url=normalize_url(config.base_url),
                timeout=10, max_retries=0) as client:
        try:
            response = client.models.list()
            models = sorted({item.id for item in response.data if isinstance(item.id, str) and item.id})
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            detail = f'HTTP {status}' if status else type(exc).__name__
            raise ValueError(f'无法读取模型列表（{detail}）。检查地址和密钥，或手填支持工具调用的模型名。') from None
    if not models:
        raise ValueError('服务返回了空模型列表；请手填模型名。')
    return models


def terminal_setup(config):
    """Optional-dependency fallback; getpass never echoes the key."""
    from dataclasses import replace
    from getpass import getpass
    print('\nGPU加速与数据分析 · API 设置（OpenAI 兼容接口）')
    print('Enter 配置；输入 skip 暂时跳过，ignore 永久忽略启动提示。/settings 可随时修改。')
    try:
        choice = input('选择 [Enter / skip / ignore]：').strip().lower()
        if choice in ('skip', 'ignore'):
            if choice == 'ignore':
                config = replace(config, skip_setup=True)
                save_config(config)
            return config
        url = normalize_url(input(f'API 地址 [{config.base_url}]：') or config.base_url)
        same = url == config.base_url
        key = getpass('API Key（隐藏输入，留空仅保留同地址的现有密钥）：') or (config.api_key if same else '')
        updated = replace(config, base_url=url, api_key=key, model=config.model if same else '')
        if input('读取该地址的模型列表？[y/N]：').lower() == 'y':
            try:
                models = discover_models(updated)
                for index, name in enumerate(models, 1):
                    print(f'{index}. {name}')
                selected = input('选择序号，或留空手填：').strip()
                if selected:
                    updated.model = models[int(selected) - 1] if 0 < int(selected) <= len(models) else ''
            except (ValueError, IndexError) as exc:
                print(str(exc))
        updated.model = input(f'模型名 [{updated.model}]：').strip() or updated.model
        if not updated.ready:
            print('配置不完整，保留原设置。')
            return config
        updated.skip_setup = input('以后不再弹出启动设置？[y/N]：').lower() == 'y'
        updated.remember_key = input('保存密钥到本机明文文件（请勿共享）？[y/N]：').lower() == 'y'
        save_config(updated)
        return updated
    except (EOFError, KeyboardInterrupt):
        return config
    except (ValueError, OSError):
        print('配置无效或无法保存，保留原设置。')
        return config
