"""
Magic words and numbers shared between compiler components; knobs and switches.
"""

# magic marker used by tmpl2py when compiling #super (means "name of enclosing method")
CURRENT_METHOD_TAG = '__EZIO_current_method'

# magic marker used by tmpl2py to tell py2moremeaningfulpy that the current function was
# originally a #block, rather than a #def
BLOCK_TAG = 'DIRECTIVE__block__'

# builtin module (as in, the return value of `__import__('__builtin__')`)
BUILTIN_MODULE_NAME = '__builtin__'

# make all these built-in functions and objects available and statically resolvable
# (because we resolve these names statically, we can't examine __builtin__ at runtime
# and make all the names available --- and a lot of the builtins are things we actively
# want to prevent people from using)
BUILTINS_WHITELIST = [
        'None', 'True', 'False', 'len', 'enumerate', 'range', 'xrange', 'list', 'tuple',
        'int', 'str', 'float', 'bool', 'dict', 'set', 'len', 'sum', 'min', 'max', 'any',
        'all', 'sorted', 'print', 'repr', 'next', 'zip', 'map', 'reduce',
]

class CompilerSettings(object):
    """Holds all the switches and the knobs to control compilation."""

    # enables use of the variadic path lookup method in Ezio.h,
    # as opposed to the PathRegistry:
    use_variadic_path_resolution = False

    # this causes bare expression statements to be written to the
    # templating transaction, and also enables special template
    # semantics like dotted path lookup. The idea is that with this on,
    # the compiler should compile the ASTs from py2moremeaningfulpy,
    # and with it off, it should be a generalized Python AST compiler
    # (although clearly most functionality is not implemented yet)
    template_mode = True
