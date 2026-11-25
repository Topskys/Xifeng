import io
files = {
    'Cargo.toml': 'version = "0.2.0"',
    'apps/desktop/src-tauri/Cargo.toml': 'version = "0.2.0"',
    'package.json': '"version": "0.2.0"',
    'core/pyproject.toml': 'version = "0.2.0"',
    'apps/desktop/src-tauri/tauri.conf.json': '"version": "0.2.0"',
    'core/xifeng/__init__.py': '__version__ = "0.2.0"',
}
for f, old in files.items():
    with io.open(f, 'r', encoding='utf-8') as fp:
        t = fp.read()
    if old not in t:
        print(f'{f}: 旧版本字符串未找到')
        continue
    new = t.replace('0.2.0', '0.3.0')
    with io.open(f, 'w', encoding='utf-8') as fp:
        fp.write(new)
    print(f'{f}: OK')