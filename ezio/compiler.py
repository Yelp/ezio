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
from ezio.constants import CURRENT_METHOD_TAG


DISPLAY_NAME = "display"
TRANSACTION_NAME = "transaction"
LITERALS_ARRAY_NAME = "string_literals"
IMPORT_ARRAY_NAME = 'imported_names'
EXPRESSIONS_ARRAY_NAME = 'expressions'
EXPRESSIONS_EXCEPTION_HANDLER = 'HANDLE_EXCEPTIONS_EXPRESSIONS'
MAIN_FUNCTION_NAME = "respond"
TERMINAL_EXCEPTION_HANDLER = "REPORT_EXCEPTION_TO_PYTHON"
# put all the template classes in this namespace,
# so they don't conflict with C names from Python.h:
CPP_NAMESPACE = "ezio_templates"
BASE_TEMPLATE_NAME = 'ezio_base_template'

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

UNARYOP_TO_CAPI = {
        _ast.Invert: 'PyNumber_Invert',
        _ast.USub: 'PyNumber_Negative',
        _ast.UAdd: 'PyNumber_Positive',
        _ast.Not: 'unary_not',
}

BINARYOP_TO_CAPI = {
        _ast.Add: 'PyNumber_Add',
        _ast.Sub: 'PyNumber_Subtract',
        _ast.Mult: 'PyNumber_Multiply',
        _ast.Div: 'PyNumber_Divide',
        _ast.Mod: 'PyNumber_Remainder',
        _ast.Pow: 'PyNumber_Power',
        _ast.LShift: 'PyNumber_Lshift',
        _ast.RShift: 'PyNumber_Rshift',
        _ast.BitOr: 'PyNumber_Or',
        _ast.BitAnd: 'PyNumber_And',
        _ast.BitXor: 'PyNumber_Xor',
        _ast.FloorDiv: 'PyNumber_FloorDivide',
}

# Python built-in objects to their names in the C-API:
PYBUILTIN_TO_CEXPR = {
        'None': 'Py_None',
        'True': 'Py_True',
        'False': 'Py_False',
}

class EZIOUnsupportedException(Exception):
    """Exception type for attempts to use unsupported features of EZIO."""
    pass

def assert_supported(condition, message=None):
    """Assert-like convenience for raising EZIOUnsupportedException."""
    if not condition:
        exception_args = (message,) if message is not None else ()
        raise EZIOUnsupportedException(*exception_args)

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

    def add_fixup(self, fixup):
        """Support the use of other LineBufferMixins as compiler fixups (in a broad sense). """
        assert isinstance(fixup, LineBufferMixin), 'Fixup must be a LineBufferMixin.'
        self.lines.append(fixup)

    @contextmanager
    def increased_indent(self, increment=1):
        assert increment > 0
        self.indent += increment
        yield
        self.indent -= increment

    def finalize(self):
        """Allow the class to dump lines (from internal state) that were not explicitly added.

        ...yeah, that made no sense. See the use cases :-|
        """
        pass

    def get_lines(self):
        """Return a list of all lines in the buffer, recursively descending into fixups."""
        self.finalize()

        result = []
        for line in self.lines:
            if isinstance(line, LineBufferMixin):
                result.extend(line.get_lines())
            elif isinstance(line, basestring):
                result.append(line)
            else:
                raise ValueError(line)
        return result

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
        # TODO: add a compiler setting to make all literals unicode --- otherwise unicode-only
        # filtering/escaping will have to convert the literals to unicode at join time
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

    def finalize(self):
        self.add_line("static PyObject *%s[%d];" % (LITERALS_ARRAY_NAME, len(self.literals)))
        self.add_line("static void init_string_literals(void) {")
        with self.increased_indent():
            for pos, literal in enumerate(self.literals):
                canonical_type = self._canonicalize_type(literal)
                cexpr_for_literal = self.dispatch_map[canonical_type](literal)
                self.add_line("%s[%d] = %s;" % (LITERALS_ARRAY_NAME, pos, cexpr_for_literal))
        self.add_line("}")

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

    def finalize(self):
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

class ImportRegistry(LineBufferMixin):
    """Encapsulates tracking of imported names, and the code to perform the imports."""

    def __init__(self):
        super(ImportRegistry, self).__init__()
        # list of string of C-API code for executing the imports
        self.imports = []
        self.symbols_to_index = {}
        self.num_objects = 0

    def _get_array_accessor(self, index):
        return '%s[%d]' % (IMPORT_ARRAY_NAME, index)

    def make_space(self):
        """Allocate a place in the import array."""
        index = self.num_objects
        self.num_objects += 1
        return index, self._get_array_accessor(index)

    def register_import(self, module, asname=None):
        """Register a normal import statement, e.g., `import a.b.c`, `import a.b.c. as d`."""
        index, accessor = self.make_space()
        if not asname:
            self.imports.append('%s = PyImport_ImportModuleEx("%s", NULL, NULL, NULL);'
                    % (accessor, module))
            self.imports.append('if (!%s) return;' % (accessor,))
            # bind the first element of the path
            # TODO we could "cheat" and bind every subpath
            resolvable_path = module.split('.')[0]
            self.symbols_to_index[(resolvable_path,)] = index
        else:
            # take a shortcut and just jump to the end of the path
            # (this corresponds to the trick of __import__(module); sys.modules[module] )
            self.imports.append('%s = PyImport_ImportModule("%s");' % (accessor, module))
            self.imports.append('if (!%s) return;' % (accessor,))
            # bind the as-name:
            self.symbols_to_index[(asname,)] = index

    def register_fromimport(self, module, fromlist, aslist):
        """Register a from-import statement, e.g., `from a.b import c, d as e`."""
        _index, accessor = self.make_space()

        packed_fromlist = ', '.join(['PyString_FromString("%s")' % (item,) for item in fromlist])
        tupled_fromlist = 'PyTuple_Pack(%d, %s)' % (len(fromlist), packed_fromlist)
        self.imports.append('%s = PyImport_ImportModuleEx("%s", NULL, NULL, %s);' %
                (accessor, module, tupled_fromlist))

        for fromname, asname in zip(fromlist, aslist):
            from_index, from_accessor = self.make_space()
            self.imports.append('%s = PyObject_GetAttr(%s, PyString_FromString("%s"));' %
                    (from_accessor, accessor, fromname))
            self.imports.append('if (!%s) return;' % (from_accessor,))
            self.symbols_to_index[(asname or fromname,)] = from_index

    def resolve_import_path(self, path):
        """Produces the C-language expression corresponding to an import path, or None."""
        index = self.symbols_to_index.get(path)
        if index is None:
            return None
        return self._get_array_accessor(index)

    def finalize(self):
        # TODO: import failures are hidden and silent
        if self.num_objects:
            self.lines.append('static PyObject *%s[%d];' % (IMPORT_ARRAY_NAME, self.num_objects))

        self.lines.append('static void init_imports(void) {')
        with self.increased_indent():
            for import_line in self.imports:
                self.add_line(import_line)
        self.add_line('}')


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
        # (self.finalize() won't be called until after the literal registry has been dumped)
        for subpath_item in subpath:
            self.literal_registry.register(subpath_item)
        return self.subpath_to_fname[subpath]

    def finalize(self):
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

    This implementation relies on all classes being compiled at the same time,
    and in the correct order.
    """

    def get_method(self, method_name):
        """Recursively walk up the inheritance chain to get the method definition."""
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

        assert class_name != BASE_TEMPLATE_NAME, '%s is reserved for the base template.' \
                % (BASE_TEMPLATE_NAME,)

        self.class_name, self.superclass_def = class_name, superclass_def

        if superclass_def is not None:
            superclass_name = superclass_def.class_name
        else:
            superclass_name = BASE_TEMPLATE_NAME

        self.add_line('class %s : public %s {' % (class_name, superclass_name))
        self.indent += 1
        # all methods and members are C++-public:
        self.add_line('public:')

        # generate a call to the superclass constructor
        constructor_args = "PyObject *display, PyObject *transaction, PyObject *self_ptr"
        superclass_constructor_call = "%s(display, transaction, self_ptr)" % (superclass_name,)
        self.add_line('%s (%s) : %s {}' % (self.class_name, constructor_args,
            superclass_constructor_call,))
        # no destructor, this thing doesn't own any dynamically allocated memory
        # (really it only exists because we need its vtable)

        self.methods = {}

    def finalize(self):
        for method in self.methods.itervalues():
            method_definition = 'virtual PyObject *' if method['virtual'] else 'PyObject *'
            method_definition += method['name']
            args_definition = '(' + ', '.join('PyObject *%s' % param for param in method['params']) + ');'
            self.add_line(method_definition + args_definition)

        self.indent -= 1
        self.add_line('};')


def generate_initial_segment():
    buf = LineBufferMixin()
    buf.add_line('#include "Python.h"')
    buf.add_line('#include "Ezio.h"')
    buf.add_line()
    return buf


def generate_final_segment(module_name, function_names):
    """Generate the final segment of the C++ file, which contains
    the module initialization code.
    """
    buf = LineBufferMixin()

    buf.add_line("static PyMethodDef k_module_methods[] = {")
    buf.indent += 1
    for function_name in function_names:
        buf.add_line('{"%s", (PyCFunction)%s::%s, METH_VARARGS, "Perform templating for %s"},' %
            (function_name, CPP_NAMESPACE, function_name, function_name))
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

    return buf


def generate_hook(function_name, class_name, public=True):
    """Generate the static "hook" function that unpacks the Python arguments,
    dispatches to the C++ code, then returns the result to Python.

    Args:
        public - if False, generate the "old-style" hook that takes in a
                 transaction list as second argument, then defers string join
                 to the caller
    """
    buf = LineBufferMixin()
    buf.add_line('static PyObject *%s(PyObject *self, PyObject *args) {' % (function_name,))
    buf.indent += 1

    buf.add_line('PyObject *display, *transaction, *self_ptr;')
    # TODO type-check list and dict here
    if public:
        unpack = '"OO", &display, &self_ptr'
    else:
        unpack = '"OOO", &display, &transaction, &self_ptr'
    buf.add_line('if (!PyArg_ParseTuple(args, %s)) { return NULL; }' % (unpack,))
    if public:
        # create a new list for the transaction
        buf.add_line('if (!(transaction = PyList_New(0))) { return NULL; }')

    buf.add_line('if (self_ptr == Py_None) { self_ptr = NULL; }')
    buf.add_line('%s::%s template_obj(display, transaction, self_ptr);' % (CPP_NAMESPACE, class_name,))
    buf.add_line('PyObject *status = template_obj.%s();' % (MAIN_FUNCTION_NAME,))
    buf.add_line('if (status) {')
    with buf.increased_indent():
        if public:
            buf.add_line('PyObject *result = ezio_concatenate(transaction);')
            buf.add_line('Py_DECREF(transaction);')
            # this wil propagate exceptions during concatenation:
            buf.add_line('return result;')
        else:
            # just return the status value
            buf.add_line('Py_INCREF(status); return status;')
    buf.add_line('}')
    # exit path for when templating encountered an exception
    if public:
        buf.add_line('Py_DECREF(transaction);')
    buf.add_line('return NULL;')

    buf.indent -= 1
    buf.add_line('}')

    return buf


def generate_c_file(module_name, literal_registry, path_registry, import_registry, expression_registry, compiled_classes):
    """Generate a complete C++ source file; string literals, path lookup functions,
    imports, all code for all classes, hooks, final segment.
    """
    cpp_file = LineBufferMixin()
    cpp_file.add_fixup(generate_initial_segment())
    cpp_file.add_fixup(literal_registry)
    if path_registry is not None:
        cpp_file.add_fixup(path_registry)
    cpp_file.add_fixup(import_registry)
    cpp_file.add_fixup(expression_registry)

    # concatenate all class definitions and their method definitions,
    # enclosing them in a C++ namespace:
    hook_names = []
    cpp_file.add_line("namespace %s {" % (CPP_NAMESPACE,))
    for compiled_class in compiled_classes:
        # add the class definition:
        cpp_file.add_fixup(compiled_class.class_definition)
        # and the code for the defined methods:
        cpp_file.add_fixup(compiled_class)

        class_name = compiled_class.class_definition.class_name
        hook_name = "%s_%s" % (class_name, MAIN_FUNCTION_NAME)
        cpp_file.add_fixup(generate_hook(hook_name, class_name, public=True))
        hook_names.append(hook_name)
    cpp_file.add_line("}")

    cpp_file.add_fixup(generate_final_segment(module_name, hook_names))

    return '\n'.join(cpp_file.get_lines())

class NameStatus(object):
    """Encapsulates the status of a name we have compile-time information about."""

    def __init__(self, accessor='NATIVE', scope='ARGUMENT', null=False, owned_ref=False):
        # how do we access this name? e.g., 'NATIVE', 'SELF'
        self.accessor = accessor
        # what's the C scope of this name? only really relevant for 'NATIVE'
        self.scope = scope
        # does this require a NULL test before use?
        self.null = null
        # do we own a reference to this name?
        self.owned_ref = owned_ref

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
    to fall all the way down the call stack and back into Python. Every code generation
    method is responsible for generating its own error handling, i.e., if you do:

        self.visit(subexpression, variable_name=tempvar)

    you don't have to perform a NULL test on `tempvar` afterwards, since that's
    the responsibility of the visitor you invoked.
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

        # our reimplementation of VFSSL:
        # static lookup among imported names, function arguments,
        # and the variable names in for loops, in REVERSE order
        # fail over to dynamic lookup in the display dict
        self.namespaces = []

        # this is a stack of breadcrumbs to follow (with goto) when encountering an exception;
        # you fall all the way down the stack, cleaning up stray references as you go,
        # until finally you return a null pointer back to the Python calling code
        self.exception_handler_stack = []

    @contextmanager
    def additional_namespace(self, namespace):
        """Contextmanager to push-pop a namespace."""
        self.namespaces.append(namespace)
        yield
        self.namespaces.pop()

    @contextmanager
    def additional_exception_handler(self, exception_handler):
        """Contextmanager to push-pop an exception handler."""
        self.exception_handler_stack.append(exception_handler)
        yield
        self.exception_handler_stack.pop()

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
        self.function_def_name = function_def.name
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
                '\n'.join(subgenerator.get_lines()))
            self.add_line('if (%s == NULL) { %s = %s; }' % (param.id, param.id, expression_lvalue))

        # add the fixup that declares all variables that are lvalues to #set statements,
        # and the namespace to resolve them. This more or less mimics Python local variable
        # semantics; a local variable is scoped to its enclosing function, not to any smaller
        # block.
        self.assignment_targets = LineBufferMixin()
        self.assignment_cleanup = LineBufferMixin()
        self.assignment_namespace = {}
        self.add_fixup(self.assignment_targets)
        self.namespaces.append(self.assignment_namespace)

        # this would only be possible if function defs were nested:
        assert len(self.exception_handler_stack) == 0
        exception_handler = "HANDLE_EXCEPTIONS_%s_%d" % (function_def.name, self.unique_id_counter.next())
        self.exception_handler_stack.append(exception_handler)

        self.namespaces.append(arg_namespace)
        for stmt in function_def.body:
            self.visit(stmt)
        # insert the fixup to clean up assignments
        self.add_fixup(self.assignment_cleanup)
        # XXX Py_None is being used as a C-truthy sentinel for success
        self.add_line("return Py_None;")
        self.add_line('%s:' % exception_handler)
        # insert the cleanup fixup *again*
        self.add_fixup(self.assignment_cleanup)
        self.add_line("return NULL;")
        self.indent -= 1
        # remove the argument and assignment namespaces:
        self.namespaces.pop()
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

        namespace = {}
        for argname in positional_args:
            namespace[argname] = NameStatus(accessor='NATIVE', scope='ARGUMENT',
                    null=False, owned_ref=False)
        # this name can be resolved to this->self_ptr (with a NULL test);
        # in specialized contexts, attribute accesses on it can be resolved to native code
        if self_arg is not None:
            namespace[self_arg] = NameStatus(accessor='SELF')
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
        func_node = call_node.func
        # a c function is a bare name that appears in the current class definition as a method:
        if isinstance(func_node, _ast.Name):
            function_name = func_node.id
            c_method = self.class_definition.get_method(function_name)
        elif isinstance(func_node, _ast.Attribute):
            value_node = func_node.value
            # or else a reference to self.asdf where 'asdf' appears in the class definition as a method:
            if isinstance(value_node, _ast.Name):
                name_status = self._get_name_status(func_node.value.id)
                if name_status and name_status.accessor == 'SELF':
                    function_name = func_node.attr
                    c_method = self.class_definition.get_method(function_name)
            # or else super(X, self).y():
            # XXX this ignores the case of dispatch to a class other than the immediate superclass,
            # but that's all the functionality Cheetah's #super directive provides anyway.
            elif (isinstance(value_node, _ast.Call) and isinstance(value_node.func, _ast.Name)
                    and value_node.func.id == 'super' and self._get_name_status('super') is None):
                assert_supported(self.compiler_settings.template_mode,
                        'super() not fully implemented')
                # function we're trying to call is the `y` in super(X, self).y():
                superclass_function_name = func_node.attr
                if superclass_function_name == CURRENT_METHOD_TAG:
                    superclass_function_name = self.function_def_name
                c_method = self.superclass_definition.get_method(superclass_function_name)
                assert c_method, 'No superclass method available.'
                function_name = '%s::%s' % \
                        (self.superclass_definition.class_name, superclass_function_name)

        if not c_method and call_node.keywords:
            return self._visit_Call_dynamic_kwargs(call_node, variable_name=variable_name)

        unique_id = self.unique_id_counter.next()
        exception_handler = "HANDLE_EXCEPTIONS_%d" % unique_id
        self.exception_handler_stack.append(exception_handler)
        # create block scope for temporary variables:
        self.add_line('{')
        self.indent += 1

        # python callables have a callable object and a return value, create temp vars for these
        result_name = None
        if not c_method:
            temp_callable_name = "tempcallablevar_%d" % unique_id
            self.add_line('PyObject *%s = NULL;' % temp_callable_name)

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
        if not c_method and new_ref_to_callable:
            self.add_line("Py_XDECREF(%s);" % temp_callable_name)
        self.add_line("goto %s;" % self.exception_handler_stack[-1])
        self.indent -= 1
        self.add_line("}")

        # close block scope
        self.indent -= 1
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
                self.add_line('%s = NULL;' % (result_name,))
            else:
                result_name = "tempresultvar_%d" % unique_id
                self._declare_and_initialize([result_name])

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
            # fail if we did not successfully compute the final result
            # (i.e., failed to evaluate the arguments, retrieve the callable, or call it)
            self.add_line("if (!%s) { goto %s; }" %
                (result_name, self.exception_handler_stack[-1]))

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

        inner_namespace = {
            var_name: NameStatus(accessor='NATIVE', scope='LOCAL', null=False, owned_ref=True)
        }

        # compile the body of the for loop
        with self.additional_namespace(inner_namespace):
            with self.additional_exception_handler(inner_exception_handler):
                for stmt in forloop.body:
                    self.visit(stmt)

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

    def _template_write(self, cexpr, newref=False):
        """Write a C expression to the transaction.

        Args:
            cexpr - C expression to write
            newref - remove the new reference that was created
        """
        if not self.compiler_settings.template_mode:
            return

        self.add_line("PyList_Append(this->%s, %s);" % (TRANSACTION_NAME, cexpr))
        if newref:
            self.add_line('Py_DECREF(%s);' % (cexpr,))

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
        name_status = self._get_name_status(name)
        if name_status is None:
            return None

        if name_status.accessor == 'NATIVE' and not name_status.null:
            return name

        if name_status.accessor == 'NATIVE' and name_status.null:
            # generate a NameError for uninitialized use of a #set variable:
            self.add_line('if (!%s) { PyErr_SetString(PyExc_NameError, "%s"); goto %s; }' %
                (name, name, self.exception_handler_stack[-1]))
            return name

        if name_status.accessor == 'SELF':
            # generate a NameError if we have no wrapped object:
            error_msg = 'No wrapped object to resolve the self-name %s' % (name,)
            self.add_line('if (!this->self_ptr) { PyErr_SetString(PyExc_NameError, "%s"); goto %s; }' %
                (error_msg, self.exception_handler_stack[-1]))
            return "this->self_ptr"

        raise Exception("Can't process name %s with unsupported status %s" % (name, name_status,))

    def _get_name_status(self, name):
        """Gets the NameStatus object for a name we have compile-time information about.
        """
        for namespace in reversed(self.namespaces):
            if name in namespace:
                return namespace[name]

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
                # write and dispose of the extra ref
                self._template_write(result_var, newref=True)

    def visit_Import(self, import_node, variable_name=None):
        """e.g., "import os", "import os.path".
        XXX imports will have very confusing effects if you include them in code,
        rather than at top-level; they'll take effect in compile order and never
        go out of scope.
        """
        assert variable_name is None, 'Not an expression.'
        for alias_node in import_node.names:
            self.import_registry.register_import(alias_node.name, asname=alias_node.asname)

    def visit_ImportFrom(self, import_node, variable_name=None):
        """e.g., "from foo import bar", "from foo.bar import baz, bat"
        """
        assert variable_name is None, 'Not an expression.'
        assert import_node.level == 0, 'Explicit relative imports unsupported.'

        fromnames = [alias.name for alias in import_node.names]
        asnames = [alias.asname for alias in import_node.names]
        self.import_registry.register_fromimport(import_node.module, fromnames, asnames)

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

    def visit_IfExp(self, if_node, variable_name=None):
        """Compile a if expression, i.e., `expr if test else otherexpr`.

        Regrettably largely copied and pasted from visit_If.
        """
        with self.block_scope():
            if not variable_name:
                target = self._make_tempvar()
                self.add_line('PyObject *%s;' % (target,))
            else:
                target = variable_name

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

            incref_fixup_1 = LineBufferMixin(initial_indent=self.indent)
            incref_fixup_2 = LineBufferMixin(initial_indent=self.indent)
            target_incref = 'Py_INCREF(%s);' % (target,)
            # now generate C++ if and else statements:
            self.add_line("if (%s) {" % (conditional_tempvar,))
            with self.increased_indent():
                newref_1 = self.visit(if_node.body, variable_name=target)
                self.add_fixup(incref_fixup_1)
            self.add_line("}")
            self.add_line("else {")
            with self.increased_indent():
                newref_2 = self.visit(if_node.orelse, variable_name=target)
                self.add_fixup(incref_fixup_2)
            self.add_line("}")

            # if one of the branches creates a new reference and the other doesn't,
            # make the other one do an INCREF, so the reference creation is the same
            # on all code paths.
            if newref_1 and not newref_2:
                incref_fixup_2.add_line(target_incref)
            elif newref_2 and not newref_1:
                incref_fixup_1.add_line(target_incref)

            newref = newref_1 or newref_2
            if not variable_name:
                self._template_write(target, newref=newref)
            else:
                return newref

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
                self.add_line("%s = NULL;" % (target,))
            else:
                target = self._make_tempvar()
                self._declare_and_initialize([target])
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
            # fail if we did not successfully populate `target`:
            self.add_line("if (!%s) { goto %s; }" % (target, self.exception_handler_stack[-1]))

            if not variable_name and new_ref_to_target:
                self.add_line("Py_DECREF(%s);" % (target,))
            else:
                return new_ref_to_target

    def visit_UnaryOp(self, unary_op_node, variable_name=None):
        """Compile a unary operation."""
        unary_not = isinstance(unary_op_node.op, _ast.Not)
        capi_call = UNARYOP_TO_CAPI.get(type(unary_op_node.op))

        with self.block_scope():
            if variable_name is not None:
                variable_target = variable_name
            else:
                variable_target = self._make_tempvar()
                self.add_line('PyObject *%s;' % (variable_target,))

            operand_tempvar = self._make_tempvar()
            self.add_line('PyObject *%s;' % (operand_tempvar,))
            # compute the operand
            operand_newref = self.visit(unary_op_node.operand, variable_name=operand_tempvar)
            # now apply the unary op
            self.add_line('%s = %s(%s);' % (variable_target, capi_call, operand_tempvar))
            # if we have a new ref to the operand, get rid of it now
            if operand_newref:
                self.add_line('Py_DECREF(%s);' % (operand_tempvar,))
            # test for failure of the unary operation:
            self.add_line('if (!%s) { goto %s; }' %
                    (variable_target, self.exception_handler_stack[-1]))

            # our unary_not implementation returns a borrowed reference;
            # all the others return new references
            newref = not unary_not
            if variable_name:
                return newref
            else:
                self._template_write(variable_target, newref=newref)

    def visit_BinOp(self, binary_op_node, variable_name=None):
        """Compile a binary operation."""
        capi_call = BINARYOP_TO_CAPI.get(type(binary_op_node.op))
        self._visit_binary_operation(capi_call, binary_op_node.left, binary_op_node.right,
                variable_name=variable_name)

    def visit_Assign(self, assignment_node, variable_name=None):
        """Compile a #set statement, in its form as a Python assignment."""
        assert not variable_name, 'Assignments are not expressions.'
        assert len(assignment_node.targets) == 1, 'Tuple unpacking is unsupported.'
        target = assignment_node.targets[0]
        assert isinstance(target, _ast.Name), 'Assign to non-names is unsupported.'
        target_name = target.id

        name_status = self._get_name_status(target_name)

        if name_status is None:
            # this name can be NULL and requires a test before use:
            target_name_status = NameStatus(accessor='NATIVE', scope='FUNCTION', owned_ref=True,
                    null=True)
            # add a declaration of the pointer to the (C function-scoped) assignment_targets fixup:
            self.assignment_targets.add_line('PyObject *%s = NULL;' % (target_name,))
            # make the name statically resolvable:
            self.assignment_namespace[target_name] = target_name_status
            # conditionally clean up the owned reference to the name
            self.assignment_cleanup.add_line('Py_XDECREF(%s);' % (target_name,))
        elif name_status.accessor == 'NATIVE':
            if not name_status.owned_ref:
                if name_status.scope == 'ARGUMENT':
                    # we can promote this argument to be an owned reference
                    self.assignment_targets.add_line('Py_INCREF(%s);' % (target_name,))
                    name_status.owned_ref = True
                    self.assignment_cleanup.add_line('Py_DECREF(%s);' % (target_name,))
                else:
                    # FIXME stupid edge case that hopefully no one needs
                    raise EZIOUnsupportedException("Can't promote non-arguments for assignment.")
            else:
                # owned ref, we can just reassign
                pass

        with self.block_scope():
            tempvar = self._make_tempvar()
            self.add_line("PyObject *%s;" % (tempvar,))
            new_ref = self.visit(assignment_node.value, variable_name=tempvar)
            # smart pointers always contain a new reference:
            if not new_ref:
                self.add_line('Py_INCREF(%s);' % (tempvar,))
            self.add_line('Py_XDECREF(%s); %s = %s;' % (target_name, target_name, tempvar,))

    def visit_With(self, with_node, variable_name=None):
        """Compile the with statement. See caveats below."""
        assert not variable_name, 'With statements are not expressions.'
        # refuse to compile unless this is our encoding of #call
        if not (with_node.optional_vars and isinstance(with_node.optional_vars, _ast.Name)
                and with_node.optional_vars.id == '__call__'):
            raise Exception('Currently the only supported use of `with` is to encode #call.')

        self._visit_CheetahCallStatement(with_node)

    def _visit_CheetahCallStatement(self, with_node):
        """Compile Cheetah's magical #call statement.

        The semantics of #call are confusing. If you do:
        #call self.layout_container(border=False)
            <p>$bar $baz($quux, $bal)
        #end call
        the effect will be to execute the enclosed code against a fresh transaction,
        then concatenate the transaction and pass the resulting string as the first
        argument to self.layout_container. It's an exotic but useful convenience.

        Since Python itself has no 'call' statement, we encode #call in Python ASTs
        by transforming it into a with statement. See tmpl2py for details.
        """
        assert self.compiler_settings.template_mode, 'Cannot compile #call outside of template mode.'
        call_node = with_node.context_expr
        assert isinstance(call_node, _ast.Call), '#call must reference a call.'

        with self.block_scope():
            exception_handler = 'HANDLE_EXCEPTIONS_%d' % (self.unique_id_counter.next(),)

            # this will hold the old value of this->transaction:
            transaction_tempvar = self._make_tempvar()
            # this will hold the intermediate result of the #call block execution:
            raw_result_tempvar = self._make_tempvar()
            self._declare_and_initialize([raw_result_tempvar])
            # save the old transaction
            self.add_line('PyObject *%s = this->transaction;' % (transaction_tempvar,))
            # create a new one
            self.add_line('if (!(this->transaction = PyList_New(0))) { goto %s; };'
                    % (self.exception_handler_stack[-1],))

            self.exception_handler_stack.append(exception_handler)
            # compile the body of the #call statement
            for stmt in with_node.body:
                self.visit(stmt)
            self.exception_handler_stack.pop()
            # concatenate the temporary transaction
            self.add_line('%s = ezio_concatenate(this->transaction);' % (raw_result_tempvar,))

            # on exceptional or unexceptional exit, clean up the temporary transaction:
            self.add_line('%s:' % (exception_handler,))
            self.add_line('Py_DECREF(this->transaction);')
            self.add_line('this->transaction = %s;' % (transaction_tempvar,))
            # if we did not successfully concatenate the temporary transaction, fail:
            self.add_line('if (!%s) { goto %s; }' % (raw_result_tempvar,
                self.exception_handler_stack[-1]))

            # prepend the raw result to the enclosed call node's args,
            # then include appropriate namespacing and compile it
            munged_call_node = copy.deepcopy(call_node)
            call_exception_handler = "HANDLE_EXCEPTIONS_%d" % (self.unique_id_counter.next(),)
            with self.additional_exception_handler(call_exception_handler):
                name_status = NameStatus(accessor='NATIVE', scope='LOCAL', owned_ref=False, null=False)
                with self.additional_namespace({raw_result_tempvar: name_status}):
                    fake_arg_node = _ast.Name(id=raw_result_tempvar, ctx=_ast.Load())
                    munged_call_node.args = [fake_arg_node] + call_node.args
                    # compile the call to the postprocessing function, and have it write the result
                    # to the transaction (which has been reset to be the original transaction)
                    self.visit(munged_call_node)

            # dispose of the raw result, on both exceptional and unexceptional paths
            # this is a deterministic Py_DECREF; we failed out of the NULL case above
            self.add_line('Py_DECREF(%s);' % (raw_result_tempvar,))
            self.add_line("if (0) {")
            with self.increased_indent():
                self.add_line('%s:' % (call_exception_handler,))
                self.add_line('Py_DECREF(%s);' % (raw_result_tempvar,))
                self.add_line("goto %s;" % (self.exception_handler_stack[-1],))
            self.add_line("}")

    def visit_List(self, list_node, variable_name=None):
        """Compile, e.g., `[1, 2, 3]`."""
        return self._visit_sequence(list_node, 'list', variable_name=variable_name)

    def visit_Tuple(self, tuple_node, variable_name=None):
        """Compile, e.g., `(1, 2, 3)."""
        return self._visit_sequence(tuple_node, 'tuple', variable_name=variable_name)

    def _visit_sequence(self, sequence_node, sequence_type, variable_name=None):
        if not variable_name:
            raise EZIOUnsupportedException('Bare sequence without a variable target')

        with self.block_scope():
            num_items = len(sequence_node.elts)
            tempvars = [self._make_tempvar() for _ in xrange(num_items)]
            self._declare_and_initialize(tempvars)

            cleanup_label = "CLEANUP_%d" % (self.unique_id_counter.next())
            self.exception_handler_stack.append(cleanup_label)
            newrefs = []
            for tempvar, elt in zip(tempvars, sequence_node.elts):
                newrefs.append(self.visit(elt, variable_name=tempvar))
            self.exception_handler_stack.pop()

            if sequence_type == 'list':
                builder = 'PyList_New'
                setter = 'PyList_SET_ITEM'
            else:
                builder = 'PyTuple_New'
                setter = 'PyTuple_SET_ITEM'
            self.add_line('%s = %s(%d);' % (variable_name, builder, num_items))
            self.add_line('if (!%s) { goto %s; }' % (variable_name, cleanup_label))
            for i, (tempvar, newref) in enumerate(zip(tempvars, newrefs)):
                if not newref:
                    self.add_line('Py_INCREF(%s);' % (tempvar,))
                # now steal the new reference to `tempvar` and put it in the tuple:
                self.add_line('%s(%s, %d, %s);' % (setter, variable_name, i, tempvar))

            self.add_line("if (0) {")
            with self.increased_indent():
                self.add_line("%s:" % (cleanup_label,))
                for tempvar, newref in zip(tempvars, newrefs):
                    if newref:
                        self.add_line("Py_XDECREF(%s);" % (tempvar,))
                self.add_line("goto %s;" % (self.exception_handler_stack[-1],))
            self.add_line("}")

            return True

    def visit_Subscript(self, subscript_node, variable_name=None):
        """Compile uses of the bracket operator, e.g., `mydict[mykey]`."""
        expr_node = subscript_node.value
        slice_node = subscript_node.slice
        if not isinstance(slice_node, _ast.Index):
            raise EZIOUnsupportedException("Can't use slices")
        index_node = slice_node.value
        self._visit_binary_operation('optimized_getitem', expr_node, index_node,
                variable_name=variable_name)

    def _visit_binary_operation(self, capi_call, left_operand, right_operand, variable_name=None):
        """Evaluate two expressions safely and apply a 2-argument C-API call to them.

        The call in question must return a new reference, and follow the convention of
        returning NULL on exception and some other value on success.
        """
        with self.block_scope():
            # placeholder for the final result, initialized to NULL
            # if it's not NULL at the end, we'll know we succeeded
            if variable_name is not None:
                variable_target = variable_name
                self.add_line('%s = NULL;' % (variable_target,))
            else:
                variable_target = self._make_tempvar()
                self._declare_and_initialize([variable_target])

            left_tempvar, right_tempvar = self._make_tempvar(), self._make_tempvar()
            self.add_line('PyObject *%s;' % (left_tempvar,))
            self.add_line('PyObject *%s = NULL;' % (right_tempvar,))

            # if this fails we'll bail out to some other handler:
            left_newref = self.visit(left_operand, variable_name=left_tempvar)
            # but now we own a ref, so compile with a cleanup handler:
            cleanup_handler = "CLEANUP_%d" % (self.unique_id_counter.next(),)
            with self.additional_exception_handler(cleanup_handler):
                right_newref = self.visit(right_operand, variable_name=right_tempvar)
                # now apply the binary op
                self.add_line('%s = %s(%s, %s);' %
                        (variable_target, capi_call, left_tempvar, right_tempvar))

            self.add_line('%s:' % (cleanup_handler,))
            # this operation must have succeeded:
            if left_newref:
                self.add_line('Py_DECREF(%s);' % (left_tempvar))
            # but this one may have failed:
            if right_newref:
                self.add_line('Py_XDECREF(%s);' % (right_tempvar))
            # if we failed to compute either operand, or the final result:
            self.add_line('if (!%s) { goto %s; }' %
                    (variable_target, self.exception_handler_stack[-1]))

            # either return the result or write it
            # (we required the C-API call to return a new reference)
            if variable_name:
                return True
            else:
                self._template_write(variable_target, newref=True)

    def visit_Pass(self, _pass_node, variable_name=None):
        """Compile the pass statement."""
        assert not variable_name, 'Pass is not an expression.'

    def run(self, module_name, parsetree):
        """
        Compile parse tree and return generated code for a full module.

        Entry point for the compiler when compiling a single class without dependencies.
        """
        self.visit(parsetree)
        return generate_c_file(module_name, self.registry, self.path_registry,
                self.import_registry, self.expression_registry, [self])
