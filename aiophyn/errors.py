"""Define package errors."""


class PhynError(Exception):
    """Define a base error."""

    ...


class RequestError(PhynError):
    """Define an error related to invalid requests."""

    ...

class BrandError(Exception):
    """ Define an error related to invalid brands."""

    ...
