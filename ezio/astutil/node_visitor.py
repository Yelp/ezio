from _ast import AST

class NodeVisitor(object):
    """
    This is partly copied and pasted from Python 2.6's ast.NodeVisitor.
    We've added a variable_name kwarg that can be passed to each visit_ method;
    when you visit_ an expression and pass a variable_name, the expression is
    compiled so that its value ends up in variable_name, and the visitor returns
    whether a new reference was created.
    """

    def visit(self, node, variable_name=None):
        """Visit a node."""
        method = 'visit_' + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        return visitor(node, variable_name=variable_name)

    def generic_visit(self, node, variable_name=None):
        """Called if no explicit visitor function exists for a node."""
        raise NotImplementedError, 'Generation for type %r not implemented.' % (type(node),)
