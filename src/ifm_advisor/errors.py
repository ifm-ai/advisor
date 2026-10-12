"""Errors raised by the advisor."""


class AdvisorRequestError(RuntimeError):
    """The advisor request could not be built or sent, or returned no usable content."""
