"""Ed25519 signature verification on the standard library alone (RFC 8032, 5.1.7).

Verification only. The package depends on nothing outside the standard library, so that an
analysis environment can check a certified dataset without installing a cryptography
library first; a certificate carries a single signature, and checking it here takes a few
milliseconds. There is deliberately no second, faster path through an installed
`cryptography`: libraries differ in the edge cases they accept (OpenSSL takes a public key of
small order, libsodium - which the platform signs with - refuses it), and one implementation
gives one answer everywhere.

Nothing here handles a secret, which is why it need not run in constant time. Do not turn it
into a signer.

The arithmetic follows the reference code of RFC 8032, section 6. Where libsodium is stricter
than the RFC - it refuses public keys and R values of small order, and S >= L - this does the
same, so that a signature is valid here exactly when it is valid for the platform.
"""

from __future__ import annotations

import hashlib

__all__ = ["verify"]

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

# Points in extended homogeneous coordinates (X, Y, Z, T) with x = X/Z, y = Y/Z, x*y = T/Z.
_NEUTRAL = (0, 1, 1, 0)


def _add(a: tuple, b: tuple) -> tuple:
    """Point addition (RFC 8032, 5.1.4). Complete: it also doubles."""
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    pa = (y1 - x1) * (y2 - x2) % _P
    pb = (y1 + x1) * (y2 + x2) % _P
    pc = 2 * t1 * t2 * _D % _P
    pd = 2 * z1 * z2 % _P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar: int, point: tuple) -> tuple:
    result = _NEUTRAL
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same(a: tuple, b: tuple) -> bool:
    """Equal as points, whatever their Z (projective coordinates are not unique)."""
    x1, y1, z1, _ = a
    x2, y2, z2, _ = b
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _decode(encoded: bytes) -> tuple | None:
    """The point a 32-byte encoding stands for (RFC 8032, 5.1.3), or None.

    Refuses what the RFC refuses: y >= p (a second spelling of the same point), an x
    that does not exist, and "negative zero".
    """
    if len(encoded) != 32:
        return None
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        if sign:
            return None
        x = 0
    else:
        x = pow(x2, (_P + 3) // 8, _P)
        if (x * x - x2) % _P:
            x = x * _SQRT_M1 % _P
        if (x * x - x2) % _P:
            return None
        if (x & 1) != sign:
            x = _P - x
    return (x, y, 1, x * y % _P)


def _small_order(point: tuple) -> bool:
    """Whether 8 * point is the neutral element.

    Such a point as public key would let anyone sign; as R it makes signatures malleable.
    libsodium refuses both, and so must we, or the two would disagree about a certificate.
    """
    for _ in range(3):
        point = _add(point, point)
    return _same(point, _NEUTRAL)


_BASE = _decode((4 * pow(5, _P - 2, _P) % _P).to_bytes(32, "little"))


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` is a valid Ed25519 signature of `message` under `public_key`.

    All three as raw bytes: a 32-byte key, a 64-byte signature. Never raises for bad
    input - a signature that cannot even be read is simply not valid.
    """
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _decode(public_key)
    r = _decode(signature[:32])
    if a is None or r is None or _small_order(a) or _small_order(r):
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _L
    # [S]B = R + [k]A. Without the cofactor, as libsodium checks it: the two forms
    # disagree only on keys of mixed order, which no honest signer has.
    return _same(_multiply(s, _BASE), _add(r, _multiply(k, a)))
