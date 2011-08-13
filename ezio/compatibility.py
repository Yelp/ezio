"""
Utilities to help with bidirectional Cheetah compatibility.
"""

def EZIO_skip(func):
    """No-op decorator; tells EZIO to ignore a function definition.

    Use this for code in a common template that you want to be visible to Cheetah,
    but which EZIO should ignore at compile-time.

    This only works if you decorate with exactly `EZIO_skip`; recommended use is
    `from ezio.compatibility import EZIO_skip` and then `@EZIO_skip`.
    """
    return func

def EZIO_noop(func):
    """No-op decorator; tells EZIO to compile a function as empty.

    Use this for code in a common template that you want to call from Cheetah,
    but which EZIO should ignore at runtime.

    See usage note for EZIO_skip.
    """
    return func
