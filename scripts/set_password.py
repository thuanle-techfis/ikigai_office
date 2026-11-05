#!/usr/bin/env python3
"""Reset an account password directly in a data directory.

    python3 scripts/set_password.py thuanle --data-dir data-prod            # generate a strong one
    python3 scripts/set_password.py thuanle --data-dir data-prod --prompt   # type your own

Stop the server that uses this data directory first: a running server keeps the
database in memory and would overwrite the change on its next save.
All existing logins of the account are signed out.
"""

import argparse
import getpass
import json
import os
import secrets
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

MIN_LENGTH = 12


def generate():
    """20 characters from letters, digits and symbols, with each class present."""
    alphabet = string.ascii_letters + string.digits + '-_.!@#%+='
    while True:
        value = ''.join(secrets.choice(alphabet) for _ in range(20))
        if (any(c.islower() for c in value) and any(c.isupper() for c in value)
                and any(c.isdigit() for c in value) and any(not c.isalnum() for c in value)):
            return value


def atomic_write(path, data):
    tmp = str(path) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('username')
    parser.add_argument('--data-dir', default=str(ROOT / 'data'))
    parser.add_argument('--prompt', action='store_true', help='type the new password instead of generating one')
    args = parser.parse_args()

    db_file = Path(args.data_dir) / 'db.json'
    db = json.loads(db_file.read_text(encoding='utf-8'))
    found = [(uid, acc) for uid, acc in db.get('accounts', {}).items()
             if acc.get('usernameLower') == args.username.lower()]
    if not found:
        sys.exit(f'Không tìm thấy tài khoản "{args.username}" trong {db_file}')
    uid, account = found[0]

    if args.prompt:
        password = getpass.getpass('Mật khẩu mới: ')
        if password != getpass.getpass('Nhập lại: '):
            sys.exit('Hai lần nhập không khớp.')
        if len(password) < MIN_LENGTH or password.lower() == args.username.lower():
            sys.exit(f'Mật khẩu cần ít nhất {MIN_LENGTH} ký tự và khác tên đăng nhập.')
    else:
        password = generate()
    if len(password) > server.PASSWORD_MAX:
        sys.exit('Mật khẩu quá dài.')

    account['passwordSalt'], account['passwordHash'] = server.password_hash(password)
    atomic_write(db_file, db)
    assert server.verify_password(password, json.loads(db_file.read_text(encoding='utf-8'))['accounts'][uid])

    sessions_file = Path(args.data_dir) / 'sessions.json'
    signed_out = 0
    if sessions_file.exists():
        sessions = json.loads(sessions_file.read_text(encoding='utf-8'))
        kept = {k: v for k, v in sessions.items() if v.get('uid') != uid}
        signed_out = len(sessions) - len(kept)
        atomic_write(sessions_file, kept)

    print(f'Đã đổi mật khẩu cho "{account.get("username")}" trong {db_file} (đăng xuất {signed_out} phiên cũ).')
    if not args.prompt:
        print(f'Mật khẩu mới: {password}')
        print('Hãy lưu vào trình quản lý mật khẩu; mật khẩu này không được lưu ở đâu khác.')


if __name__ == '__main__':
    main()
