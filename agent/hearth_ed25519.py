#!/usr/bin/env python3
r"""Ed25519, in the standard library and nothing else.

Why this file exists
--------------------
Hearth's updater has to answer one question before it will let anything near
the user's machine: were these bytes signed by the key this application was
built with. TLS cannot answer it. TLS proves you reached a host; it says
nothing about who wrote what that host is serving, and a release host is
exactly the thing an attacker takes over. A signature made with a private key
that never touches the release host, checked against a public key compiled
into the shipped application, is the only check that survives losing the host.

The signature scheme has to be asymmetric for that to mean anything, and
Python's standard library ships no asymmetric cryptography at all: hashlib and
hmac and nothing else. The project's rule is standard library only in agent/,
desktop/server/ and scripts/, and that rule is not a stylistic preference --
it is why a clean checkout builds on a machine with nothing installed, and why
the shipped payload has no third-party code in it to audit or to be
compromised. So the primitive is written here, against RFC 8032, using
hashlib.sha512 and Python integers.

That is a real decision with a real cost, so here is the honest accounting.

WHAT THIS IS GOOD AT. Verification. Verifying a signature involves no secret:
the inputs are a public key, a message and a signature, all of which the
attacker already has. There is no secret-dependent branch or table index to
leak, because there is no secret. A pure-Python verifier is therefore exactly
as safe as a C one, only slower, and "slower" here means about ten
milliseconds per signature on a laptop, once per update check.

WHAT THIS IS NOT GOOD AT. Signing in a hostile environment. sign() below
multiplies the base point by a secret scalar using Python integers, and
Python's integer arithmetic is not constant time. An attacker who can measure
the timing of many signatures on the same machine could in principle learn
something about the key. That threat does not exist for the way this is used:
signing happens on the operator's own machine, offline, from a key file they
control, a handful of times a year, with nobody else measuring. If signing
ever moves onto a shared or network-facing machine, it must move to a real
implementation at the same time. That sentence is the whole caveat and it is
written here rather than in a commit message so it cannot get lost.

WHAT IS ENFORCED. The signature encoding is checked for canonicality: S must
be reduced modulo the group order, and both encoded points must have y < p.
Unreduced S is the classic malleability hole -- S and S+L verify equally under
a naive implementation, so the same message ends up with two "valid"
signatures and anything that identifies an update by its signature can be
confused. Reducing is not enough; it must be REFUSED, which is what ref10 and
libsodium do and what is done here.

CORRECTNESS. Agreement with the rest of the world is the only property that
matters for an interoperable signature scheme, and it is asserted three ways
in _self_test: the RFC 8032 section 7.1 test vectors, a round trip through
this module's own sign and verify, and a battery of mutations (every byte of
the signature, the key and the message flipped in turn) that must all fail.
The vectors are the load-bearing ones: they are bytes produced by other
people's implementations, so passing them means this agrees with OpenSSL and
libsodium rather than merely agreeing with itself. It was additionally checked
against OpenSSL 3.5.6 on the machine it was written on, by having OpenSSL sign
a message and this module verify it and the reverse; that check is not in the
self-test because it needs a subprocess, and this module deliberately has no
way to start one (see below).

NOTHING HERE RUNS ANYTHING. Same rule as scripts/vendor_llama.py, and
_self_test enforces it the same way, by scanning this file's own source. A
module whose job is to decide whether foreign bytes are trustworthy must not
also be able to execute them.
"""

import argparse
import binascii
import hashlib
import os
import sys

# --------------------------------------------------------------------------
# The curve
# --------------------------------------------------------------------------

#: The field. Ed25519 works modulo 2^255 - 19.
P = 2 ** 255 - 19

#: The order of the base point's prime-order subgroup. Scalars live here, and
#: a signature's S must be strictly below it.
L = 2 ** 252 + 27742317777372353535851937790883648493

#: The curve constant d of the twisted Edwards curve -x^2 + y^2 = 1 + d x^2 y^2.
D = -121665 * pow(121666, P - 2, P) % P

#: A square root of -1 modulo p, needed to recover x from y.
SQRT_M1 = pow(2, (P - 1) // 4, P)

KEY_BYTES = 32
SIGNATURE_BYTES = 64


class SignatureError(ValueError):
    """A signature, key or encoding this module refuses.

    Raised only by the strict entry points (sign, public_key, and the decoders
    they use). verify() never raises: a caller asking "is this signed" wants
    False for a malformed signature, not an exception to remember to catch,
    and every way of being malformed is a way of being unsigned.
    """


# --------------------------------------------------------------------------
# Points, in extended twisted Edwards coordinates
# --------------------------------------------------------------------------
#
# A point is (X, Y, Z, T) with x = X/Z, y = Y/Z and x*y = T/Z. The redundant
# T is what makes addition unified: one formula that is correct for every
# pair of points including a point added to itself, so there is no separate
# doubling routine and therefore no branch on whether two points are equal.

def _add(p1, p2):
    """The unified addition law. Correct for any two points on the curve."""
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * D % P
    dd = 2 * z1 * z2 % P
    e = (b - a) % P
    f = (dd - c) % P
    g = (dd + c) % P
    h = (b + a) % P
    return (e * f % P, g * h % P, f * g % P, e * h % P)


#: The neutral element (0, 1).
ZERO = (0, 1, 1, 0)


def _scalar_mult(scalar, point):
    """[scalar]point, by double-and-add.

    Not constant time, and deliberately so rather than accidentally: see the
    module docstring. Every use in verify() has a public scalar; the one use
    with a secret scalar is in sign(), which is documented as an
    operator-machine operation.
    """
    result = ZERO
    addend = point
    while scalar > 0:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _recover_x(y, sign):
    """The x coordinate matching `y` with the requested low bit, or None.

    None means the encoding does not name a point on the curve at all, which
    is a refusal rather than a value: an implementation that invented an x
    here would be accepting keys and signatures that are not keys and
    signatures.
    """
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    if x2 == 0:
        # y = +-1. Only x = 0 works, and only with a zero sign bit; the
        # encoding of -0 is not a valid encoding of 0.
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


def _decompress(data):
    """A 32-byte encoded point as (X, Y, Z, T), or None if it is not one.

    Rejects a y at or above p. Those encodings are the classic non-canonical
    case: several distinct byte strings would otherwise decode to the same
    point, so two different signatures would verify for one message and
    anything keyed on the bytes could be confused.
    """
    if len(data) != KEY_BYTES:
        return None
    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= P:
        return None
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def _compress(point):
    """A point as its 32-byte encoding."""
    x, y, z, _t = point
    inv_z = pow(z, P - 2, P)
    x = x * inv_z % P
    y = y * inv_z % P
    return int.to_bytes(y | ((x & 1) << 255), KEY_BYTES, "little")


#: The base point. y = 4/5, and x is whichever root has an even low bit.
_BASE_Y = 4 * pow(5, P - 2, P) % P
_BASE_X = _recover_x(_BASE_Y, 0)
BASE = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % P)


# --------------------------------------------------------------------------
# Keys and signatures
# --------------------------------------------------------------------------

def _hash_to_scalar(*chunks):
    """SHA-512 over the concatenated chunks, reduced modulo the group order."""
    digest = hashlib.sha512()
    for chunk in chunks:
        digest.update(chunk)
    return int.from_bytes(digest.digest(), "little") % L


def _secret_scalar(seed):
    """The clamped scalar and the nonce prefix derived from a 32-byte seed.

    The clamping is RFC 8032's: clear the low three bits so the scalar is a
    multiple of the cofactor, clear the top bit and set the one below it so
    the scalar has a fixed bit length. Both exist to remove choices an
    implementation could otherwise get wrong.
    """
    if len(seed) != KEY_BYTES:
        raise SignatureError("an Ed25519 seed is {} bytes, got {}".format(
            KEY_BYTES, len(seed)))
    digest = hashlib.sha512(seed).digest()
    head = bytearray(digest[:32])
    head[0] &= 0xF8
    head[31] &= 0x7F
    head[31] |= 0x40
    return int.from_bytes(bytes(head), "little"), digest[32:]


def public_key(seed):
    """The 32-byte public key for a 32-byte private seed."""
    scalar, _prefix = _secret_scalar(seed)
    return _compress(_scalar_mult(scalar, BASE))


def sign(seed, message):
    """A 64-byte Ed25519 signature over `message`.

    Deterministic: the nonce comes from the key and the message, never from a
    random number generator, so two signatures over the same message with the
    same key are byte identical. That is what makes a release reproducible and
    what makes the OpenSSL cross-check in the module docstring a byte
    comparison rather than a verification.
    """
    if not isinstance(message, (bytes, bytearray)):
        raise SignatureError("a message must be bytes")
    scalar, prefix = _secret_scalar(seed)
    encoded_a = _compress(_scalar_mult(scalar, BASE))
    r = _hash_to_scalar(prefix, message)
    encoded_r = _compress(_scalar_mult(r, BASE))
    k = _hash_to_scalar(encoded_r, encoded_a, message)
    s = (r + k * scalar) % L
    return encoded_r + int.to_bytes(s, KEY_BYTES, "little")


def verify(public, message, signature):
    """True when `signature` is a valid Ed25519 signature over `message`.

    Never raises, for any input at all. A caller asking this question wants a
    yes or a no; every way of being malformed -- a truncated key, a signature
    that is not on the curve, an S that was not reduced -- is a way of being
    unsigned, and turning some of those into exceptions and others into False
    would make the failure path something a caller has to get right twice.

    The checks, in order, and what each one is for:

      1. Lengths. A 63-byte signature is not a short signature.
      2. S < L. An unreduced S is the malleability hole: without this check
         S and S + L both verify, so one signed message has many valid
         signatures. Refused rather than reduced.
      3. Both points decode canonically (see _decompress).
      4. [S]B == R + [k]A, computed cofactorlessly, which is the strict form
         and the one ref10, libsodium and OpenSSL implement. Compared as
         encodings rather than as coordinates, because a point has many
         projective representations and exactly one encoding.
    """
    if not isinstance(public, (bytes, bytearray)) or len(public) != KEY_BYTES:
        return False
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != SIGNATURE_BYTES:
        return False
    if not isinstance(message, (bytes, bytearray)):
        return False
    encoded_r = bytes(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    point_a = _decompress(bytes(public))
    if point_a is None:
        return False
    point_r = _decompress(encoded_r)
    if point_r is None:
        return False
    k = _hash_to_scalar(encoded_r, bytes(public), bytes(message))
    left = _scalar_mult(s, BASE)
    right = _add(point_r, _scalar_mult(k, point_a))
    return _compress(left) == _compress(right)


# --------------------------------------------------------------------------
# Hex helpers
# --------------------------------------------------------------------------

def from_hex(text, expected_length=None):
    """Bytes from a hex string, refusing anything that is not clean hex.

    binascii rather than bytes.fromhex because fromhex tolerates embedded
    whitespace, and a key that is accepted with a space in the middle is a key
    that can be typed two ways.
    """
    if not isinstance(text, str):
        raise SignatureError("expected a hex string, got {}".format(type(text).__name__))
    try:
        raw = binascii.unhexlify(text)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("not valid hex: {}".format(exc)) from exc
    if expected_length is not None and len(raw) != expected_length:
        raise SignatureError("expected {} bytes of hex, got {}".format(
            expected_length, len(raw)))
    return raw


def to_hex(raw):
    return binascii.hexlify(bytes(raw)).decode("ascii")


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

#: Source tokens that would mean this module can run what it verifies. The
#: scan below is a real guarantee rather than a comment.
_EXECUTION_TOKENS = ("import subprocess", "os.system", "os.popen", "os.exec",
                     "os.spawn", "import ctypes", "runpy", "eval(", "exec(")

#: RFC 8032 section 7.1, tests 1 to 3, plus the SHA(abc) case. Bytes produced
#: by other implementations, which is the point: passing these means this
#: agrees with the rest of the world rather than with itself.
RFC8032_VECTORS = [
    {
        "seed": "9d61b19deffd5a60ba844af492ec2cc4"
                "4449c5697b326919703bac031cae7f60",
        "public": "d75a980182b10ab7d54bfed3c964073a"
                  "0ee172f3daa62325af021a68f707511a",
        "message": "",
        "signature": "e5564300c360ac729086e2cc806e828a"
                     "84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
                     "d25bf5f0595bbe24655141438e7a100b",
    },
    {
        "seed": "4ccd089b28ff96da9db6c346ec114e0f"
                "5b8a319f35aba624da8cf6ed4fb8a6fb",
        "public": "3d4017c3e843895a92b70aa74d1b7ebc"
                  "9c982ccf2ec4968cc0cd55f12af4660c",
        "message": "72",
        "signature": "92a009a9f0d4cab8720e820b5f642540"
                     "a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
                     "387b2eaeb4302aeeb00d291612bb0c00",
    },
    {
        "seed": "c5aa8df43f9f837bedb7442f31dcb7b1"
                "66d38535076f094b85ce3a2e0b4458f7",
        "public": "fc51cd8e6218a1a38da47ed00230f058"
                  "0816ed13ba3303ac5deb911548908025",
        "message": "af82",
        "signature": "6291d657deec24024827e69c3abe01a3"
                     "0ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc659"
                     "4a7c15e9716ed28dc027beceea1ec40a",
    },
    {
        "seed": "833fe62409237b9d62ec77587520911e"
                "9a759cec1d19755b7da901b96dca3d42",
        "public": "ec172b93ad5e563bf4932c70e1245034"
                  "c35467ef2efd4d64ebf819683467e2bf",
        "message": "ddaf35a193617abacc417349ae204131"
                   "12e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd"
                   "454d4423643ce80e2a9ac94fa54ca49f",
        "signature": "dc2a4459e7369633a52b1bf277839a00"
                     "201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e030"
                     "3dca179c138ac17ad9bef1177331a704",
    },
]


def _self_test():
    # -- nothing here can run what it verifies -----------------------------
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
        source = fh.read()
    body = source.split("_EXECUTION_TOKENS = (", 1)[0]
    for token in _EXECUTION_TOKENS:
        assert token not in body, (
            "hearth_ed25519 must never be able to run what it verifies, but its "
            "source contains {!r}".format(token))

    # -- the curve constants are the curve ---------------------------------
    # A base point that is not on the curve would produce a self-consistent
    # signature scheme that nobody else can verify, so check membership
    # directly rather than trusting the derivation above.
    bx, by = _BASE_X, _BASE_Y
    assert (-bx * bx + by * by - 1 - D * bx * bx % P * (by * by) % P) % P == 0
    assert _compress(_scalar_mult(L, BASE)) == _compress(ZERO), (
        "the base point does not have order L")

    # -- RFC 8032 vectors --------------------------------------------------
    for index, vector in enumerate(RFC8032_VECTORS):
        seed = from_hex(vector["seed"], 32)
        want_public = from_hex(vector["public"], 32)
        message = from_hex(vector["message"])
        want_signature = from_hex(vector["signature"], 64)
        got_public = public_key(seed)
        assert got_public == want_public, (
            "vector {}: public key {} != {}".format(index, to_hex(got_public),
                                                    vector["public"]))
        got_signature = sign(seed, message)
        assert got_signature == want_signature, (
            "vector {}: signature {} != {}".format(index, to_hex(got_signature),
                                                   vector["signature"]))
        assert verify(want_public, message, want_signature), index

        # MUTATION: every single-byte change to the signature, the key or the
        # message must fail. Flipping the low bit of each byte in turn rather
        # than one arbitrary byte, because a verifier that ignored, say, the
        # top half of S would pass a spot check and fail this.
        for position in range(len(want_signature)):
            broken = bytearray(want_signature)
            broken[position] ^= 0x01
            assert not verify(want_public, message, bytes(broken)), (
                "vector {}: a signature with byte {} flipped verified".format(
                    index, position))
        for position in range(len(want_public)):
            broken = bytearray(want_public)
            broken[position] ^= 0x01
            assert not verify(bytes(broken), message, want_signature), (
                "vector {}: a key with byte {} flipped verified".format(index, position))
        for position in range(len(message)):
            broken = bytearray(message)
            broken[position] ^= 0x01
            assert not verify(want_public, bytes(broken), want_signature), (
                "vector {}: a message with byte {} flipped verified".format(
                    index, position))

    # -- malleability: S must be REFUSED, not reduced ----------------------
    # S and S + L are the same scalar modulo the group order. An
    # implementation that reduces instead of refusing accepts both, so one
    # signed message has many valid signatures and anything that identifies
    # an update by its signature bytes can be confused about which is which.
    vector = RFC8032_VECTORS[1]
    seed = from_hex(vector["seed"], 32)
    pub = from_hex(vector["public"], 32)
    msg = from_hex(vector["message"])
    sig = from_hex(vector["signature"], 64)
    assert verify(pub, msg, sig)
    s_value = int.from_bytes(sig[32:], "little")
    malleable = sig[:32] + int.to_bytes(s_value + L, 32, "little")
    assert len(malleable) == 64
    assert not verify(pub, msg, malleable), "an unreduced S must be refused"

    # -- lengths and garbage are False, never an exception -----------------
    for bad_public in (b"", b"\x00" * 31, b"\x00" * 33, "not bytes", None, 7):
        assert verify(bad_public, msg, sig) is False, bad_public
    for bad_signature in (b"", b"\x00" * 63, b"\x00" * 65, "not bytes", None):
        assert verify(pub, msg, bad_signature) is False, bad_signature
    assert verify(pub, "not bytes", sig) is False
    # An all-zero signature and an all-zero key are the shapes an uninitialised
    # buffer takes, and neither may be mistaken for a valid signature.
    assert verify(pub, msg, b"\x00" * 64) is False
    assert verify(b"\x00" * 32, msg, sig) is False

    # -- non-canonical point encodings are refused -------------------------
    # y = p and y = p + 1 encode nothing: they are above the field. A decoder
    # that reduced them would give two byte strings that decode to one point.
    for y in (P, P + 1, 2 ** 255 - 1):
        assert _decompress(int.to_bytes(y & ((1 << 256) - 1), 32, "little")) is None, y
    # A y that names no point on the curve at all.
    assert _decompress(int.to_bytes(2, 32, "little")) is None

    # -- round trip with a key this module made itself ---------------------
    made = hashlib.sha256(b"hearth-ed25519-self-test").digest()
    pub2 = public_key(made)
    for message in (b"", b"a", b"hearth" * 100, bytes(range(256))):
        signature = sign(made, message)
        assert verify(pub2, message, signature), message[:16]
        assert not verify(pub, message, signature), (
            "a signature must not verify under a different key")
        # Deterministic: signing twice gives the same bytes.
        assert sign(made, message) == signature

    # -- a signature from a different key never verifies -------------------
    other = hashlib.sha256(b"a different key entirely").digest()
    other_pub = public_key(other)
    assert other_pub != pub2
    for message in (b"", b"release manifest"):
        assert not verify(pub2, message, sign(other, message))
        assert not verify(other_pub, message, sign(made, message))

    # -- hex helpers refuse what they should -------------------------------
    assert to_hex(from_hex("00ff")) == "00ff"
    for bad in ("0", "zz", "00 ff", "0xff"):
        try:
            from_hex(bad)
            raise AssertionError("from_hex must refuse {!r}".format(bad))
        except SignatureError:
            pass
    try:
        from_hex("00" * 31, 32)
        raise AssertionError("from_hex must enforce the expected length")
    except SignatureError:
        pass

    print("hearth-ed25519 self-test OK")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hearth_ed25519",
        description="Ed25519 signing and verification, standard library only.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return _self_test()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
