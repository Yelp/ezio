"""
Code to compile (restricted) Python ASTs to C++ and package the resulting code as
C extension modules.
"""
from __future__ import with_statement

import _ast
import copy
import itertools
import sys
from contextlib import contextmanager

from ezio.astutil.node_visitor import NodeVisitor


DISPLAY_NAME = "display"
TRANSACTION_NAME = "transaction"
LITERALS_ARRAY_NAME = "string_literals"
IMPORT_ARRAY_NAME = 'imported_names'
EXPRESSIONS_ARRAY_NAME = 'expressions'
EXPRESSIONS_EXCEPTION_HANDLER = 'HANDLE_EXCEPTIONS_EXPRESSIONS'
MAIN_FUNCTION_NAME = "respond"
TERMINAL_EXCEPTION_HANDLER = "REPORT_EXCEPTION_TO_PYTHON"

RESERVED_WORDS = set([DISPLAY_NAME, TRANSACTION_NAME, LITERALS_ARRAY_NAME, IMPORT_ARRAY_NAME, MAIN_FUNCTION_NAME])

# AST node classes to the corresponding operator ID used by PyObject_RichCompare:
CMPOP_TO_OPID = {
        _ast.Eq: 'Py_EQ',
        _ast.NotEq: 'Py_NE',
        _ast.Lt: 'Py_LT',
        _ast.LtE: 'Py_LE',
        _ast.Gt: 'Py_GT',
        _ast.GtE: 'Py_GE',
}

# Python built-in objects to their names in the C-API:
PYBUILTIN_TO_CEXPR = {
        'None': 'Py_None',
        'True': 'Py_True',
        'False': 'Py_False',
}

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


class LiteralRegistry(LineBufferMixin):
    """Encapsulates the creation of Python string/int/bool objects for templating.

    Some use cases: string literals in the template, integers used as constant arguments.
    """

    def __init__(self):
        super(LiteralRegistry, self).__init__()
        self.literals = []
        self.literal_key_to_index = {}

        self.dispatch_map = {
            'int': self.generate_int,
            'float': self.generate_float,
            'str': self.generate_str,
        }

    def _canonicalize_type(self, value):
        """Basically, distinguish 3 from 3.0 (since they're ==) by tagging them with int/float."""
        val_type = type(value)
        if val_type in (int, long):
            return 'int'
        elif val_type == float:
            return 'float'
        elif val_type in (str, unicode):
            return 'str'
        else:
            raise Exception("Can't create literals for type %r" % (val_type,))

    def _value_to_key(self, value):
        """Hashable key uniquely identifying a value, by including the type."""
        return (self._canonicalize_type(value), value)

    def generate_float(self, value):
        return "PyFloat_FromDouble(%r)" % (value,)

    def generate_int(self, value):
        # XXX assume that (sys.maxint at compile-time) == (sys.maxint at runtime)
        if value >= (-1*sys.maxint - 1) and value <= sys.maxint:
            return "PyInt_FromLong(%r)" % (value,)
        else:
            # just have it decode the base-10 representation
            return 'PyLong_FromString("%r", NULL, 10)' % (value,)

    def generate_str(self, value):
        # http://stackoverflow.com/questions/4000678/using-python-to-generate-a-c-string-literal-of-json
        formatted_value = '"' + value.replace('\\', r'\\').replace('"', r'\"').replace("\n", r'\n') + '"'
        if isinstance(formatted_value, str):
            return "PyString_FromString(%s)" % (formatted_value,)
        elif isinstance(formatted_value, unicode):
            return "PyUnicode_FromString(%s)" % (formatted_value.encode('utf-8'),)
        else:
            raise ValueError(type(formatted_value))

    def register(self, literal):
        """Intern `literal` and return its index in the intern table."""
        key = self._value_to_key(literal)
        index = self.literal_key_to_index.get(key)
        if index is not None:
            return index

        self.literals.append(literal)
        index = len(self.literals) - 1
        self.literal_key_to_index[key] = index
        return index

    def cexpr_for_index(self, index):
        """Return a C expression to access the literal at `index`."""
        return "%s[%d]" % (LITERALS_ARRAY_NAME, index)

    def dump(self):
        self.add_line("static PyObject *%s[%d];" % (LITERALS_ARRAY_NAME, len(self.literals)))
        self.add_line("static void init_string_literals(void) {")
        with self.increased_indent():
            for pos, literal in enumerate(self.literals):
                canonical_type = self._canonicalize_type(literal)
                cexpr_for_literal = self.dispatch_map[canonical_type](literal)
                self.add_line("%s[%d] = %s;" % (LITERALS_ARRAY_NAME, pos, cexpr_for_literal))
        self.add_line("}")
        return self.lines

class ExpressionRegistry(LineBufferMixin):
    """Encapsulates static creation of Python objects that cannot necessarily be uniqued,
    or created by means of a simple C expression; core use case is default arguments.
    """

    def __init__(self):
        super(ExpressionRegistry, self).__init__()
        self.num_objects = 0
        self.lvalue_to_resolver = {}

    def register(self):
        """Get an lvalue that you can assign the evaluation of a desired expression to.

        Note that this is different from other registries, in that the client has to
        generate and pass in its own expression-evaluating code.
        """
        result = "%s[%d]" % (EXPRESSIONS_ARRAY_NAME, self.num_objects)
        self.num_objects += 1
        return result

    def set_expression_resolver(self, lvalue, resolving_code):
        """Set C++ code that will perform assignment for a given lvalue."""
        self.lvalue_to_resolver[lvalue] = resolving_code

    def dump(self):
        self.add_line("static PyObject *%s[%d];" % \
            (EXPRESSIONS_ARRAY_NAME, len(self.lvalue_to_resolver)))
        self.add_line("static void init_expressions(void) {")
        with self.increased_indent():
            for resolver in self.lvalue_to_resolver.itervalues():
                self.add_line(resolver)
            # XXX this exception handler is a no-op; if an exception
            # has been set during the execution of the resolvers,
            # it'll get picked up as soon as we return from our
            # PyMODINIT_FUNC bootstrap. So this handler doesn't have
            # to do anything, it's just a convenience so the CodeGenerator
            # can always have an exception handler label available to it.
            self.add_line("%s:;" % (EXPRESSIONS_EXCEPTION_HANDLER,))
        self.add_line("}")
        return self.lines

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

def extract_params_with_defaults(args):
    num_required_args = len(args.args) - len(args.defaults)
    return args.args[num_required_args:]

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

    def add_method(self, method, params, defaults=()):
        assert method == MAIN_FUNCTION_NAME or method not in RESERVED_WORDS, 'Method name %s is a reserved word' % method
        assert method not in self.methods, 'Cannot double-define method %s for class %s' % (method, self.class_name)

        superclass_method = None
        if self.superclass_def is not None:
            superclass_method = self.superclass_def.get_method(method)
        if superclass_method is not None:
            assert len(superclass_method['params']) == len(params), 'Incompatible superclass definition'
            assert len(superclass_method['defaults']) == len(defaults), 'Incompatible superclass definition'
            superclass_method['virtual'] = True

        # OK, this is the first place in the hierarchy that this method has been defined:
        self.methods[method] = {
            'name': method,
            'params': params,
            'defaults': defaults,
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
    buf.add_line('init_expressions();')
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


def generate_c_file(module_name, literal_registry, path_registry, import_registry, expression_registry, compiled_classes):
    """Generate a complete C++ source file; string literals, path lookup functions,
    imports, all code for all classes, hooks, final segment.
    """
    all_lines = []

    all_lines += generate_initial_segment()
    all_lines += literal_registry.dump()
    all_lines += path_registry.dump() if path_registry is not None else []
    all_lines += import_registry.dump()
    all_lines += expression_registry.dump()

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

    def __init__(self, class_definition=None, literal_registry=None, path_registry=None,
            import_registry=None, compiler_settings=None, unique_id_counter=None,
            superclass_definition=None, expression_registry=None):
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
        self.expression_registry = ExpressionRegistry() if expression_registry is None else expression_registry

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

    def make_subgenerator(self):
        return CodeGenerator(class_definition=self.class_definition,
            literal_registry=self.registry, path_registry=self.path_registry,
            import_registry=self.import_registry, compiler_settings=copy.copy(self.compiler_settings),
            unique_id_counter=self.unique_id_counter, superclass_definition=self.superclass_definition,
            expression_registry=self.expression_registry)

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
        assert not variable_name, 'Classes are not expressions'
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
            default_names = frozenset(param.id for param in extract_params_with_defaults(function.args))
            # skip the definition of the main method, if this is a subclass:
            if function.name != MAIN_FUNCTION_NAME or write_toplevel_entities:
                self.class_definition.add_method(function.name, param_names, default_names)

        for stmt in class_node.body:
            assert isinstance(stmt, _ast.FunctionDef), 'Cannot compile non-method elements of classes.'
            # and skip the compilation of the main method as well if necessary, as above:
            if stmt.name != MAIN_FUNCTION_NAME or write_toplevel_entities:
                self.visit_FunctionDef(stmt, method=True)

    def visit_Expr(self, expr, variable_name=None):
        """Expr is a statement for a bare expression, wrapping the expression as .value."""
        return self.visit(expr.value, variable_name=variable_name)

    def visit_Str(self, str_node, variable_name=None):
        # value of a string literal is the member 's'
        return self._visit_literal(str_node.s, variable_name=variable_name)

    def visit_Num(self, num_node, variable_name=None):
        # value of a numeric literal is the member 'n'
        return self._visit_literal(num_node.n, variable_name=variable_name)

    def _visit_literal(self, value, variable_name=None):
        index = self.registry.register(value)
        literal_reference = self.registry.cexpr_for_index(index)
        if variable_name:
            # return a borrowed reference:
            self.add_line("%s = %s;" % (variable_name, literal_reference))
            return False
        elif self.compiler_settings.template_mode:
            self._template_write(literal_reference)

    def visit_FunctionDef(self, function_def, method=False):
        argslist_str, arg_namespace = self._generate_argslist_for_declaration(function_def.args, method=method)
        self.add_line("PyObject* %s::%s(%s) {" % (self.class_definition.class_name, function_def.name, argslist_str))
        self.indent += 1

        params_with_defaults = extract_params_with_defaults(function_def.args)
        for param, default_expr in zip(params_with_defaults, function_def.args.defaults):
            # compile code to generate the default value:
            subgenerator = self.make_subgenerator()
            subgenerator.compiler_settings.template_mode = False
            subgenerator.exception_handler_stack.append(EXPRESSIONS_EXCEPTION_HANDLER)
            expression_lvalue = self.expression_registry.register()
            subgenerator.visit(default_expr, variable_name=expression_lvalue)
            self.expression_registry.set_expression_resolver(expression_lvalue,
                '\n'.join(subgenerator.lines))
            self.add_line('if (%s == NULL) { %s = %s; }' % (param.id, param.id, expression_lvalue))

        # this would only be possible if function defs were nested:
        assert len(self.exception_handler_stack) == 0
        exception_handler = "HANDLE_EXCEPTIONS_%s_%d" % (function_def.name, self.unique_id_counter.next())
        self.exception_handler_stack.append(exception_handler)

        self.namespaces.append(arg_namespace)
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
        """Set up arguments for a native method declaration.

        Returns: comma_separated_argnames, a namespace dictionary for the arguments
        """
        # args has members "args", "defaults", "kwarg", "vararg";
        # the first of these is the positional parameters (including those with defaults),
        # "kwarg" and "vararg" refer to ** and * parameters respectively.
        assert not any ((args.kwarg, args.vararg)), "Kwargs/varargs currently unsupported"

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

    def _generate_argslist_for_invocation(self, call_node, c_method):
        """Generate C expressions and temporary variables for the arguments to an invocation.

        This helper function is applicable to native methods and Python calls without keywords,
        and it knows how to turn missing keyword arguments for a native method into NULLs.

        Returns: a list of pairs (var_name, new_ref), where var_name is the name of
            the argument (or 'NULL') and new_ref is whether a new reference was created for it
        """

        if c_method:
            remaining_params = list(c_method['params'])
            used_params = set()

            tempvars_and_exprs = []
            # apply the positional params to the function's params, in order:
            assert len(call_node.args) <= len(remaining_params), 'Too many arguments'
            for param, arg in zip(remaining_params, call_node.args):
                tempvars_and_exprs.append((self._make_tempvar(), arg))
                used_params.add(param)

            # leftover function params must be satisfied from the kwargs:
            remaining_params = remaining_params[len(call_node.args):]
            param_to_kwarg = dict((keyword.arg, keyword.value) for keyword in call_node.keywords)
            for param in remaining_params:
                kwarg_expr = param_to_kwarg.get(param)
                if kwarg_expr is None:
                    assert param in c_method['defaults'], 'Param %s has no default argument' % (param,)
                    tempvars_and_exprs.append(('NULL', None))
                else:
                    tempvars_and_exprs.append((self._make_tempvar(), kwarg_expr))
                    param_to_kwarg.pop(param)
                used_params.add(param)

            # all kwargs should have been consumed
            if param_to_kwarg:
                bad_param = param_to_kwarg.keys()[0]
                if bad_param in used_params:
                    raise Exception('Double-definition of param %s' % (bad_param,))
                else:
                    raise Exception('Undefined kwarg %s' % (bad_param,))
        else:
            assert not call_node.keywords # should be on the other code path
            # simple case where we just evaluate all the tempvars in order
            tempvars_and_exprs = [(self._make_tempvar(), arg) for arg in call_node.args]

        # generate names and declarations for the variables that will hold the invocation arguments
        args_tempvars = [tempvar for (tempvar, expr) in tempvars_and_exprs if expr is not None]
        self._declare_and_initialize(args_tempvars)

        # evaluate every argument; failures during evaluation will pass control
        # to self.exception_handler_stack[-1], i.e., the exception handler of
        # the visit_Call
        tempvars_and_newrefs = []
        for tempvar, expr in tempvars_and_exprs:
            if expr is not None:
                new_ref = self.visit(expr, variable_name=tempvar)
                tempvars_and_newrefs.append((tempvar, new_ref))
            else:
                # notify visit_Call that it should pass the NULL pointer here:
                tempvars_and_newrefs.append((tempvar, None))

        return tempvars_and_newrefs

    def _generate_argstuple_and_kwdict_for_invocation(self, call_node):
        """Helper to create the PyTuple/PyDict packing of arguments for PyObject_Call.

        For the tempvars_and_newrefs value that's returned, see _generate_argslist_for_invocation.
        """
        args_tempvars = [self._make_tempvar() for _ in call_node.args]
        kwargs_tempvars = [self._make_tempvar() for _ in call_node.keywords]
        arg_tuple_tempvar = self._make_tempvar()
        kwargs_dict_tempvar = self._make_tempvar()
        self._declare_and_initialize(args_tempvars + kwargs_tempvars + \
            [arg_tuple_tempvar, kwargs_dict_tempvar])

        tempvars_and_newrefs = []
        for tempvar, arg in zip(args_tempvars, call_node.args):
            new_ref = self.visit(arg, variable_name=tempvar)
            tempvars_and_newrefs.append((tempvar, new_ref))

        tempvar_to_keyword_idx = {}
        for tempvar, keyword in zip(kwargs_tempvars, call_node.keywords):
            new_ref = self.visit(keyword.value, variable_name=tempvar)
            tempvars_and_newrefs.append((tempvar, new_ref))
            tempvar_to_keyword_idx[tempvar] = self.registry.register(keyword.arg)

        packed_tempvars = '' if len(args_tempvars) == 0 else ' ,' + ', '.join(args_tempvars)
        self.add_line("%s = PyTuple_Pack(%d%s);" %
            (arg_tuple_tempvar, len(args_tempvars), packed_tempvars))
        self.add_line("if (!%s) { goto %s; }" %
                (arg_tuple_tempvar, self.exception_handler_stack[-1]))
        self.add_line("%s = PyDict_New();" % (kwargs_dict_tempvar,))
        self.add_line("if (!%s) { goto %s; }" % (kwargs_dict_tempvar, self.exception_handler_stack[-1]))
        for tempvar, keyword_idx in tempvar_to_keyword_idx.iteritems():
            self.add_line("if (PyDict_SetItem(%s, %s, %s) < 0) { goto %s; }" % (
                    kwargs_dict_tempvar,
                    self.registry.cexpr_for_index(keyword_idx),
                    tempvar,
                    self.exception_handler_stack[-1],
                ))

        return arg_tuple_tempvar, kwargs_dict_tempvar, tempvars_and_newrefs

    def _declare_and_initialize(self, varnames):
        """Declare a group of PyObject* variables and initialize them to NULL."""
        if varnames:
            declarations = ", ".join('*%s' % varname for varname in varnames)
            self.add_line("PyObject %s = NULL;" % (declarations,))

    def visit_Call(self, call_node, variable_name=None):
        """Compile a function invocation.

        It is known at compile time whether the function being called is native (i.e.,
        defined in a template and compiled to C++) or Python (i.e., a callable Python
        object from an import, a built-in, or the display dictionary). There are
        three distinct cases: native, Python with only positional args, and Python
        with keyword args.
        """
        # positional params are in call_node.args, all else is unsupported:
        assert not any((call_node.starargs, call_node.kwargs)), 'Unsupported feature'

        function_name = None
        c_method = None
        # a c function is a bare name that appears in the current class definition as a method:
        if isinstance(call_node.func, _ast.Name):
            function_name = call_node.func.id
            c_method = self.class_definition.get_method(function_name)

        if not c_method and call_node.keywords:
            return self._visit_Call_dynamic_kwargs(call_node, variable_name=variable_name)

        unique_id = self.unique_id_counter.next()
        exception_handler = "HANDLE_EXCEPTIONS_%d" % unique_id
        self.exception_handler_stack.append(exception_handler)
        # create block scope for temporary variables:
        self.add_line('{')

        # python callables have a callable object and a return value, create temp vars for these
        result_name = None
        if not c_method:
            temp_callable_name = "tempcallablevar_%d" % unique_id
            self.add_line('PyObject *%s;' % temp_callable_name)

            # were we given an externally scoped C variable to put the result in?
            if variable_name:
                result_name = variable_name
            else:
                result_name = "tempresultvar_%d" % unique_id
                self.add_line('PyObject *%s;' % result_name)

        argname_and_newrefs = self._generate_argslist_for_invocation(call_node, c_method)
        args_tempvars = [argname for argname, _ in argname_and_newrefs]

        if c_method:
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
                self._template_write(result_name)
                # dispose of the extra ref
                self.add_line("Py_DECREF(%s);" % (result_name,))

        # clean up our owned references to the arguments if appropriate
        # if this isn't a C function and write=True, the new reference to the resulting element
        # gets stolen by the list (PyList_Append); if write=False, it's the caller's responsibility
        # to clean it up
        for (arg, newref) in argname_and_newrefs:
            if newref:
                self.add_line("Py_DECREF(%s);" % arg)
        if not c_method and new_ref_to_callable:
            self.add_line("Py_DECREF(%s);" % temp_callable_name)

        self.exception_handler_stack.pop()

        self.add_line("if (0) {")
        self.indent += 1
        self.add_line("%s:" % exception_handler)
        for (argname, newref) in argname_and_newrefs:
            if newref:
                self.add_line("Py_XDECREF(%s);" % (argname,))
        if not c_method:
            self.add_line("Py_XDECREF(%s);" % temp_callable_name)
        self.add_line("goto %s;" % self.exception_handler_stack[-1])
        self.indent -= 1
        self.add_line("}")

        # close block scope
        self.add_line('}')

        # python convention is that function call always returns a new ref:
        if variable_name:
            return True

    def _visit_Call_dynamic_kwargs(self, call_node, variable_name=None):
        """Special case for Call, when a dictionary has to be allocated.

        This is only necessary when the callable is a Python object, not native code,
        and the call has keyword parameters. This code does exception handling a little
        differently; the exception handling label points to "cleanup" code that executes
        in both exceptional and non-exceptional cases, then the exception condition is
        tested again to see whether a jump to an underlying exception handler is required,
        or whether we can proceed.
        """
        with self.block_scope():
            unique_id = self.unique_id_counter.next()
            cleanup_label = "CLEANUP_%d" % unique_id
            self.exception_handler_stack.append(cleanup_label)
            temp_callable_name = "tempcallablevar_%d" % unique_id
            self.add_line('PyObject *%s = NULL;' % temp_callable_name)

            # were we given an externally scoped C variable to put the result in?
            if variable_name:
                result_name = variable_name
            else:
                result_name = "tempresultvar_%d" % unique_id
                self.add_line('PyObject *%s;' % result_name)

            argtuple, kwargdict, tempvars_and_newrefs = self._generate_argstuple_and_kwdict_for_invocation(call_node)

            new_ref_to_callable = self.visit(call_node.func, variable_name=temp_callable_name)
            self.add_line("%s = PyObject_Call(%s, %s, %s);" %
                (result_name, temp_callable_name, argtuple, kwargdict))
            self.add_line("if (%s == NULL) { goto %s; }" % (result_name, cleanup_label))
            if not variable_name:
                self._template_write(result_name)
                # dispose of the extra ref
                self.add_line("Py_DECREF(%s);" % (result_name,))

            self.exception_handler_stack.pop()
            self.add_line("%s:" % (cleanup_label,))
            self.add_line("Py_XDECREF(%s); Py_XDECREF(%s);" % (argtuple, kwargdict))
            if new_ref_to_callable:
                self.add_line("Py_XDECREF(%s);" % (temp_callable_name,))
            self.add_line(" ".join("Py_XDECREF(%s);" % (tempvar,)
                for tempvar, newref in tempvars_and_newrefs if newref))
            success_condition = " && ".join((argtuple, kwargdict, temp_callable_name, result_name))
            self.add_line("if (!(%s)) { goto %s; }" %
                (success_condition, self.exception_handler_stack[-1]))

        if variable_name:
            return True

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

        self.exception_handler_stack.append(outer_exception_handler)
        new_ref_created_for_iterable = self.visit(forloop.iter, variable_name=temp_sequence_name)
        self.add_line('if (!(%s = PySequence_Fast(%s, "Not a sequence."))) { goto %s; };' %
            (temp_fast_sequence_name, temp_sequence_name, outer_exception_handler))
        length_name = "temp_sequence_length_%d" % unique_id
        self.add_line("%s = PySequence_Fast_GET_SIZE(%s);" % (length_name, temp_fast_sequence_name))
        counter_name = 'counter_%d' % unique_id
        self.add_line("Py_ssize_t %s;" % counter_name)
        self.add_line("for(%(counter)s = 0; %(counter)s < %(length)s; %(counter)s++) {" %
            {'counter': counter_name, 'length': length_name})

        self.indent += 1
        self.add_line("PyObject *%s = PySequence_Fast_GET_ITEM(%s, %s);" %
            (var_name, temp_fast_sequence_name, counter_name))
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

        # remove the outer_exception_handler from the stack:
        self.exception_handler_stack.pop()

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
        self.add_line('{')
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

    def _template_write(self, cexpr):
        """Write a C expression to the transaction.

        Args:
            cexpr - C expression to write
            test_null - check the value for nullness first
        """
        if not self.compiler_settings.template_mode:
            return

        self.add_line("PyList_Append(this->%s, %s);" % (TRANSACTION_NAME, cexpr))

    def _make_tempvar(self, prefix=None):
        """Get a temporary variable with a unique name."""
        if prefix:
            prefix = "_%s" % (prefix,)
        else:
            prefix = ""
        return "tempvar%s_%d" % (prefix, self.unique_id_counter.next(),)

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

        # attempt resolution from a local variable
        cexpr_for_name = self._local_resolve_name(name)

        if not cexpr_for_name:
            # TODO this logic should include builtins;
            # also we'll support some nonstandard builtins
            cexpr_for_name = self._import_resolve_name(name)

        if not cexpr_for_name:
            cexpr_for_name = self._builtin_resolve_name(name)

        # attempt to resolve the name as an import
        if not cexpr_for_name:
            cexpr_for_name = self.import_registry.resolve_import_path((name,))

        if not cexpr_for_name:
            if self.compiler_settings.template_mode:
                return self._visit_Name_from_display(name_node, variable_name=variable_name)
            else:
                raise Exception("Not in template mode; can't resolve %s statically" % (name,))

        # all static resolutions return a borrowed ref:
        if variable_name:
            self.add_line("%s = %s;" % (variable_name, cexpr_for_name))
            return False
        else:
            self._template_write(cexpr_for_name)

    def _visit_Name_from_display(self, name_node, variable_name=None):
        """Special-cased visitor for when the name must be resolved from display."""
        name = name_node.id

        if variable_name:
            target = variable_name
        else:
            # require a block scope for the temporary
            target = self._make_tempvar()
            self.add_line("{")
            self.indent += 1
            self.add_line("PyObject *%s;" % (target,))

        literal_id = self.registry.register(name)
        self.add_line("%s = PyDict_GetItem(this->%s, %s[%d]);" %
            (target, DISPLAY_NAME, LITERALS_ARRAY_NAME, literal_id))

        self.add_line('if (!%s) { PyErr_SetString(PyExc_KeyError, "%s"); goto %s; }' %
           (target, name, self.exception_handler_stack[-1]))
        if variable_name:
            # return a new reference:
            self.add_line('Py_INCREF(%s);' % (variable_name,))
            return True
        else:
            self._template_write(target)
            self.indent -= 1
            self.add_line("}")

    def _local_resolve_name(self, name):
        """Attempt to resolve a name as a native C++ local variable, i.e.,
        a method parameter or a for-loop temporary variable.
        """
        for namespace in reversed(self.namespaces):
            if name in namespace and namespace[name] == 'NATIVE':
                return name

        return None

    def _builtin_resolve_name(self, name):
        if name in PYBUILTIN_TO_CEXPR:
            return PYBUILTIN_TO_CEXPR[name]
        # TODO support all builtins
        return None

    def _import_resolve_name(self, name):
        """Attempt to resolve a name (statically) as an import."""
        import_path = (name,)
        if import_path in self.import_registry.symbols_to_index:
            return '%s[%d]' % (IMPORT_ARRAY_NAME, self.import_registry.symbols_to_index[import_path])

        return None

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
                self._template_write(result_var)
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

    def visit_If(self, if_node, variable_name=None):
        """Compile an if statement."""
        assert not variable_name, 'If is not an expression'

        with self.block_scope():
            # hold the C boolean status of the conditional:
            conditional_tempvar = self._make_tempvar(prefix='conditional')
            self.add_line("int %s;" % (conditional_tempvar,))

            if isinstance(if_node.test, _ast.BoolOp):
                self.visit_BoolOp(if_node.test, variable_name=None, boolean_name=conditional_tempvar)
            else:
                conditional_expr = self._make_tempvar(prefix='conditional_expr')
                self.add_line("PyObject *%s;" % (conditional_expr,))
                new_ref = self.visit(if_node.test, variable_name=conditional_expr)
                self._truth_test(conditional_expr, conditional_tempvar, new_ref)
                # we don't need the Python object for the conditional anymore:
                if new_ref:
                    self.add_line("Py_DECREF(%s);" % (conditional_expr,))

            # now generate C++ if and else statements:
            self.add_line("if (%s) {" % (conditional_tempvar,))
            with self.increased_indent():
                for stmt in if_node.body:
                    self.visit(stmt)
            self.add_line("}")

            # note that the AST parser unpacks "elif quux:" into "else: if quux:":
            if if_node.orelse:
                self.add_line("else {")
                with self.increased_indent():
                    for stmt in if_node.orelse:
                        self.visit(stmt)
                self.add_line("}")

    def _truth_test(self, variable_target, boolean_target, new_ref):
        """Get the Python truth value of an object, with error checking."""
        cleanup_ref1 = "Py_DECREF(%s)" % (variable_target,) if new_ref else ""
        self.add_line("%s = PyObject_IsTrue(%s);" % (boolean_target, variable_target))
        self.add_line("if (%s == -1) { %s; goto %s; }" %
            (boolean_target, cleanup_ref1, self.exception_handler_stack[-1]))

    def visit_BoolOp(self, boolop_node, variable_name=None, boolean_name=None):
        """Compile logical 'and' and 'or'.

        There's a unique wrinkle here. The semantics of Python are such that if a
        is true and b is false, "a or b" will evaluate to a but "if a or b" will
        only evaluate the truth of a once --- it's not as simple as evaluating
        the 'and' expression and then testing the result again. Thus, we have to
        expose an additional interface, the boolean_name variable, which can hold
        the boolean (i.e., C int) truth value of the node as soon as it's known
        (in the above example, as soon as a has been tested).
        """
        op_is_or = isinstance(boolop_node.op, _ast.Or)

        self.add_line("{")

        if boolean_name is not None:
            boolean_target = boolean_name
        else:
            boolean_target = self._make_tempvar()
            self.add_line("int %s = -1;" % (boolean_target,))

        if variable_name is not None:
            variable_target = variable_name
        else:
            variable_target = self._make_tempvar()
            self._declare_and_initialize([variable_target])

        # TODO FIXME support "a and b and c"
        # in the meantime, a stupid workaround is ((a and b) and c)
        assert len(boolop_node.values) == 2, "For now, an and/or must have exactly 2 operands."
        value1, value2 = boolop_node.values

        if isinstance(value1, _ast.BoolOp):
            new_ref_1 = self.visit_BoolOp(value1, variable_name=variable_target,
                boolean_name=boolean_target)
        else:
            new_ref_1 = self.visit(value1, variable_name=variable_target)
            self._truth_test(variable_target, boolean_target, new_ref_1)

        # for an OR, we defer to the second value on falsehood, otherwise we defer to it on truth
        negation_required = "!" if op_is_or else ""
        self.add_line("if (%s%s) {" % (negation_required, boolean_target,))
        with self.increased_indent():
            if new_ref_1:
                self.add_line("Py_DECREF(%s);" % (variable_target,))

            # as of now, we don't own any references, so no need for a cleanup handler:
            if isinstance(value2, _ast.BoolOp):
                new_ref_2 = self.visit_BoolOp(value2, variable_name=variable_target,
                    boolean_name=boolean_target)
            else:
                new_ref_2 = self.visit(value2, variable_name=variable_target)
                # enforce identical reference creation status for both code paths:
                if new_ref_1 and not new_ref_2:
                    self.add_line("Py_INCREF(%s);" % (variable_target,))
                # evalute truth if the caller asked for it:
                if boolean_name:
                    self._truth_test(variable_target, boolean_target, new_ref_2)
        self.add_line("}")
        if new_ref_2 and not new_ref_1:
            # enforce identital reference creation status:
            self.add_line("else { Py_INCREF(%s); }" % (variable_target,))

        new_ref = new_ref_1 or new_ref_2
        if variable_name is None:
            if new_ref:
                self.add_line("Py_DECREF(%s);" % (variable_target))
                self.add_line("}")
        else:
            self.add_line("}")
            return new_ref

    def visit_Compare(self, compare_node, variable_name=None):
        """Compile all the binary comparisons, e.g., <=, is, not in."""
        assert len(compare_node.ops) == 1, 'Multiple comparisons in the same expression are unsupported.'
        op_node = compare_node.ops[0]
        # see if we're doing a rich comparison:
        op_id = CMPOP_TO_OPID.get(type(op_node))
        # or an is:
        is_or_is_not = type(op_node) in (_ast.Is, _ast.IsNot)
        # or a containment:
        in_or_not_in = type(op_node) in (_ast.In, _ast.NotIn)

        with self.block_scope():
            value1, value2 = self._make_tempvar(), self._make_tempvar()
            self._declare_and_initialize((value1, value2))
            if in_or_not_in:
                contains_status = self._make_tempvar()
                self.add_line("int %s = -1;" % (contains_status,))
            if variable_name is not None:
                target = variable_name
            else:
                target = self._make_tempvar()
                self.add_line("PyObject *%s;" % (target,))
            self.add_line("%s = NULL;" % (target,))
            cleanup_label = 'CLEANUP_%d' % (self.unique_id_counter.next(),)

            self.exception_handler_stack.append(cleanup_label)
            newref1 = self.visit(compare_node.left, variable_name=value1)
            newref2 = self.visit(compare_node.comparators[0], variable_name=value2)
            self.exception_handler_stack.pop()

            new_ref_to_target = False
            if op_id:
                # rich comparison returns a new reference
                self.add_line("%s = PyObject_RichCompare(%s, %s, %s);" %
                    (target, value1, value2, op_id))
                new_ref_to_target = True
            elif is_or_is_not:
                # generate a simple pointer comparison
                # if we wanted this to be fast, we could entangle it
                # with the conditional statements and report the
                # result of the comparison directly, rather than going
                # through Py_True and Py_False
                operator = "==" if isinstance(op_node, _ast.Is) else "!="
                self.add_line("%s = (%s %s %s) ? Py_True : Py_False;" %
                    (target, value1, operator, value2))
            elif in_or_not_in:
                self.add_line("%s = PySequence_Contains(%s, %s);" %
                    (contains_status, value2, value1))
                operator = "" if isinstance(op_node, _ast.In) else "!"
                self.add_line("if (%s == -1) { goto %s; }" % (contains_status, cleanup_label))
                self.add_line("%s = (%s%s) ? Py_True : Py_False;" %
                    (target, operator, contains_status))

            self.add_line("%s:" % (cleanup_label,))
            # we don't need the comparison operands:
            for val, newref in ((value1, newref1), (value2, newref2)):
                if newref:
                    self.add_line("Py_XDECREF(%s);" % (val,))
            self.add_line("if (!(%s && %s && %s)) { Py_XDECREF(%s); goto %s; }" %
                (value1, value2, target, target, self.exception_handler_stack[-1]))

            if not variable_name and new_ref_to_target:
                self.add_line("Py_DECREF(%s);" % (target,))
            else:
                return new_ref_to_target

    def visit_UnaryOp(self, unary_op_node, variable_name=None):
        """Compile a unary operation."""
        assert isinstance(unary_op_node.op, _ast.Not), 'Right now, "not" is the only supported unary operator.'

        with self.block_scope():
            boolean_target = self._make_tempvar()
            self.add_line("int %s = -1;" % (boolean_target,))
            if variable_name is not None:
                variable_target = variable_name
            else:
                variable_target = self._make_tempvar()
                self._declare_and_initialize([variable_target])

            new_ref = self.visit(unary_op_node.operand, variable_name=variable_target)
            self._truth_test(variable_target, boolean_target, new_ref)
            # we don't need the operand:
            if new_ref:
                self.add_line("Py_DECREF(%s);" % (variable_target,))
            # negate the value of boolean_target:
            self.add_line("%s = (%s) ? Py_False : Py_True;" % (variable_target, boolean_target,))
            if variable_name:
                # return a borrowed ref to Py_True or Py_False:
                return False

    def run(self, module_name, parsetree):
        """
        Compile parse tree and return generated code for a full module.

        Entry point for the compiler when compiling a single class without dependencies.
        """
        self.visit(parsetree)
        return generate_c_file(module_name, self.registry, self.path_registry,
                self.import_registry, self.expression_registry, [self])
