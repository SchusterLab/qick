"""Command line of the QICK board server.

    python -m qick.boardserver --token-file F --image-dir D --port P [--load NAME | --attach NAME]
    python -m qick.boardserver --fake --token-file F --image-dir D --port P
    python -m qick.boardserver import-image --image-dir D --name NAME --bit X.bit [--hwh X.hwh]
    python -m qick.boardserver new-token FILE [--role run|read]

The token is only ever read from a file (never a command-line argument or an
environment variable) and is never logged.
"""
import argparse
import json
import logging
import os
import secrets
import signal
import stat
import sys
import threading

from . import protocol as P

log = logging.getLogger('qick.boardserver')


def _serve(argv):
    ap = argparse.ArgumentParser(prog='python -m qick.boardserver', description='QICK board server')
    ap.add_argument('--config', help='JSON file with any of the settings below (command line wins)')
    ap.add_argument('--host', default=None, help='address to listen on (default 0.0.0.0)')
    ap.add_argument('--port', type=int, default=None, help='TCP port (default 8788; 0 picks a free one)')
    ap.add_argument('--token-file', default=None, help='file holding the token(s); required unless --insecure-fake')
    ap.add_argument('--image-dir', default=None, help='image store directory (created if missing)')
    ap.add_argument('--lock-file', default=None, help='board ownership lock (default /tmp/qick-boardserver.lock)')
    ap.add_argument('--max-queue-mb', type=float, default=None, help='bound on chunk memory held (default 256)')
    ap.add_argument('--allow-job-image-load', action='store_true', default=None,
                    help='let a job naming another image load it (default: refuse with image_mismatch)')
    ap.add_argument('--load', metavar='IMAGE', help='load this store image at start (programs the FPGA)')
    ap.add_argument('--attach', metavar='IMAGE',
                    help='use what the FPGA already runs, assuming it is this store image (no download)')
    ap.add_argument('--soc-kwargs', default=None, help='JSON object of extra QickSoc arguments (real backend)')
    ap.add_argument('--soc-class', default=None, choices=['auto', 'QickSoc', 'RFQickSoc216V1'],
                    help='soc class for the real backend (default auto: RFQickSoc216V1 when the .hwh has the '
                         'rfb_control block)')
    ap.add_argument('--no-rf-safe-on-load', action='store_true',
                    help='do NOT switch readout amplifiers off before a load and apply the RF safe state after it')
    ap.add_argument('--fake', action='store_true', help='fake board (no hardware); creates fake images if needed')
    ap.add_argument('--insecure-fake', action='store_true', help='with --fake only: accept any client, no token')
    ap.add_argument('--log-file', default=None)
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args(argv)

    cfg = {}
    if a.config:
        with open(a.config) as f:
            cfg.update(json.load(f))
    if 'token' in cfg:
        sys.exit('refusing to start: the config file contains "token"; put tokens in a token file (token_file)')
    for key in ('host', 'port', 'token_file', 'image_dir', 'lock_file', 'allow_job_image_load', 'soc_kwargs',
                'log_file', 'soc_class'):
        v = getattr(a, key)
        if v is not None:
            cfg[key] = v
    if a.no_rf_safe_on_load:
        cfg['rf_safe_on_load'] = False
    if a.max_queue_mb is not None:
        cfg['max_queue_bytes'] = int(a.max_queue_mb * (1 << 20))

    handlers = [logging.StreamHandler()]
    if cfg.get('log_file'):
        handlers.append(logging.FileHandler(cfg.pop('log_file')))
    cfg.pop('log_file', None)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, handlers=handlers,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    from .images import ImageStore
    from .server import BoardServer

    if a.insecure_fake and not a.fake:
        sys.exit('refusing to start: --insecure-fake is only allowed together with --fake')
    tokens = None
    if cfg.get('token_file'):
        path = os.path.expanduser(cfg['token_file'])
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning('token file %s is readable by group/others; chmod 600 it', path)
        tokens = P.read_token_file(path)
    elif not a.insecure_fake:
        sys.exit('refusing to start: no --token-file (only --fake --insecure-fake may run without one)')
    cfg['insecure_fake'] = bool(a.insecure_fake and a.fake)

    if not cfg.get('image_dir'):
        sys.exit('--image-dir is required')
    images = ImageStore(os.path.expanduser(cfg['image_dir']))

    if a.fake:
        from .backend import FakeBackend, fake_image_files, FAKE_VARIANTS
        backend = FakeBackend()
        names = [m['name'] for m in images.list()]
        for variant in FAKE_VARIANTS:
            if variant not in names:
                bit, hwh = fake_image_files(variant)
                images.add_bytes(variant, bit, hwh, added_by='--fake', note='built-in fake image')
        if not a.load and not a.attach:
            a.load = 'fake-a'
    else:
        from .backend import QickSocBackend
        kw = cfg.get('soc_kwargs')
        if isinstance(kw, str):
            kw = json.loads(kw)
        backend = QickSocBackend(soc_kwargs=kw, soc_class=cfg.get('soc_class') or 'auto')
    if cfg.get('rf_safe_on_load') is False:
        log.warning('RF safe procedure around image loads is OFF (--no-rf-safe-on-load)')

    server_cfg = {k: v for k, v in cfg.items() if k not in ('token_file', 'image_dir', 'soc_kwargs', 'soc_class')}
    srv = BoardServer(backend, images, server_cfg, tokens=tokens)
    srv.start()
    try:
        if a.load:
            srv.load_at_start(a.load)
        elif a.attach:
            srv.attach_at_start(a.attach)
        else:
            log.info('no image loaded; clients must load_image before running jobs')
    except Exception:
        log.exception('could not load the start image; the server runs without one')
    print('qick.boardserver ready on port %d' % srv.address[1], flush=True)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *x: stop.set())
    while not stop.wait(1.0):
        pass
    log.info('shutting down')
    srv.stop()


def _import_image(argv):
    ap = argparse.ArgumentParser(prog='python -m qick.boardserver import-image',
                                 description='copy a .bit/.hwh pair into the image store')
    ap.add_argument('--image-dir', required=True)
    ap.add_argument('--name', required=True)
    ap.add_argument('--bit', required=True)
    ap.add_argument('--hwh', default=None, help='default: the .bit path with .hwh')
    ap.add_argument('--note', default=None)
    ap.add_argument('--replace', action='store_true')
    ap.add_argument('--rf-safe', default=None, metavar='JSON',
                    help='RF safe state applied right after each load of this image: {"settings": [...], '
                         '"limits": {...}, "settle_s": s} (rf_set settings; limits as in the protocol note)')
    a = ap.parse_args(argv)
    from .images import ImageStore
    hwh = a.hwh or os.path.splitext(a.bit)[0] + '.hwh'
    store = ImageStore(a.image_dir)
    safe = None
    if a.rf_safe:
        from . import rf as RF
        with open(a.rf_safe) as f:
            doc = json.load(f)
        safe = {'settings': RF.check_items(doc.get('settings') or [], allow_empty=True),
                'limits': RF.check_limits_table(doc.get('limits')), 'stored_by': 'import-image'}
        if doc.get('settle_s') is not None:
            safe['settle_s'] = RF.check_settle(doc['settle_s'])
    meta = store.add_files(a.name, a.bit, hwh, added_by='import-image', replace=a.replace, note=a.note)
    if safe is not None:
        store.set_rf_safe(a.name, safe)
        meta['rf_safe'] = safe
    print(json.dumps(meta, indent=1))


def _new_token(argv):
    ap = argparse.ArgumentParser(prog='python -m qick.boardserver new-token',
                                 description='append a new random token to a token file (mode 600)')
    ap.add_argument('file')
    ap.add_argument('--role', choices=['run', 'read'], default='run')
    a = ap.parse_args(argv)
    tok = secrets.token_urlsafe(32)
    fd = os.open(a.file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write('%s %s\n' % (a.role, tok))
    os.chmod(a.file, 0o600)
    print('appended a %s token to %s (copy the file to the client over a trusted channel)' % (a.role, a.file))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == 'import-image':
        _import_image(argv[1:])
    elif argv and argv[0] == 'new-token':
        _new_token(argv[1:])
    else:
        _serve(argv)


if __name__ == '__main__':
    main()
