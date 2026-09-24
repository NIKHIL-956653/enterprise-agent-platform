"""
Password hashing.

argon2id: memory-hard, so a GPU cracking rig loses most of its speed advantage over
a normal CPU. Current OWASP first choice. Never store or log a plaintext password.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# One hasher for the process. Defaults follow the argon2-cffi maintainers' recommended
# parameters and change as hardware gets faster - that's why `needs_rehash` exists below.
_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Return an argon2id hash. The salt is random per call and stored inside the string."""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """
    True if the password matches.

    Returns False rather than raising, so a bad password and a corrupt hash look the same
    to the caller - the login endpoint must not reveal which one it was.
    """
    try:
        return _hasher.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """
    True if this hash used weaker parameters than we now use.

    Call it right after a successful login: you have the plaintext at that moment, so you
    can silently upgrade the stored hash. It's how a system strengthens itself over years
    without ever asking users to reset anything.
    """
    return _hasher.check_needs_rehash(hashed)
