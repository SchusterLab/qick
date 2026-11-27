"""Store of firmware images on the board: named .bit + .hwh pairs, by content hash.

Layout of the image directory:

    <root>/<name>/image.bit
    <root>/<name>/image.hwh
    <root>/<name>/meta.json      name, hashes, sizes, when and by whom it was added
    <root>/<name>/soccfg.json    the configuration the image produced at its last load
    <root>/.uploads/<id>/        uploads in progress (removed on commit or abort)

An image is identified by `hash` = SHA-256 of (bit SHA-256 hex + hwh SHA-256 hex).
Standard library only.
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid

from .protocol import RequestError

NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
FILES = ('bit', 'hwh')


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def image_hash(bit_sha256, hwh_sha256):
    return hashlib.sha256((bit_sha256 + hwh_sha256).encode('ascii')).hexdigest()


def _write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


class ImageStore:
    def __init__(self, root, max_file_bytes=512 << 20):
        self.root = os.path.abspath(root)
        self.max_file_bytes = max_file_bytes
        os.makedirs(self.root, exist_ok=True)
        self.upload_root = os.path.join(self.root, '.uploads')
        if os.path.isdir(self.upload_root):
            # uploads do not survive a restart
            shutil.rmtree(self.upload_root, ignore_errors=True)
        os.makedirs(self.upload_root, exist_ok=True)
        self.lock = threading.RLock()
        self.uploads = {}

    # --- reading ---------------------------------------------------------

    def _dir(self, name):
        return os.path.join(self.root, name)

    def paths(self, meta):
        d = self._dir(meta['name'])
        return os.path.join(d, 'image.bit'), os.path.join(d, 'image.hwh')

    def _meta(self, name):
        try:
            with open(os.path.join(self._dir(name), 'meta.json')) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def list(self):
        out = []
        with self.lock:
            for name in sorted(os.listdir(self.root)):
                if name.startswith('.') or not os.path.isdir(self._dir(name)):
                    continue
                meta = self._meta(name)
                if meta is None:
                    continue
                cfg = self.cached_soccfg(name)
                meta = dict(meta)
                meta['has_soccfg'] = cfg is not None
                if cfg is not None:
                    meta['fw_timestamp'] = cfg.get('fw_timestamp')
                    meta['board'] = cfg.get('board')
                out.append(meta)
        return out

    def resolve(self, ref):
        """Find an image by exact name, or by hash (full, or a unique prefix of 8+ hex digits)."""
        if not isinstance(ref, str) or not ref:
            raise RequestError('bad_request', 'image must be a name or hash', field='image')
        images = self.list()
        for m in images:
            if m['name'] == ref:
                return m
        if len(ref) >= 8 and re.match(r'^[0-9a-f]+$', ref):
            hits = [m for m in images if m['hash'].startswith(ref)]
            if len(hits) >= 1 and len(set(m['hash'] for m in hits)) == 1:
                return hits[0]
            if hits:
                raise RequestError('bad_request', 'hash prefix %s is ambiguous' % ref, field='image')
        raise RequestError('unknown_image', 'no image named or hashed %r in the store' % ref, image=ref)

    def cached_soccfg(self, name):
        try:
            with open(os.path.join(self._dir(name), 'soccfg.json')) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save_soccfg(self, name, cfg):
        with self.lock:
            if os.path.isdir(self._dir(name)):
                _write_json(os.path.join(self._dir(name), 'soccfg.json'), cfg)

    # --- adding ----------------------------------------------------------

    def _install(self, name, src_dir, meta, replace):
        """Move a directory holding image.bit/image.hwh into place under `name`."""
        with self.lock:
            existing = self._meta(name)
            if existing is not None:
                if existing['hash'] == meta['hash']:
                    shutil.rmtree(src_dir, ignore_errors=True)
                    return existing
                if not replace:
                    shutil.rmtree(src_dir, ignore_errors=True)
                    raise RequestError('name_exists', 'image %r exists with hash %s' % (name, existing['hash']),
                                       existing_hash=existing['hash'])
            _write_json(os.path.join(src_dir, 'meta.json'), meta)
            if existing is not None:
                old = os.path.join(self.upload_root, 'replaced-' + uuid.uuid4().hex)
                os.replace(self._dir(name), old)
                shutil.rmtree(old, ignore_errors=True)
            os.replace(src_dir, self._dir(name))
            return meta

    def _check_name(self, name):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise RequestError('bad_request', 'image name must match %s' % NAME_RE.pattern, field='name')

    def add_files(self, name, bit_path, hwh_path, added_by, replace=False, note=None):
        """Copy a bit/hwh pair into the store (used by `python -m qick.boardserver import-image`)."""
        self._check_name(name)
        tmp = os.path.join(self.upload_root, 'import-' + uuid.uuid4().hex)
        os.makedirs(tmp)
        shutil.copyfile(bit_path, os.path.join(tmp, 'image.bit'))
        shutil.copyfile(hwh_path, os.path.join(tmp, 'image.hwh'))
        bsha = file_sha256(os.path.join(tmp, 'image.bit'))
        hsha = file_sha256(os.path.join(tmp, 'image.hwh'))
        meta = {'name': name, 'hash': image_hash(bsha, hsha), 'bit_sha256': bsha, 'hwh_sha256': hsha,
                'bit_size': os.path.getsize(os.path.join(tmp, 'image.bit')),
                'hwh_size': os.path.getsize(os.path.join(tmp, 'image.hwh')),
                'added': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'added_by': added_by, 'note': note}
        return self._install(name, tmp, meta, replace)

    def add_bytes(self, name, bit, hwh, added_by, replace=False, note=None):
        tmp = os.path.join(self.upload_root, 'bytes-' + uuid.uuid4().hex)
        os.makedirs(tmp)
        for fn, data in (('image.bit', bit), ('image.hwh', hwh)):
            with open(os.path.join(tmp, fn), 'wb') as f:
                f.write(data)
        try:
            return self.add_files(name, os.path.join(tmp, 'image.bit'), os.path.join(tmp, 'image.hwh'),
                                  added_by, replace=replace, note=note)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # --- chunked upload --------------------------------------------------

    def begin_upload(self, name, bit_sha256, hwh_sha256, bit_size, hwh_size, added_by, owner, replace=False,
                     note=None):
        self._check_name(name)
        for field, v in (('bit_sha256', bit_sha256), ('hwh_sha256', hwh_sha256)):
            if not isinstance(v, str) or not re.match(r'^[0-9a-f]{64}$', v):
                raise RequestError('bad_request', '%s must be 64 lowercase hex digits' % field, field=field)
        for field, v in (('bit_size', bit_size), ('hwh_size', hwh_size)):
            if not isinstance(v, int) or v <= 0 or v > self.max_file_bytes:
                raise RequestError('limit_exceeded' if isinstance(v, int) and v > 0 else 'bad_request',
                                   '%s must be 1..%d bytes' % (field, self.max_file_bytes), field=field,
                                   limit='max_file_bytes')
        upload = uuid.uuid4().hex
        d = os.path.join(self.upload_root, upload)
        os.makedirs(d)
        for f in FILES:
            open(os.path.join(d, 'image.' + f), 'wb').close()
        with self.lock:
            self.uploads[upload] = {'dir': d, 'name': name, 'owner': owner, 'replace': bool(replace),
                                    'note': note, 'added_by': added_by,
                                    'expect': {'bit': (bit_sha256, bit_size), 'hwh': (hwh_sha256, hwh_size)},
                                    'written': {'bit': 0, 'hwh': 0}}
        return upload

    def _upload(self, upload, owner):
        with self.lock:
            u = self.uploads.get(upload)
        if u is None or u['owner'] != owner:
            raise RequestError('upload_unknown', 'no upload %r on this connection' % (upload,))
        return u

    def write_upload(self, upload, owner, which, offset, data):
        u = self._upload(upload, owner)
        if which not in FILES:
            raise RequestError('bad_request', 'file must be "bit" or "hwh"', field='file')
        if not isinstance(offset, int) or offset != u['written'][which]:
            raise RequestError('bad_request', 'offset must be %d (chunks are sent in order)' % u['written'][which],
                               field='offset')
        size = u['expect'][which][1]
        if offset + len(data) > size:
            raise RequestError('limit_exceeded', 'chunk runs past the announced %s_size %d' % (which, size),
                               limit='%s_size' % which)
        with open(os.path.join(u['dir'], 'image.' + which), 'r+b') as f:
            f.seek(offset)
            f.write(data)
        u['written'][which] = offset + len(data)
        return u['written'][which]

    def commit_upload(self, upload, owner):
        u = self._upload(upload, owner)
        got = {}
        for which in FILES:
            sha, size = u['expect'][which]
            if u['written'][which] != size:
                raise RequestError('bad_request', '%s has %d of %d bytes' % (which, u['written'][which], size),
                                   field=which)
            got[which] = file_sha256(os.path.join(u['dir'], 'image.' + which))
        bad = [w for w in FILES if got[w] != u['expect'][w][0]]
        if bad:
            self.abort_upload(upload, owner)
            raise RequestError('hash_mismatch', 'SHA-256 of %s does not match what was announced' % ' and '.join(bad),
                               files=bad, expected={w: u['expect'][w][0] for w in bad},
                               received={w: got[w] for w in bad})
        meta = {'name': u['name'], 'hash': image_hash(got['bit'], got['hwh']),
                'bit_sha256': got['bit'], 'hwh_sha256': got['hwh'],
                'bit_size': u['expect']['bit'][1], 'hwh_size': u['expect']['hwh'][1],
                'added': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'added_by': u['added_by'], 'note': u['note']}
        with self.lock:
            self.uploads.pop(upload, None)
        return self._install(u['name'], u['dir'], meta, u['replace'])

    def abort_upload(self, upload, owner=None):
        with self.lock:
            u = self.uploads.get(upload)
            if u is None or (owner is not None and u['owner'] != owner):
                return False
            shutil.rmtree(u['dir'], ignore_errors=True)
            self.uploads.pop(upload)
        return True

    def abort_owner(self, owner):
        """Discard every upload a vanished connection left unfinished."""
        with self.lock:
            ids = [k for k, u in self.uploads.items() if u['owner'] == owner]
        for k in ids:
            self.abort_upload(k)
        return ids
