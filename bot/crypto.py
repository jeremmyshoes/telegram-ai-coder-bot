"""Шифрование чувствительных данных (API-ключей пользователей) через Fernet."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class KeyVault:
    """Обёртка над Fernet для шифрования/дешифрования строк."""

    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "ENCRYPTION_KEY некорректен. Сгенерируйте через "
                "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`"
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Не удалось расшифровать значение (возможно сменился ENCRYPTION_KEY)") from exc
