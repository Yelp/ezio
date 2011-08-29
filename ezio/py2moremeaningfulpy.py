"""
    py2moremeaningfulpy
    ~~~~~~~~~~~~~~~~~~~

    This module converts the ASTs generated from the syntactically valid Python
    returned by tmpl2py into something more meaningful that the backend will
    work with.
"""

import _ast
import ast
from copy import deepcopy

# Prevents needless verbosity.
# So that we don't have to constantly say ast.NodeClass to get the node class.
from _ast import (
    arguments,
    Call,
    ClassDef,
    Expr,
    FunctionDef,
    Import,
    ImportFrom,
    Load,
    Module,
    Name,
    Param
)

from ezio.constants import BLOCK_TAG

def py2moremeaningfulpy(tmpl_name, ast_):
    """Convenience function for using AstGen."""

    gen = AstGen(tmpl_name, ast_)
    gen.translate()
    return gen.modnode


# TODO: convert conditional expressions into real if statements, if necessary
# for the codegen


class AstGen(object):
    """After initializing a new instance, call its translate() method to
    perform the conversion and get the resulting AST. The transformation is
    destructive to the AST that is operated on, but a (deep) copy is made to
    hide this from the end user.

    Here are the transformations made:
        1. process ``import foo as __extends__`` into the class statement
        header for the template class.
        2. hoist out nested function definitions out. If the def was the result
        of a #block in the original template file, leave behind a function call
        to it.
        3. put top-level text and control-flow (i.e. non-defs) into a magic
        respond method.
    """

    def __init__(self, tmpl_name, ast_):

        # this is mutated throughout the conversion process
        self.ast_ = deepcopy(ast_)

        # the final module
        self.modnode = Module(body=[]) # list is mutable, we'll rely on that
        ast.copy_location(self.modnode, self.ast_)

        # the resulting class for the template
        self.clsnode = ClassDef(
                name=tmpl_name,
                bases=[], # mutable; we will rely on that
                body=[], # mutable; we will rely on that
                decorator_list=[])
        ast.copy_location(self.clsnode, self.ast_)

        # Protect from translating twice.
        self.has_been_translated = False

    def translate(self):
        """Build the resulting module into `self.modnode`."""

        assert isinstance(self.ast_, ast.Module), "must be a module"

        assert not self.has_been_translated, "already translated"

        # XXX: relies on  "global" instance state

        self._resolve_base_class()
        # flatten defs, and put them into the self.clsnode.body
        self._hoist_defs_into_clsbody()
        # pluck out all top-level imports, and put them in self.modnode.body,
        # preceding self.clsnode
        self._scrape_imports()

        self.clsnode.body.append(self._createrespond())
        self.modnode.body.append(self.clsnode)

        ast.fix_missing_locations(self.modnode)

        self.has_been_translated = True

    def _resolve_base_class(self):
        """Create the node for the class that this template is going to turn
        into.
        """

        body = self.ast_.body

        # if first import is of the form::
        #     import foo.bar.baz as __extends__
        if (isinstance(body[0], Import) and
            len(body[0].names) == 1 and
            body[0].names[0].asname == '__extends__'):

            supercls = body[0].names[0].name
            self.clsnode.bases = [Name(id=supercls, ctx=Load())]

            # remove the magic __extends__ import, mutating self.ast_.body
            body[:] = body[1:]

        else:
            self.clsnode.bases = []

    def _hoist_defs_into_clsbody(self):
        """Hoist out inner function definitions into the body of the class to
        be output. All of the leg work is done by a FunctionDefFlattener
        NodeTransformer.
        """

        self.flattener = FunctionDefFlattener()
        self.flattener.visit(self.ast_)
        self.clsnode.body[:] = self.flattener.hoisted_defs

    def _scrape_imports(self):
        """Append import statments from self.ast_.body to self.modnode.body,
        leaving only nonimports in self.ast_.body.
        """

        nonimports = []
        for node in self.ast_.body:
            if isinstance(node, (Import, ImportFrom)):
                self.modnode.body.append(node)
            else:
                nonimports.append(node)
        self.ast_.body[:] = nonimports

    def _createrespond(self):
        """Create the `respond()` method on the final class."""
        args = arguments(
                args=[Name(id='self', ctx=Param())],
                vararg=None,
                kwarg=None,
                defaults=[])

        # the actual respond function
        respond = FunctionDef(
                name='respond',
                args=args,
                body=self.ast_.body, # whatever is left at the top level
                decorator_list=[])

        ast.copy_location(self.ast_, respond)
        ast.fix_missing_locations(respond)

        return respond


class FunctionDefFlattener(ast.NodeTransformer):
    """Accumulate all function definitions and remove them from the AST. If
    the function definition is tagged as being a #block, leave behind a
    function call to it. Also, interpret EZIO_skip and EZIO_noop.
    """

    def __init__(self):
        self.hoisted_defs = []

    def visit_FunctionDef(self, node):
        # Pseudocode:
        # * flatten this node's children, then
        # * if it's a #block node
        #     + add the def to self.hoisted_defs, unmunging its name
        #     + return a function call to ourself (replaces us)
        # * else
        #     + add the def to self.hoisted_defs
        #     + return None (delete this node)

        # Recurse on children. After this step, the function body is
        # flattened. This order means that the hoisted functions will be in
        # postorder.
        skip = False
        noop = False

        # process the magical EZIO_skip and EZIO_noop decorators:
        for decorator in node.decorator_list:
            if isinstance(decorator, _ast.Name):
                if decorator.id == 'EZIO_skip':
                    skip = True
                elif decorator.id == 'EZIO_noop':
                    noop = True
        if skip:
            # omit the entire node, without adding it to self.hoisted_defs:
            return None
        elif noop:
            # wipe out the node's body:
            node.body = [_ast.Pass()]

        self.generic_visit(node)

        if node.name.startswith(BLOCK_TAG): # we're a #block

            blockname = node.name[len(BLOCK_TAG):]
            assert len(blockname) > 0, "a #block must have a name"

            # add the def, with name replaced by `blockname`, to
            # self.hoisted_defs
            node.name = blockname
            node.args.args.insert(0, Name(id='self', ctx=Param()))
            self.hoisted_defs.append(node)

            # return an expression with a function call to ourself
            funcall = Call(
                    func=ast.Name(id=blockname, ctx=ast.Load()),
                    args=[],
                    keywords=[],
                    starargs=None,
                    kwargs=None)

            # make an expression statement
            expr = Expr(value=funcall)

            # don't lose debugging locations!
            ast.copy_location(expr, node)
            ast.fix_missing_locations(expr)

            return expr # replace ourself with the expression statement

        else:

            node.args.args.insert(0, Name(id='self', ctx=Param()))
            self.hoisted_defs.append(node)
            return None # notify the NodeTransformer to delete us
