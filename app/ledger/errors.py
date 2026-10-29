class LedgerError(Exception):
    """A business-rule violation. The message is safe to show / speak to the merchant."""


class NotFoundError(LedgerError):
    pass
