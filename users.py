"""People who use this server and the devices they signed in with.

Signing in never involves passwords:
- The owner adds a person and sends them an invite link. Opening it signs that browser in.
- A TV or the Chrome extension shows a short code (and QR code); a signed-in phone approves it.
Only hashes of device tokens and invite codes are stored.
"""
import hashlib
import json
import os
import secrets
import threading
import time

INVITE_DAYS = 7
PAIRING_SECONDS = 600
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # no 0/O or 1/I: easy to read off a TV


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Users:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.pairings = {}      # code -> {secret, kind, expires, device token once approved}
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        for key in ('users', 'devices', 'invites'):
            self.data.setdefault(key, {})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(self.data, handle, indent=1)
        os.replace(temporary, self.path)

    # People

    def create(self, name, admin=False, user_id=None):
        name = ' '.join(str(name or '').split())[:40]
        if not name:
            raise ValueError('Enter a name.')
        with self.lock:
            user_id = user_id or secrets.token_hex(6)
            self.data['users'][user_id] = {'name': name, 'admin': bool(admin), 'created': int(time.time())}
            self.save()
            return user_id

    def get(self, user_id):
        with self.lock:
            user = self.data['users'].get(user_id)
            return dict(user, id=user_id) if user else None

    def all(self):
        with self.lock:
            devices = {}
            for device in self.data['devices'].values():
                devices[device['user']] = devices.get(device['user'], 0) + 1
            return [dict(user, id=user_id, devices=devices.get(user_id, 0))
                    for user_id, user in sorted(self.data['users'].items(), key=lambda item: item[1]['created'])]

    def remove(self, user_id):
        with self.lock:
            if self.data['users'].get(user_id, {}).get('admin'):
                raise ValueError('The owner cannot be removed.')
            self.data['users'].pop(user_id, None)
            for table in ('devices', 'invites'):
                self.data[table] = {key: value for key, value in self.data[table].items() if value['user'] != user_id}
            self.save()

    def owner_signed_in(self):
        owner = self.owner()
        with self.lock:
            return any(device['user'] == owner for device in self.data['devices'].values())

    def owner(self):
        with self.lock:
            for user_id, user in self.data['users'].items():
                if user.get('admin'):
                    return user_id
        return None

    # Invites

    def invite(self, user_id, seconds=INVITE_DAYS * 86400):
        """A code (and link) that signs a device in as this person. Reusable for a week (phone, laptop...).
        Short enough to type, so it also works when the link opened in a chat app's own browser."""
        with self.lock:
            if user_id not in self.data['users']:
                raise ValueError('Unknown person.')
            code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(10))
            now = time.time()
            self.data['invites'] = {key: value for key, value in self.data['invites'].items() if value['expires'] > now}
            self.data['invites'][digest(code)] = {'user': user_id, 'expires': int(now + seconds)}
            self.save()
            return code

    def redeem(self, code, device_name):
        with self.lock:
            invite = self.data['invites'].get(digest(clean_code(code, 10)))
            if not invite or invite['expires'] < time.time() or invite['user'] not in self.data['users']:
                return None
            return self.add_device(invite['user'], device_name)

    # Devices

    def add_device(self, user_id, name):
        with self.lock:
            token = secrets.token_urlsafe(32)
            self.data['devices'][digest(token)] = {'user': user_id, 'name': str(name or 'Device')[:60],
                                                   'created': int(time.time())}
            self.save()
            return token

    def device_user(self, token):
        if not token:
            return None
        with self.lock:
            device = self.data['devices'].get(digest(token))
            return self.get(device['user']) if device else None

    def sign_out(self, token):
        with self.lock:
            if self.data['devices'].pop(digest(token or ''), None):
                self.save()

    # Pairing a TV or the extension from a signed-in phone

    def start_pairing(self, kind):
        with self.lock:
            now = time.time()
            self.pairings = {code: p for code, p in self.pairings.items() if p['expires'] > now}
            if len(self.pairings) > 200:
                raise ValueError('Too many sign-in attempts. Try again in a few minutes.')
            code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            secret = secrets.token_urlsafe(24)
            self.pairings[code] = {'secret': secret, 'kind': str(kind)[:30], 'expires': now + PAIRING_SECONDS}
            return code, secret

    def pairing(self, code):
        code = clean_code(code)
        with self.lock:
            pairing = self.pairings.get(code)
            if pairing and pairing['expires'] > time.time():
                return dict(pairing, code=code)
        return None

    def approve(self, code, user_id):
        code = clean_code(code)
        with self.lock:
            pairing = self.pairings.get(code)
            if not pairing or pairing['expires'] < time.time():
                raise ValueError('That code has expired. Show a new one on the TV.')
            if pairing.get('token'):
                raise ValueError('That code was already used.')
            pairing['token'] = self.add_device(user_id, pairing['kind'])

    def collect(self, secret):
        """The waiting device asks whether it was approved; hands the token over exactly once."""
        with self.lock:
            for code, pairing in list(self.pairings.items()):
                if secrets.compare_digest(pairing['secret'], str(secret or '')):
                    if pairing.get('token'):
                        del self.pairings[code]
                        return pairing['token']
                    return None
        raise ValueError('This sign-in code expired. A new one will be shown.')


def clean_code(code, length=8):
    return ''.join(ch for ch in str(code or '').upper() if ch in CODE_ALPHABET)[:length]


def show_code(code):
    """ABCDEFGH -> ABCD-EFGH: easier to read off a screen."""
    half = (len(code) + 1) // 2
    return code[:half] + '-' + code[half:]
