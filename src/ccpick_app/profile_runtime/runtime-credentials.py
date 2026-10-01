import json
import os
from pathlib import Path
import sys

def main():
    if sys.platform != 'darwin' or len(sys.argv) != 4:
        raise ValueError('credential_adapter_invalid')
    install_path, directory_arg, operation = sys.argv[1:]
    install = json.loads(Path(install_path).read_text(encoding='utf-8-sig'))
    directory = Path(directory_arg)
    root = Path(install['dataRoot']).parent
    if not directory.is_absolute() or directory.resolve() != directory or (not directory.is_relative_to(root)):
        raise ValueError('unsafe_path')
    from claude_swap import macos_keychain
    from claude_swap.session import keychain_service_name, read_config_dir_credentials
    service = keychain_service_name(directory_arg)
    if operation == 'read':
        raw = read_config_dir_credentials(directory_arg, strict_keychain=True, keychain_service=service)
        print(raw or '{}')
    elif operation == 'write':
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or not isinstance(json.loads(raw), dict):
            raise ValueError('credential_adapter_invalid')
        macos_keychain.set_password(service, macos_keychain.keychain_account_name(), raw)
        shadow = directory / '.credentials.json'
        if shadow.exists():
            if shadow.is_symlink() or not shadow.is_file():
                raise ValueError('unsafe_path')
            temporary = shadow.with_name('.credentials.runtime-' + str(os.getpid()) + '.tmp')
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    stream.write(raw)
                os.replace(temporary, shadow)
            finally:
                temporary.unlink(missing_ok=True)
        print('{"ok":true}')
    else:
        raise ValueError('credential_adapter_invalid')
if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"error":"credential_store_unavailable"}')
        sys.exit(1)
