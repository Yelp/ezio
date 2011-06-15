"""
Code to compile (restricted) Python ASTs to C++ and package the resulting code as
C extension modules.
"""
from __future__ import with_statement

import _ast
import itertools
import operator
from contextlib import contextmanager

from ezio.astutil.node_visitor import NodeVisitor


DISPLAY_NAME = "display"
TRANSACTION_NAME = "transaction"
LITERALS_ARRAY_NAME = "string_literals"
IMPORT_ARRAY_NAME = 'imported_names'
MAIN_FUNCTION_NAME = "respond"
TERMINAL_EXCEPTION_HANDLER = "REPORT_EXCEPTION_TO_PYTHON"

RESERVED_WORDS = set([DISPLAY_NAME, TRANSACTION_NAME, LITERALS_ARRAY_NAME, IMPORT_ARRAY_NAME, MAIN_FUNCTION_NAME])

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


class LineBufferMixin(object):
    """Mixin for classes that maintain a collection of indented lines.
    Use it like this:
    self.indent += 1
    self.add_line('foo = bar;')
    self.indent -= 1
    """

    INDENT_WS = '\t'

    def __init__(self, initial_indent=0):
        super(LineBufferMixin, self).__init__()
        self.indent = self.initial_indent = initial_indent
        # default segment:
        self.lines = []

    def add_line(self, line=""):
        # ayust will get mad if we have leading whitespace on our blank lines
        indent = self.indent if line else 0
        self.lines.append('%s%s' % (self.INDENT_WS * indent, line))

    @contextmanager
    def increased_indent(self, increment=1):
        assert increment > 0
        self.indent += increment
        yield
        self.indent -= increment


class LiteralRegistry(object):
    """Encapsulates the creation of Python string objects for all string literals
    that appear in the template.
    """

    def __init__(self):
        self.literals = []
        self.literal_to_index = {}

    def register(self, literal):
        index = self.literal_to_index.get(literal)
        if index is not None:
            return index

        self.literals.append(literal)
        index = len(self.literals) - 1
        self.literal_to_index[literal] = index
        return index

    def dump(self):
        """
        http://stackoverflow.com/questions/4000678/using-python-to-generate-a-c-string-literal-of-json
        """
        pieces = ["static PyObject *%s[%d];" % (LITERALS_ARRAY_NAME, len(self.literals))]
        pieces.append("static void init_string_literals(void) {")
        for pos, literal in enumerate(self.literals):
            formatted_literal = '"' + literal.replace('\\', r'\\').replace('"', r'\"').replace("\n", r'\n') + '"'
            pieces.append("\t%s[%d] = PyString_FromString(%s);" % (LITERALS_ARRAY_NAME, pos, formatted_literal))
        pieces.append("}")
        pieces.append("static void destroy_string_literals(void) {")
        pieces.append("\tPy_ssize_t i;")
        pieces.append("\tfor (i = 0; i < %d; i++) {" % len(self.literals))
        pieces.append("\t\tPy_DECREF(%s[i]);" % LITERALS_ARRAY_NAME)
        pieces.append("\t}")
        pieces.append("}")
        #return '\n'.join(pieces)
        return pieces

class ImportRegistry(object):
    """Encapsulates tracking of imported names, and the code to perform the imports."""

    def __init__(self):
        # list of string of C-API code for executing the imports
        self.imports = []
        self.symbols_to_index = {}
        self.num_objects = 0

    def _get_array_accessor(self, index):
        return '%s[%d]' % (IMPORT_ARRAY_NAME, index)

    def register(self, name, asname=None, module=None):
        """Registers an import.
        Args:
            name - desired name to be imported
                (for "import foo.bar" this is foo.bar, for "from foo.bar import baz" this is baz)
            asname - name to introduce into the namespace instead of `name`
            module - for a from import, the base module, i.e.,
                foo.bar in "from foo.bar import baz"
        """
        # FIXME this doesn't cover every case
        if module:
            target = "%s.%s" % (module, name)
        else:
            target = name

        index = self.num_objects
        array_accessor = self._get_array_accessor(index)
        func_name = 'PyImport_ImportModule'
        self.imports.append('%s = %s("%s");' % (array_accessor, func_name, target))

        # XXX in Python, the expression foo.bar.baz is "attribute access on foo, then attribute access on the result",
        # so paths are always resolved from a bare name. We're retaining the possibility of "flattening" accesses
        # to imported names, so that foo.bar.baz will be statically resolved to a pointer to an imported PyObject*
        if asname is not None:
            resolvable_path = (asname,)
        else:
            resolvable_path = tuple(name.split('.'))

        self.symbols_to_index[resolvable_path] = index
        self.num_objects += 1

    def resolve_import_path(self, path):
        """Produces the C-language expression corresponding to an import path, or None."""
        index = self.symbols_to_index.get(path)
        if index is None:
            return None
        return self._get_array_accessor(index)

    def dump(self):
        # TODO: import failures are hidden and silent
        pieces = []
        if self.num_objects:
            pieces.append('static PyObject *%s[%d];' % (IMPORT_ARRAY_NAME, self.num_objects))

        pieces.append('static void init_imports(void) {')
        pieces.extend('\t' + code for code in self.imports)
        pieces.append('}')
        return pieces


class PathRegistry(LineBufferMixin):
    """Encapsulates tracking of "paths", e.g., the .bar.baz in foo.bar.baz,
    and the pieces of code that perform the dotted-path lookup for them.
    """

    def __init__(self, literal_registry):
        super(PathRegistry, self).__init__()
        self.subpath_to_fname = {}
        self.unique_id_counter = itertools.count()
        self.literal_registry = literal_registry

    def register(self, subpath):
        assert isinstance(subpath, tuple)
        if subpath in self.subpath_to_fname:
            return self.subpath_to_fname[subpath]
        self.subpath_to_fname[subpath] = "resolvepath_%d" % self.unique_id_counter.next()
        # ensure that all the path names have been registered
        # (self.dump() won't be called until after the literal registry has been dumped)
        for subpath_item in subpath:
            self.literal_registry.register(subpath_item)
        return self.subpath_to_fname[subpath]

    def dump(self):
        for subpath, fname in self.subpath_to_fname.iteritems():
            # generate a function that follows 'subpath' on 'base'
            # and returns a new reference to whatever it finds (or NULL)
            self.add_line("static PyObject *%s(PyObject *base) {" % (fname,))
            self.indent += 1
            self.add_line("/* Resolves %s */" % (subpath,))
            # PyDict_GetItem can segfault on NULL, so let's check:
            self.add_line("if (base == NULL) return NULL;")
            if len(subpath) > 0:
                # suppress a GCC warning about this var being unused for the empty path:
                self.add_line("PyObject *temp;")
            else:
                # if subpath is empty, INCREF would never be called, so we need to add a special case here
                self.add_line('Py_XINCREF(base);')

            for index, subpath_item in enumerate(subpath):
                last_item = (index == len(subpath) - 1)

                literal_id = self.literal_registry.register(subpath_item)
                c_expression_for_literal = "%s[%d]" % (LITERALS_ARRAY_NAME, literal_id)
                self.add_line("temp = PyDict_GetItem(base, %s);" % (c_expression_for_literal,))
                # PyObject_GetAttr returns a new reference, but GetItem returns a borrowed one
                # we *should* be safe in decref'ing the new ref, to match the borrowed ref behavior.
                # XXX this is unsafe if getattr can nuke the base object, i.e., it is in principle unsafe
                # to PyObject_GetAttr on an object that you only have a borrowed ref to, but this would
                # only be true of pathological __getattr__ implementations and we'll ignore this case for now.
                self.add_line("if (temp == NULL) {")
                self.indent += 1
                self.add_line("temp = PyObject_GetAttr(base, %s);" % (c_expression_for_literal,))
                self.add_line("if (temp == NULL) return NULL;")
                if not last_item:
                    # GetAttr returned a new ref, let's remove it
                    self.add_line("Py_DECREF(temp);")
                self.indent -= 1
                self.add_line("} else {")
                if last_item:
                    # incref the borrowed ref from PyDict_GetItem
                    self.indent += 1
                    self.add_line("Py_XINCREF(temp);")
                    self.indent -= 1
                self.add_line("}")
                self.add_line("base = temp;")
            self.add_line("return base;")
            self.indent -= 1
            self.add_line("}")
        return self.lines


def positional_args_and_self_arg(args):
    """Divide positional parameters for a method into "self" and the actual positional params.

    Arguments:
        args: ast.arguments, i.e., (args, vararg, kwarg, defaults)
    Returns: list of param names, name of the self argument
    """
    assert len(args.args) > 0, 'Method has no self argument.'
    self_arg = args.args[0].id
    return [arg.id for arg in args.args[1:]], self_arg


class ClassDefinition(LineBufferMixin):
    """Encapsulates a definition of a class.
    This implementation relies on all classes being compiled
    at the same time, in the correct order...
    """

    def generate_constructor(self):
        # if there's a superclass def we don't declare members or a new constructor
        if self.superclass_def:
            self.add_line('%s (PyObject *%s, PyObject *%s) : %s(%s, %s) {}' % \
                (self.class_name, DISPLAY_NAME, TRANSACTION_NAME,
                self.superclass_def.class_name, DISPLAY_NAME, TRANSACTION_NAME))
            return

        # otherwise, declare members:
        self.add_line('PyObject *%s, *%s;' % (DISPLAY_NAME, TRANSACTION_NAME))

        self.add_line('%s (PyObject *%s, PyObject *%s) {' % (self.class_name, DISPLAY_NAME, TRANSACTION_NAME))
        self.indent += 1
        # could use an initialization list for this, but whatever:
        for varname in (DISPLAY_NAME, TRANSACTION_NAME):
            self.add_line('this->%s = %s;' % (varname, varname))
        self.indent -= 1
        self.add_line('}')

        # no destructor, this thing doesn't own any dynamically allocated memory
        # (really it only exists because we need its vtable)

    def get_method(self, method_name):
        if self.superclass_def is not None:
            superclass_method = self.superclass_def.get_method(method_name)
            if superclass_method is not None:
                return superclass_method

        return self.methods.get(method_name)

    def has_method(self, method_name):
        return self.get_method(method_name) is not None

    def add_method(self, method, params):
        assert method == MAIN_FUNCTION_NAME or method not in RESERVED_WORDS, 'Method name %s is a reserved word' % method
        assert method not in self.methods, 'Cannot double-define method %s for class %s' % (method, self.class_name)

        superclass_method = None
        if self.superclass_def is not None:
            superclass_method = self.superclass_def.get_method(method)
        if superclass_method is not None:
            assert len(superclass_method['params']) == len(params), 'Incompatible superclass definition'
            superclass_method['virtual'] = True

        # OK, this is the first place in the hierarchy that this method has been defined:
        self.methods[method] = {
            'name': method,
            'params': params,
            'virtual': False
        }

    def __init__(self, class_name, superclass_def):
        super(ClassDefinition, self).__init__()

        self.class_name, self.superclass_def = class_name, superclass_def

        superclass_declaration = ''
        if superclass_def is not None:
            superclass_declaration = ': public %s ' % superclass_def.class_name
        self.add_line('class %s %s{' % (class_name, superclass_declaration))

        self.indent += 1
        # all methods and members are C++-public:
        self.add_line('public:')

        self.generate_constructor()

        self.methods = {}

    def dump(self):
        for method in self.methods.itervalues():
            method_definition = 'virtual PyObject *' if method['virtual'] else 'PyObject *'
            method_definition += method['name']
            args_definition = '(' + ', '.join('PyObject *%s' % param for param in method['params']) + ');'
            self.add_line(method_definition + args_definition)

        self.indent -= 1
        self.add_line('};')

        return self.lines


def generate_initial_segment():
    return [
        '#include "Python.h"',
        '#include "Ezio.h"',
        ''
    ]


def generate_final_segment(module_name, function_names):
    """Generate the final segment of the C++ file, which contains
    the module initialization code.
    """
    buf = LineBufferMixin()

    buf.add_line("static PyMethodDef k_module_methods[] = {")
    buf.indent += 1
    for function_name in function_names:
        buf.add_line('{"%s", (PyCFunction)%s, METH_VARARGS, "Perform templating for %s"},' %
            (function_name, function_name, function_name))
    buf.add_line("{NULL, NULL, 0, NULL}")
    buf.indent -= 1
    buf.add_line("};")

    buf.add_line()

    buf.add_line("PyMODINIT_FUNC init%s(void) {" % module_name)
    buf.indent += 1
    buf.add_line('Py_InitModule("%s", k_module_methods);' % module_name)
    buf.add_line('init_string_literals();')
    buf.add_line('init_imports();')
    buf.indent -= 1
    buf.add_line('}')
    buf.add_line('')

    return buf.lines


def generate_hook(function_name, class_name):
    """Generate the static "hook" function that unpacks the Python arguments,
    dispatches to the C++ code, then returns the result to Python.
    """
    buf = LineBufferMixin()

    buf.add_line('static PyObject *%s(PyObject *self, PyObject *args) {' % function_name)
    buf.indent += 1
    buf.add_line('PyObject *%s, *%s;' % (DISPLAY_NAME, TRANSACTION_NAME))
    # TODO type-check list and dict here
    buf.add_line('if (!PyArg_ParseTuple(args, "OO", &%s, &%s)) {'
        % (DISPLAY_NAME, TRANSACTION_NAME))
    buf.indent += 1
    buf.add_line('return NULL;')
    buf.indent -= 1
    buf.add_line('}')
    buf.add_line('%s template_obj(%s, %s);' % (class_name, DISPLAY_NAME, TRANSACTION_NAME))
    buf.add_line('PyObject *status = template_obj.%s();' % (MAIN_FUNCTION_NAME,))
    buf.add_line('if (status) {')
    buf.indent += 1
    buf.add_line('Py_INCREF(status); return status;')
    buf.indent -= 1
    buf.add_line('} else { return NULL; }')
    buf.indent -= 1
    buf.add_line('}')

    return buf.lines


def generate_c_file(module_name, literal_registry, path_registry, import_registry, compiled_classes):
    """Generate a complete C++ source file; string literals, path lookup functions,
    imports, all code for all classes, hooks, final segment.
    """
    all_lines = []

    all_lines += generate_initial_segment()
    all_lines += literal_registry.dump()
    all_lines += path_registry.dump() if path_registry is not None else []
    all_lines += import_registry.dump()

    # concatenate all class definitions and their method definitions
    hook_names = []
    for compiled_class in compiled_classes:
        all_lines += compiled_class.class_definition.dump()
        all_lines += compiled_class.lines

        class_name = compiled_class.class_definition.class_name
        hook_name = "%s_%s" % (class_name, MAIN_FUNCTION_NAME)
        all_lines += generate_hook(hook_name, class_name)
        hook_names.append(hook_name)

    all_lines += generate_final_segment(module_name, hook_names)

    return '\n'.join(all_lines)


class CodeGenerator(LineBufferMixin, NodeVisitor):
    """
    This subclasses a near relative of ast.NodeVisitor in order to walk a Python AST
    and generate C++ code for it.

    Methods look like this:
    visit_SomeExpressionNode(self, node, variable_name=None)

    If variable_name is None, the expression is evaluated and the result is appended
    to the transaction. If it is not None, the expression is evaluated and assigned
    to the variable name in question; then the return value of the visitor is whether
    a new reference to that value was created.

    Some methods, e.g., visit_Str, may return borrowed references --- but only if there
    is a guarantee that the reference will persist across re-entry into Python code.
    For example, it is possible to borrow a reference to a string literal from the $LITERALS_ARRAY_NAME,
    because the template module will always own one reference to those literals --- but it is not
    acceptable to borrow a reference to a name from the display dictionary, since re-entry into Python
    could result in all those references being removed and the object being freed.

    self.exception_handler_stack[-1] should contain a label that can be jumped to
    to fall all the way down the call stack and back into Python.
    """

    def __init__(self, class_definition=None, literal_registry=None, path_registry=None, import_registry=None, compiler_settings=None,
            unique_id_counter=None, superclass_definition=None):
        super(CodeGenerator, self).__init__()

        # this one can stay null if we don't have one already;
        # this gets passed in when we're generating code for a function or block
        # and some other CodeGenerator is doing the top-level work on the class.
        self.class_definition = class_definition
        self.superclass_definition = superclass_definition

        assert not(bool(literal_registry) ^ bool(path_registry)), 'Must supply both literal and path registries, or neither'
        self.registry = LiteralRegistry() if literal_registry is None else literal_registry
        self.path_registry = PathRegistry(self.registry) if path_registry is None else path_registry
        self.import_registry = ImportRegistry() if import_registry is None else import_registry
        self.compiler_settings = CompilerSettings() if compiler_settings is None else compiler_settings
        self.unique_id_counter = itertools.count() if unique_id_counter is None else unique_id_counter

        self.lines = []

        # our reimplementation of VFSSL:
        # static lookup among imported names, function arguments,
        # and the variable names in for loops, in REVERSE order
        # fail over to dynamic lookup in the display dict
        self.namespaces = []

        # this is a stack of breadcrumbs to follow (with goto) when encountering an exception;
        # you fall all the way down the stack, cleaning up stray references as you go,
        # until finally you return a null pointer back to the Python calling code
        self.exception_handler_stack = []

    def visit_Module(self, module_node, variable_name=None):
        assert variable_name is None, 'Cannot compile module for assignment.'

        for stmt in module_node.body:
            self.visit(stmt)

    def visit_ClassDef(self, class_node, variable_name=None):
        """Compile the class definition for a template module.
        Of course, multiple class definitions can appear in a single
        Python module, but we're relying on there being only one
        (since our pipeline converts .tmpl files into Python
        modules containing a single class).
        """
        # we have to get all the classes and t-sort them by inheritance at build time,
        # otherwise we'll have no idea how to compile invocations, because we won't know if
        # the function being called is an inherited native method or a Python callable from
        # the display dict until we know what the available methods are.

        assert len(class_node.bases) <= 1, 'Multiple inheritance is unsupported'
        if class_node.bases:
            assert isinstance(class_node.bases[0], _ast.Name)
            superclass_name = class_node.bases[0].id
            assert self.superclass_definition is not None, 'Superclass %s found for %s, but no definition supplied.' % \
                (superclass_name, class_node.name)
            assert self.superclass_definition.class_name == superclass_name, 'Wrong superclass definition provided.'
        else:
            assert self.superclass_definition is None, 'Superclass def supplied for %s, but no superclass specified syntactically' % \
                (class_node.name,)

        self.class_definition = ClassDefinition(class_node.name, self.superclass_definition)

        # must statically discover all method names
        write_toplevel_entities = self.superclass_definition is None
        function_definitions = [stmt for stmt in class_node.body if isinstance(stmt, _ast.FunctionDef)]
        for function in function_definitions:
            # .args is all arguments, .args.args is the positional params:
            param_names, _ = positional_args_and_self_arg(function.args)
            # skip the definition of the main method, if this is a subclass:
            if function.name != MAIN_FUNCTION_NAME or write_toplevel_entities:
                self.class_definition.add_method(function.name, param_names)

        for stmt in class_node.body:
            assert isinstance(stmt, _ast.FunctionDef), 'Cannot compile non-method elements of classes.'
            # and skip the compilation of the main method as well if necessary, as above:
            if stmt.name != MAIN_FUNCTION_NAME or write_toplevel_entities:
                self.visit_FunctionDef(stmt, method=True)

    def visit_Expr(self, expr, variable_name=None):
        """Expr is a statement for a bare expression, wrapping the expression as  .value."""
        return self.visit(expr.value, variable_name=variable_name)

    def visit_Str(self, str_node, variable_name=None):
        # value of a string literal is the member 's'
        index = self.registry.register(str_node.s)
        literal_reference = "%s[%d]" % (LITERALS_ARRAY_NAME, index)
        if variable_name:
            # return a borrowed reference:
            self.add_line("%s = %s;" % (variable_name, literal_reference))
            return False
        elif self.compiler_settings.template_mode:
            self._template_write(literal_reference, test_null=False)

    def visit_FunctionDef(self, function_def, method=False):
        argslist_str, arg_namespace = self._generate_argslist_for_declaration(function_def.args, method=method)
        self.add_line("PyObject* %s::%s(%s) {" % (self.class_definition.class_name, function_def.name, argslist_str))

        # this would only be possible if function defs were nested:
        assert len(self.exception_handler_stack) == 0
        exception_handler = "HANDLE_EXCEPTIONS_%s_%d" % (function_def.name, self.unique_id_counter.next())
        self.exception_handler_stack.append(exception_handler)

        self.namespaces.append(arg_namespace)
        self.indent += 1
        for stmt in function_def.body:
            self.visit(stmt)
        # XXX Py_None is being used as a C-truthy sentinel for success
        self.add_line("return Py_None;")
        self.add_line('%s:' % exception_handler)
        self.add_line("return NULL;")
        self.indent -= 1
        self.namespaces.pop()
        self.add_line("}")

        self.exception_handler_stack.pop()

    def _generate_argslist_for_declaration(self, args, method=False):
        """Returns both the generated code and a
        namespace dict for the defined arguments.
        """
        # args has members "args", "defaults", "kwarg", "vararg";
        # the first of these is the positional parameters and that's what we care about
        assert not any ((args.defaults, args.kwarg, args.vararg)), "Kwargs/varargs currently unsupported"

        if method:
            positional_args, self_arg = positional_args_and_self_arg(args)
        else:
            positional_args, self_arg = args.args, None

        defined_args = ["PyObject *%s" % name for name in positional_args]
        argslist_str = ", ".join(defined_args)

        # these names can be resolved "natively", i.e., they're in the C namespace
        namespace = dict((argname, 'NATIVE') for argname in positional_args)
        # this is to facilitate later compilation of code that refers to "self";
        # right now, any references to "self" should produce an error
        if self_arg is not None:
            namespace[self_arg] = 'SELF'
        return argslist_str, namespace

    def _generate_argslist_for_invocation(self, args, unique_id, exception_handler):
        """Generate C expressions and temporary variables for all the arguments to the invocation."""
        # skip over annoying edge cases when there are no args:
        if not args:
            return []

        # generate names and declarations for the variables that will hold the invocation arguments
        args_tempvars = ["tempvar_%d_%d" % (unique_id, i) for i in xrange(len(args))]
        argslist_declarations = ", ".join('*%s' % varname for varname in args_tempvars)
        self.add_line("PyObject %s = NULL;" % (argslist_declarations,))

        tempvars_and_newrefs = []
        for tempvar, expr in zip(args_tempvars, args):
            new_ref = self.visit(expr, variable_name=tempvar)
            tempvars_and_newrefs.append((tempvar, new_ref))

        # eagerly evaluate every argument, then fail if any did not evaluate correctly
        # TODO do something about all these unused labels...
        all_tempvars_nonnull = " && ".join(args_tempvars)
        # attempt a decref on every argument that would have created a new reference,
        # had it been successfully evaluated (unsuccessful evaluation yields simply a null pointer):
        cleanup_all_tempvars = " ".join("Py_XDECREF(%s);" % (tempvar,) for tempvar, newref in tempvars_and_newrefs if newref)
        self.add_line("if (!(%s)) { %s; goto %s; }" % (all_tempvars_nonnull, cleanup_all_tempvars, exception_handler))

        return tempvars_and_newrefs

    def visit_For(self, forloop, variable_name=None):
        """Compile a for loop, block-scoped.
        TODO the temporary variable is scoped to the inside of the for loop; this violates Python semantics
        but is not easily adapted to the C++ setting.
        """
        assert variable_name is None, 'For loops are not expressions'
        assert not forloop.orelse, 'Cannot compile else block of for loop'
        # block-scope the whole thing, to prevent C++ complaining about jumps over initialization statements
        self.add_line()
        self.add_line('{')

        # target (i.e., the temporary variable) is an expr, which for us must be a Name, whose actual name is in the id member
        # TODO we could support tuple unpacking here
        assert isinstance(forloop.target, _ast.Name), 'For-loop target must be a name'
        var_name = forloop.target.id

        unique_id = self.unique_id_counter.next()
        inner_exception_handler = "INNER_HANDLE_EXCEPTIONS_%d" % unique_id
        # handles exceptions inside the loop
        outer_exception_handler = "OUTER_HANDLE_EXCEPTIONS_%d" % unique_id

        temp_sequence_name = 'temp_sequence_%d' % unique_id
        temp_fast_sequence_name = "temp_fast_sequence_%d" % unique_id
        self.add_line('PyObject *%s, *%s = NULL;' % (temp_sequence_name, temp_fast_sequence_name))
        length_name = "temp_sequence_length_%d" % unique_id
        self.add_line("Py_ssize_t %s;" % length_name)

        new_ref_created_for_iterable = self.visit(forloop.iter, variable_name=temp_sequence_name)

        self.add_line('if (!(%s)) { goto %s; }' % (temp_sequence_name, outer_exception_handler))
        self.add_line('if (!(%s = PySequence_Fast(%s, "Not a sequence."))) { goto %s; };' % (temp_fast_sequence_name, temp_sequence_name, outer_exception_handler))
        length_name = "temp_sequence_length_%d" % unique_id
        self.add_line("%s = PySequence_Fast_GET_SIZE(%s);" % (length_name, temp_fast_sequence_name))
        counter_name = 'counter_%d' % unique_id
        self.add_line("Py_ssize_t %s;" % counter_name)
        self.add_line("for(%(counter)s = 0; %(counter)s < %(length)s; %(counter)s++) {" % {'counter': counter_name, 'length': length_name})

        self.indent += 1
        self.add_line("PyObject *%s = PySequence_Fast_GET_ITEM(%s, %s);" % (var_name, temp_fast_sequence_name, counter_name))
        self.add_line("if (!%s) { goto %s; }" % (var_name, outer_exception_handler))
        # GET_ITEM borrowed a reference, let's incref this thing while we're using it
        self.add_line("Py_INCREF(%s);" % var_name)
        self.namespaces.append({var_name: 'NATIVE'})
        self.exception_handler_stack.append(inner_exception_handler)

        # compile the body of the for loop
        # TODO assignment statements will necessitate scoping that will spill outside of this:
        for stmt in forloop.body:
            self.visit(stmt)

        self.exception_handler_stack.pop()
        self.namespaces.pop()

        # decref the item we got from the list and incref'ed above:
        self.add_line("Py_DECREF(%s);" % var_name)

        # inner exception handler: decref the temporary variable name:
        self.add_line("if (0) {")
        self.indent += 1
        self.add_line('%s:' % inner_exception_handler)
        self.add_line('Py_DECREF(%s);' % var_name)
        # defer remaining cleanup to the outer exception handler
        self.add_line('goto %s;' % outer_exception_handler)
        self.indent -= 1
        self.add_line("}")

        self.indent -= 1
        self.add_line("}")

        # NON-exceptional path; deterministically decref and skip the exception handlers
        if new_ref_created_for_iterable:
            self.add_line("Py_DECREF(%s);" % (temp_sequence_name,))
        self.add_line("Py_DECREF(%s);" % (temp_fast_sequence_name))

        # safe-decref the temporary variables
        self.add_line("if (0) {")
        self.indent += 1
        self.add_line("%s:" % outer_exception_handler)
        if new_ref_created_for_iterable:
            self.add_line("Py_XDECREF(%s);" % (temp_sequence_name,))
        self.add_line("Py_XDECREF(%s);" % (temp_fast_sequence_name))
        self.add_line("goto %s;" % self.exception_handler_stack[-1])
        self.indent -= 1
        self.add_line("}")

        # close the block scope
        self.add_line('}')

    def visit_Call(self, call_node, variable_name=None):
        """This is like generate_path; with write=True it generates code to do the invocation
        and append the result to the transaction, with write=False it generates code to do
        the invocation and returns (in Python) a C expression for the result.

        Here's the convention for write=False; if you call a codegen method with write=False,
        it will dump out a bunch of "setup" code (e.g., resolving a path, whatever), then return
        a C expression for the value you want; the value will be INCREF'ed if appropriate,
        and if it is, it's up to you to get it DECREF'ed.

        Note that it is known at compile time whether the function being invoked is a Python callable
        or a C function.
        """
        # positional params are in call_node.args, all else is unsupported:
        assert not any((call_node.keywords, call_node.starargs, call_node.kwargs)), 'Unsupported feature'

        unique_id = self.unique_id_counter.next()

        exception_handler = "HANDLE_EXCEPTIONS_%d" % unique_id

        # create block scope for temporary variables:
        self.add_line('{')

        function_name = None
        c_function = False
        # a c function is a bare name that appears in the current class definition as a method:
        if isinstance(call_node.func, _ast.Name):
            function_name = call_node.func.id
            c_function = self.class_definition.has_method(function_name)
        # python callables have a callable object and a return value, create temp vars for these
        result_name = None
        if not c_function:
            temp_callable_name = "tempcallablevar_%d" % unique_id
            self.add_line('PyObject *%s;' % temp_callable_name)

            # were we given an externally scoped C variable to put the result in?
            if variable_name:
                result_name = variable_name
            else:
                result_name = "tempresultvar_%d" % unique_id
                self.add_line('PyObject *%s;' % result_name)

        # we've got new references to all the arguments, so create temp vars for them:
        argname_and_newrefs = self._generate_argslist_for_invocation(call_node.args, unique_id, exception_handler)
        args_tempvars = [argname for argname, _ in argname_and_newrefs]

        if c_function:
            # this is a C function, which we will invoke and which will modify
            # transaction in-place rather than returning a value
            # TODO there may be some use case for C functions that return values...
            assert not variable_name, '%s is a C function, at this time C functions do not return values' % function_name
            # generate code that invokes the C function and checks the result for truth
            self.add_line("if (!this->%s(%s)) {" % (function_name, ', '.join(args_tempvars)))
            self.indent += 1
            self.add_line("goto %s;" % exception_handler)
            self.indent -= 1
            self.add_line("}")
        else:
            # evaluate call_node.func as a Python expr
            new_ref_to_callable = self.visit(call_node.func, variable_name=temp_callable_name)
            self.add_line("if (%s == NULL) { goto %s; }" % (temp_callable_name, exception_handler))
            # now dispatch to it; NULL is the sentinel value to stop reading the arguments:
            packed_args = ', '.join(args_tempvars) + (', NULL' if args_tempvars else ' NULL')
            self.add_line("%s = PyObject_CallFunctionObjArgs(%s, %s);" %
                (result_name, temp_callable_name, packed_args))
            self.add_line("if (%s == NULL) { goto %s; }" % (result_name, exception_handler))
            if not variable_name:
                self._template_write(result_name, test_null=False)
                # dispose of the extra ref
                self.add_line("Py_DECREF(%s);" % (result_name,))

        # clean up our owned references to the arguments if appropriate
        # if this isn't a C function and write=True, the new reference to the resulting element
        # gets stolen by the list (PyList_Append); if write=False, it's the caller's responsibility
        # to clean it up
        for (arg, newref) in argname_and_newrefs:
            if newref:
                self.add_line("Py_DECREF(%s);" % arg)
        if not c_function and new_ref_to_callable:
            self.add_line("Py_DECREF(%s);" % temp_callable_name)

        self.add_line("if (0) {")
        self.indent += 1
        self.add_line("%s:" % exception_handler)
        for (argname, newref) in argname_and_newrefs:
            if newref:
                self.add_line("Py_XDECREF(%s);" % (argname,))
        if not c_function:
            self.add_line("Py_XDECREF(%s);" % temp_callable_name)
        self.add_line("goto %s;" % self.exception_handler_stack[-1])
        self.indent -= 1
        self.add_line("}")

        # close block scope
        self.add_line('}')

        # python convention is that function call always returns a new ref:
        if variable_name:
            return True

    def visit_Attribute(self, attribute_node, variable_name=None):
        """
        Attribute is the node for, e.g., foo.bar.
        """
        # in template mode, if we see an attribute, it's the beginning of a dotted path string
        # with nonstandard semantics:
        if self.compiler_settings.template_mode:
            return self.generate_path(attribute_node, variable_name=variable_name)

        assert variable_name, 'If not in template mode, we require a variable name to hold the result.'

        # block-scope this, because we require a temporary variable
        self.add_line('{');
        self.indent += 1

        attr_id = self.registry.register(attribute_node.attr)
        attribute_str_object = "%s[%d]" % (LITERALS_ARRAY_NAME, attr_id)

        unique_id = self.unique_id_counter.next()
        base_var = "temp_base_%d" % (unique_id,)
        self.add_line('PyObject *%s;' % (base_var,))
        new_ref = self.visit(attribute_node.value, variable_name=base_var)
        self.add_line('%s = PyObject_GetAttr(%s, %s);' % (variable_name, base_var, attribute_str_object))
        if new_ref:
            self.add_line('Py_DECREF(%s);' % (base_var,))
        self.add_line('if (!%s) { goto %s; }' % (variable_name, self.exception_handler_stack[-1]))

        self.indent -= 1
        self.add_line('}')

    def _template_write(self, cexpr, test_null=True):
        """Write a C expression to the transaction. This expression will
        be evaluated multiple times --- make sure it's a variable name
        or an array access, not a function call.

        Args:
            cexpr - C expression to write
            test_null - check the value for nullness first
        """
        # TODO throw an exception on NULL instead of silently skipping?
        if test_null:
            self.add_line("if (%s != NULL) {" % (cexpr))
            self.indent += 1

        self.add_line("PyList_Append(this->%s, %s);" % (TRANSACTION_NAME, cexpr))

        if test_null:
            self.add_line("}")
            self.indent -= 1

    def _make_tempvar(self):
        """Get a temporary variable with a unique name."""
        return "tempvar_%d" % (self.unique_id_counter.next(),)

    @contextmanager
    def block_scope(self):
        """Create a C++ block scope."""
        self.add_line("{")
        self.indent += 1
        yield
        self.indent -= 1
        self.add_line("}")

    def visit_Name(self, name_node, variable_name=None):
        """Resolve a name in isolation."""
        name = name_node.id

        resolved_from_display = False
        # all name resolution code will borrow the reference
        cexpr_for_name = self._local_resolve_name(name)
        if not cexpr_for_name:
            # TODO this logic should include builtins;
            # also we'll support some nonstandard builtins
            cexpr_for_name = self._import_resolve_name(name)

        # attempt to resolve the name as an import
        if not cexpr_for_name:
            cexpr_for_name = self.import_registry.resolve_import_path((name,))

        if not cexpr_for_name:
            if self.compiler_settings.template_mode:
                cexpr_for_name = self._display_resolve_name(name)
                resolved_from_display = True
            else:
                raise Exception("Not in template mode; can't resolve %s statically" % (name,))

        # if someone wanted this as a variable, return a borrowed ref,
        # unless we resolved it from display, in which case we need a new
        # ref in case we re-enter Python code and somehow destroy the object.
        if variable_name:
            self.add_line("%s = %s;" % (variable_name, cexpr_for_name))
            if resolved_from_display:
                # TODO here's where we need to set an exception for failed display lookup
                self.add_line("if (!%s) { goto %s; }" % (variable_name, self.exception_handler_stack[-1]))
                # TODO we should allow path lookup to borrow a reference to variables from display ---
                # otherwise path lookup does a back-to-back incref-decref on the base of the path,
                # which is rather inelegant.
                self.add_line("else { Py_INCREF(%s); }" % (variable_name,))
            return resolved_from_display
        # if they wanted it written, write it safely:
        elif resolved_from_display:
            # the accessor for resolving a name from display is a PyDict_GetItem,
            # and we don't want to make that call more than once.
            with self.block_scope():
                tempvar = self._make_tempvar()
                self.add_line("PyObject *%s = %s;" % (tempvar, cexpr_for_name))
                self._template_write(tempvar, test_null=True)
        else:
            self._template_write(cexpr_for_name, test_null=False)

    def _local_resolve_name(self, name):
        """Attempt to resolve a name as a native C++ local variable, i.e.,
        a method parameter or a for-loop temporary variable.
        """
        for namespace in reversed(self.namespaces):
            if name in namespace and namespace[name] == 'NATIVE':
                return name

        return None

    def _import_resolve_name(self, name):
        """Attempt to resolve a name (statically) as an import."""
        import_path = (name,)
        if import_path in self.import_registry.symbols_to_index:
            return '%s[%d]' % (IMPORT_ARRAY_NAME, self.import_registry.symbols_to_index[import_path])

        return None

    def _display_resolve_name(self, name):
        """Attempt to resolve a name from the display dictionary."""
        literal_id = self.registry.register(name)
        return "PyDict_GetItem(this->%s, %s[%d])" % (DISPLAY_NAME, LITERALS_ARRAY_NAME, literal_id)

    def generate_path(self, attribute_node, variable_name=None):
        """
        This does a spot of "arms-length recursion"; it follows
        all attribute accesses and makes them into a "path", i.e.,
        foo.bar.baz becomes the operation of applying the (baz, bat)
        path to foo.
        """
        path = []
        terminal_node = attribute_node
        while True:
            path.append(terminal_node.attr)
            terminal_node = terminal_node.value
            if not isinstance(terminal_node, _ast.Attribute):
                break
        path.reverse()
        path = tuple(path)

        with self.block_scope():
            unique_id = self.unique_id_counter.next()
            temp_base_var = "temp_var_base_%d" % (unique_id,)
            self.add_line("PyObject *%s;" % (temp_base_var,))
            if variable_name:
                result_var = variable_name
            else:
                result_var = "temp_var_base_%d" % (unique_id,)

            new_ref = self.visit(terminal_node, variable_name=temp_base_var)

            if self.compiler_settings.use_variadic_path_resolution:
                name_indices = [self.registry.register(name) for name in path]
                path_varargs = "".join(" ,%s[%d]" % (LITERALS_ARRAY_NAME, index) for index in name_indices)
                c_expr = "resolve_path(%s, %d %s)" % (temp_base_var, len(name_indices), path_varargs)
            else:
                path_fname = self.path_registry.register(path)
                c_expr = "%s(%s)" % (path_fname, temp_base_var)

            if new_ref:
                self.add_line("Py_DECREF(%s);" % (temp_base_var,))
            self.add_line("if (!(%s = %s)) { goto %s; }" % (result_var, c_expr, self.exception_handler_stack[-1]))

            if variable_name:
                # path lookup returns a new ref
                return True
            else:
                self._template_write(result_var, test_null=False)
                # dispose of the extra ref:
                self.add_line("Py_DECREF(%s);" % (result_var,))

    def visit_Import(self, import_node, variable_name=None):
        """e.g., "import os", "import os.path".
        XXX imports will have very confusing effects if you include them in code,
        rather than at top-level; they'll take effect in compile order and never
        go out of scope.
        """
        assert variable_name is None, 'Not an expression.'
        for alias_node in import_node.names:
            self.import_registry.register(alias_node.name, asname=alias_node.asname)

    def visit_ImportFrom(self, import_node, variable_name=None):
        """e.g., "from foo import bar", "from foo.bar import baz, bat"
        """
        assert variable_name is None, 'Not an expression.'
        assert import_node.level == 0, 'Explicit relative imports unsupported.'
        for alias_node in import_node.names:
            self.import_registry.register(alias_node.name, asname=alias_node.asname, module=import_node.module)

    def run(self, module_name, parsetree):
        """
        Compile parse tree and return generated code for a full module.

        Entry point for the compiler when compiling a single class without dependencies.
        """
        self.visit(parsetree)
        return generate_c_file(module_name, self.registry, self.path_registry, self.import_registry, [self])
