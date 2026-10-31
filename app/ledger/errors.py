from app.i18n import DEFAULT_LANG, t


class LedgerError(Exception):
    """A business-rule violation. The message is safe to show / speak to the merchant.

    Raise with `LedgerError.of("err_key", **params)` to make the message translatable; str(exc) stays
    Hindi (which is what the LLM reads), `exc.text(lang)` gives it in the merchant's language.
    """

    key: str | None = None
    params: dict = {}

    @classmethod
    def of(cls, key: str, **params):
        exc = cls(t(DEFAULT_LANG, key, **params))
        exc.key, exc.params = key, params
        return exc

    def text(self, lang: str | None) -> str:
        return t(lang, self.key, **self.params) if self.key else str(self)


class NotFoundError(LedgerError):
    pass
