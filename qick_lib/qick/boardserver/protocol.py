"""Framing and message conventions of the QICK board server, shared by both ends.

Board side: Python 3.8 or newer, standard library only in this module (numpy is
imported inside the array helpers, so headers can be framed without it).

A frame on the TCP stream is

    4 bytes   length of the header, unsigned little-endian
    4 bytes   length of the body, unsigned little-endian
    header    UTF-8 JSON object, at most MAX_HEADER bytes
    body      raw bytes, at most MAX_BODY bytes

Arrays travel in the body, laid end to end, and are described in the header's
"arrays" list as {"name", "dtype", "shape", "offset", "nbytes"}; dtype strings are
numpy's little-endian forms ("<i4", "<f8", "|u1" ...). See
slab/notes/board-server-protocol.md for the message types.
"""
import hashlib
import hmac
import json
import os
import socket
import struct

PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOLS = [1]

MAX_HEADER = 1 << 20     # 1 MiB
MAX_BODY = 64 << 20      # 64 MiB

_PREFIX = struct.Struct('<II')

# dtype kinds allowed in a body: bool, signed and unsigned int, float, complex
_ALLOWED_KINDS = 'biufc'

# Stable machine-readable codes for every refusal (`error` messages) and for job
# failures (`done.error.code`). Listed in slab/notes/board-server-protocol.md.
ERROR_CODES = {
    'bad_frame': 'frame could not be parsed (closes the connection)',
    'frame_too_large': 'header or body above the size cap (closes the connection)',
    'not_authenticated': 'request before a successful auth (closes the connection)',
    'auth_failed': 'HMAC did not match a token for the requested role (closes the connection)',
    'unsupported_protocol': 'hello asked for a protocol version this server does not speak',
    'unknown_type': 'message type not known to this server',
    'bad_request': 'a field is missing or invalid (see "field")',
    'forbidden': 'the connection role does not allow this request',
    'no_image': 'no firmware image is loaded',
    'unknown_job': 'no such job (or it has been freed)',
    'not_owner': 'only the job owner may do this',
    'unknown_program': 'prog_ref not in the program cache (send the program inline)',
    'program_format': 'the program dictionary could not be loaded (see "field")',
    'limit_exceeded': 'a board limit was exceeded (see "limit")',
    'seq_freed': 'fetch asked for chunks that were already freed',
    'unsupported': 'a feature that is reserved but not implemented in this version',
    'unknown_image': 'no image with that name or hash in the store',
    'hash_mismatch': 'uploaded file does not have the announced SHA-256',
    'name_exists': 'an image with that name and different content exists',
    'upload_unknown': 'no upload in progress with that id',
    'job_running': 'refused while a job is running (load_image, even with force)',
    'jobs_queued': 'refused while jobs are queued (load_image without force)',
    'image_mismatch': 'the job names an image other than the loaded one',
    'image_load_failed': 'constructing QickSoc from the image raised',
    'board_loading': 'an image load is in progress',
    'internal': 'unexpected server error (traceback in "traceback")',
    'no_rf_board': 'the loaded image (or the channel named) has no QICK box RF card of the kind needed',
    'rf_safe_failed': 'load_image refused before reprogramming: the readout amplifiers could not be confirmed off '
                      '("record"); nothing was loaded (skip_rf_safe overrides)',
    'adc_flags': 'rf_set applied, but an ADC is over range or over voltage at idle ("record"; on over-voltage '
                 'the card amplifier was switched off)',
    'rf_failed': 'an rf_set step raised; later steps were not applied ("record")',
    'rf_limit': 'an rf_set setting is outside the loaded image\'s limits table ("output", "limit", "value")',
    # only in done.error.code:
    'job_exception': 'a Python exception in the job (traceback in "traceback")',
    'tproc_queue_overflow': 'the tProc lost a push into a full dispatcher queue (see "queues")',
    'image_changed': 'the image changed after the job was submitted and the job names no image (run.image)',
    'adc_interrupt': 'an ADC interrupt (over range / over voltage) was raised when the round was prepared '
                     '(RFQickSoc216V1.prepare_round)',
}

AUTH_CONTEXT = b'qick-boardserver-auth/1'


class ProtocolError(Exception):
    """A frame that breaks the framing rules. `code` is one of ERROR_CODES."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RequestError(Exception):
    """A refused request: becomes an `error` reply with this code and text."""
    def __init__(self, code, message, **fields):
        super().__init__(message)
        self.code = code
        self.fields = fields


# --- framing ---------------------------------------------------------------

def encode_header(header):
    data = json.dumps(header, separators=(',', ':'), allow_nan=True).encode('utf-8')
    if len(data) > MAX_HEADER:
        raise ProtocolError('frame_too_large', 'header of %d bytes exceeds %d' % (len(data), MAX_HEADER))
    return data


def encode_frame(header, body=b''):
    """Return the frame as a list of buffers (the body parts are not copied)."""
    parts = body if isinstance(body, (list, tuple)) else [body]
    nbody = sum(memoryview(p).nbytes for p in parts)
    if nbody > MAX_BODY:
        raise ProtocolError('frame_too_large', 'body of %d bytes exceeds %d' % (nbody, MAX_BODY))
    h = encode_header(header)
    return [_PREFIX.pack(len(h), nbody), h] + [p for p in parts if memoryview(p).nbytes]


POLL_INTERVAL = 0.5   # socket timeout used for all reads and writes; loops retry on it


def prepare_socket(sock):
    """Give the socket the fixed poll timeout that send_frame/recv_frame expect."""
    sock.settimeout(POLL_INTERVAL)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


def _send_all(sock, buf, stall_timeout, should_stop):
    view = memoryview(buf).cast('B')
    stalled = 0.0
    while len(view):
        if should_stop is not None and should_stop():
            raise ConnectionError('connection is closing')
        try:
            k = sock.send(view)
        except socket.timeout:
            stalled += POLL_INTERVAL
            if stall_timeout is not None and stalled >= stall_timeout:
                raise ConnectionError('peer did not read for %.0f s' % (stalled,))
            continue
        if k == 0:
            raise ConnectionError('connection closed while sending')
        view = view[k:]
        stalled = 0.0


def send_frame(sock, header, body=b'', stall_timeout=None, should_stop=None):
    """Send one frame. With stall_timeout, give up when the peer reads nothing for
    that long. The socket must have been through prepare_socket()."""
    for buf in encode_frame(header, body):
        _send_all(sock, buf, stall_timeout, should_stop)


def _recv_exact(sock, n, idle_timeout, frame_timeout, should_stop, started):
    """Read exactly n bytes. Raises ConnectionError on EOF or on a stalled peer."""
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    waited = 0.0
    while got < n:
        if should_stop is not None and should_stop():
            raise ConnectionError('connection is closing')
        try:
            k = sock.recv_into(view[got:], n - got)
        except socket.timeout:
            waited += POLL_INTERVAL
            limit = frame_timeout if (started or got) else idle_timeout
            if limit is not None and waited >= limit:
                if started or got:
                    raise ConnectionError('peer stalled for %.0f s in the middle of a frame' % (waited,))
                raise socket.timeout('no frame within %.0f s' % (waited,))
            continue
        if k == 0:
            if got or started:
                raise ConnectionError('connection closed after %d of %d bytes' % (got, n))
            raise EOFError('connection closed')
        got += k
        waited = 0.0
    return bytes(buf)


def recv_frame(sock, idle_timeout=None, frame_timeout=60.0, max_header=MAX_HEADER, max_body=MAX_BODY,
               should_stop=None):
    """Read one frame; return (header dict, body bytes).

    idle_timeout applies while waiting for a frame to begin (None: wait forever);
    once a frame has started, the peer must keep sending within frame_timeout, so a
    client that vanishes mid-frame raises ConnectionError instead of hanging. A clean
    close between frames raises EOFError. A header or body above the cap raises
    ProtocolError('frame_too_large') before the oversize part is read.
    """
    prefix = _recv_exact(sock, _PREFIX.size, idle_timeout, frame_timeout, should_stop, False)
    hlen, blen = _PREFIX.unpack(prefix)
    if hlen > max_header:
        raise ProtocolError('frame_too_large', 'header of %d bytes exceeds %d' % (hlen, max_header))
    if blen > max_body:
        raise ProtocolError('frame_too_large', 'body of %d bytes exceeds %d' % (blen, max_body))
    if hlen == 0:
        raise ProtocolError('bad_frame', 'empty header')
    hdata = _recv_exact(sock, hlen, frame_timeout, frame_timeout, should_stop, True)
    body = _recv_exact(sock, blen, frame_timeout, frame_timeout, should_stop, True) if blen else b''
    try:
        header = json.loads(hdata.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        raise ProtocolError('bad_frame', 'header is not UTF-8 JSON: %s' % (e,))
    if not isinstance(header, dict) or not isinstance(header.get('type'), str):
        raise ProtocolError('bad_frame', 'header must be a JSON object with a string "type"')
    return header, body


# --- arrays in the body ----------------------------------------------------

def pack_arrays(named):
    """named: list of (name, ndarray). Returns (descriptors, body parts)."""
    import numpy as np
    descs = []
    parts = []
    offset = 0
    for name, arr in named:
        a = np.asarray(arr)
        if a.dtype.kind not in _ALLOWED_KINDS:
            raise ValueError('array %r has dtype %s, not allowed on the wire' % (name, a.dtype))
        if a.dtype.byteorder == '>' or (a.dtype.byteorder == '=' and not _little()):
            a = a.astype(a.dtype.newbyteorder('<'))
        a = np.ascontiguousarray(a)
        nbytes = a.nbytes
        descs.append({'name': name, 'dtype': a.dtype.str, 'shape': list(a.shape),
                      'offset': offset, 'nbytes': nbytes})
        parts.append(memoryview(a).cast('B') if nbytes else b'')
        offset += nbytes
    return descs, parts


def unpack_arrays(descs, body):
    """Inverse of pack_arrays. Returns an ordered dict name -> ndarray (writable copies)."""
    import numpy as np
    out = {}
    if not isinstance(descs, list):
        raise RequestError('bad_request', '"arrays" must be a list', field='arrays')
    for d in descs:
        try:
            name, dtype, shape = d['name'], np.dtype(d['dtype']), tuple(int(x) for x in d['shape'])
            offset, nbytes = int(d['offset']), int(d['nbytes'])
        except (KeyError, TypeError, ValueError) as e:
            raise RequestError('bad_request', 'bad array descriptor %r: %s' % (d, e), field='arrays')
        if dtype.kind not in _ALLOWED_KINDS or dtype.byteorder == '>':
            raise RequestError('bad_request', 'array %r: dtype %s not allowed' % (name, dtype), field='arrays')
        count = 1
        for s in shape:
            if s < 0:
                raise RequestError('bad_request', 'array %r: negative shape' % (name,), field='arrays')
            count *= s
        if count * dtype.itemsize != nbytes or offset < 0 or offset + nbytes > len(body):
            raise RequestError('bad_request', 'array %r: shape/nbytes/offset inconsistent with body' % (name,),
                               field='arrays')
        out[name] = np.frombuffer(body, dtype=dtype, count=count, offset=offset).reshape(shape).copy()
    return out


def _little():
    import sys
    return sys.byteorder == 'little'


# --- nested structures with arrays (program dictionaries) ------------------

ARRAY_REF = '$array'


def pack_tree(obj):
    """Turn a nested structure (dump_prog output) into JSON-safe data plus arrays.

    numpy arrays become {"$array": "a<n>"} and are returned in the array list;
    numpy scalars become Python numbers; objects with to_dict() (Waveform) become
    dicts; tuples become lists; dict keys become strings.
    Returns (tree, [(name, ndarray), ...]).
    """
    import numpy as np
    arrays = []

    def walk(o):
        if isinstance(o, np.ndarray):
            name = 'a%d' % len(arrays)
            arrays.append((name, o))
            return {ARRAY_REF: name}
        if isinstance(o, dict):
            return {str(k): walk(v) for k, v in o.items()}
        if hasattr(o, 'to_dict') and not isinstance(o, (str, bytes)):
            return walk(o.to_dict())
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.bool_):
            return bool(o)
        if o is None or isinstance(o, (bool, int, float, str)):
            return o
        raise TypeError('cannot put %s on the wire' % (type(o).__name__,))
    return walk(obj), arrays


def unpack_tree(tree, arrays):
    """Inverse of pack_tree: replace {"$array": name} by the named array."""
    def walk(o):
        if isinstance(o, dict):
            if len(o) == 1 and ARRAY_REF in o:
                name = o[ARRAY_REF]
                if name not in arrays:
                    raise RequestError('program_format', 'array %r referenced but not in body' % (name,),
                                       field='arrays')
                return arrays[name]
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o
    return walk(tree)


def tree_hash(tree, named_arrays):
    """Content hash of a packed tree and its arrays (same on both ends)."""
    import numpy as np
    h = hashlib.sha256()
    h.update(json.dumps(tree, sort_keys=True, separators=(',', ':')).encode('utf-8'))
    for name, arr in named_arrays:
        a = np.ascontiguousarray(arr)
        if a.dtype.byteorder == '>' or (a.dtype.byteorder == '=' and not _little()):
            a = a.astype(a.dtype.newbyteorder('<'))
        h.update(('%s|%s|%s|' % (name, a.dtype.str, list(a.shape))).encode('ascii'))
        h.update(memoryview(a).cast('B') if a.nbytes else b'')
    return h.hexdigest()


# --- authentication --------------------------------------------------------

def new_nonce():
    return os.urandom(32).hex()


def auth_mac(token, nonce, role, label):
    """HMAC-SHA256 that proves knowledge of the token without sending it.

    The MAC covers the server's nonce, the role asked for and the client label,
    so none of them can be swapped by a relay.
    """
    if isinstance(token, str):
        token = token.encode('utf-8')
    msg = b'\0'.join([AUTH_CONTEXT, nonce.encode('ascii'), role.encode('utf-8'), label.encode('utf-8')])
    return hmac.new(token, msg, hashlib.sha256).hexdigest()


def read_token_file(path):
    """Read tokens from a file.

    Each non-empty, non-comment line is either a bare token (role "run") or
    "<role> <token>" with role "run" or "read". Returns a list of (role, token bytes).
    """
    tokens = []
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            fields = line.split()
            if len(fields) == 1:
                tokens.append(('run', fields[0].encode('utf-8')))
            elif len(fields) == 2 and fields[0] in ('run', 'read'):
                tokens.append((fields[0], fields[1].encode('utf-8')))
            else:
                raise ValueError('token file %s: lines must be "<token>" or "<run|read> <token>"' % (path,))
    if not tokens:
        raise ValueError('token file %s has no tokens' % (path,))
    return tokens


def connect(host, port, timeout=10.0):
    sock = socket.create_connection((host, port), timeout=timeout)
    prepare_socket(sock)
    return sock
