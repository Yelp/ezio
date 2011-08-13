"""
Magic words and numbers shared between compiler components.
"""

# magic marker used by tmpl2py when compiling #super (means "name of enclosing method")
CURRENT_METHOD_TAG = '__EZIO_current_method'

# magic marker used by tmpl2py to tell py2moremeaningfulpy that the current function was
# originally a #block, rather than a #def
BLOCK_TAG = 'DIRECTIVE__block__'
