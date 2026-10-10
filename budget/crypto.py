from cryptography.fernet import Fernet

from . import config


def _fernet():
    return Fernet(config.TOKEN_ENCRYPTION_KEY.encode())


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value):
    return _fernet().decrypt(value.encode()).decode()
