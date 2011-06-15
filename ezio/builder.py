"""
Build tools, including stuff for project management.

The main entry points here are compile_single_file and build_project.
"""

from __future__ import with_statement

import ast
import os
import re
import shutil
import sys
import tempfile

import distutils.core

from . import tmpl2py
from . import py2moremeaningfulpy
from .tsort import topological_sort
from .compiler import CodeGenerator, generate_c_file

EXTENDS_REGEX = re.compile('^#extends (.*)$')

MODULE_NAME = 'templates'

_dirname, _filename = os.path.split(__file__)
EZIO_DIR = os.path.join(_dirname, '..', 'ezio')

def buildext(filename, add_pg_option=False):
    """Programmatically compile a file of C(++) source code to a Python C extension.

    Args:
        filename - c/c++ source file
        add_pg_option - compile with support for gprof
    """
    # XXX black magic; we need to supply, e.g., "tools/templates/simple" rather than "simple"
    # as the module name, in order to make --inplace build the C module under tools/templates
    # rather than the cwd
    module_name_with_path, _ = os.path.splitext(filename)

    tempdir = tempfile.mkdtemp()

    pg_option = ['-pg'] if add_pg_option else []
    distutils.core.setup(
        script_name='setup.py',
        script_args=['build_ext', '--inplace', '--build-temp=%s' % tempdir],
        ext_modules=[distutils.core.Extension(module_name_with_path, [filename], include_dirs=[EZIO_DIR], extra_compile_args=pg_option)]
    )

    shutil.rmtree(tempdir)

def process_filename(filename):
    """Extract the module name and the target C filename from the .tmpl source file name,
    for the case where we're compiling a single class. This is weird and it's entangled
    with the compile_single_file function below.
    """
    directory, basename = os.path.split(filename)
    module_name, extension = os.path.splitext(basename)
    assert extension, 'File %s is not a .tmpl file' % filename
    out_file_name = os.path.join(directory, '%s.cpp' % module_name)
    return module_name, out_file_name

def project_dirname_to_c_filename(dirname):
    """Extract the target C filename from a project directory that's being compiled.
    Entangled with build_project below."""
    return os.path.join(dirname, MODULE_NAME + ".cpp")

def compile_single_file(filename):
    """Compile a .tmpl file, with no dependencies, to a single C file."""
    module_name, out_file_name = process_filename(filename)

    with open(filename) as infile:
        parsetree = tmpl2moremeaningfulpy(module_name, infile)

    generator = CodeGenerator()
    code = generator.run(module_name, parsetree)

    with open(out_file_name, 'w') as out_file:
        out_file.write(code)

    return out_file_name

def compile_class(filename, **kwargs):
    """Compile a .tmpl file, with any dependencies specified in the kwargs,
    and return the resulting code generator object.
    """
    module_name, _ = process_filename(filename)

    with open(filename) as infile:
        parsetree = tmpl2moremeaningfulpy(module_name, infile)

    generator = CodeGenerator(**kwargs)
    generator.visit(parsetree)
    return generator

def find_superclass(filename):
    """Scan a file for an #extends declaration, return the declared superclass or None."""
    with open(filename) as infile:
        for line in infile:
            extends_match = EXTENDS_REGEX.match(line)
            if not extends_match:
                continue
            return extends_match.group(1)

        return None

def flatten(list_of_lists):
    """Small convenience from util.yelpy."""
    result = []
    for l in list_of_lists:
        result.extend(l)
    return result

# TODO support nesting/packages
def produce_dependency_ordering(project_dir):
    """Scan a directory of template files for class-superclass relationships,
    use topological sort to produce an order in which to compile them and
    a map of class names to superclass names.
    """
    classes = []
    pairs = []

    class_to_superclass = {}

    for entry in os.listdir(project_dir):
        if not entry.endswith('.tmpl'):
            continue

        class_name, _, _ = entry.partition('.')
        superclass_name = find_superclass(os.path.join(project_dir, entry))
        classes.append(class_name)
        if superclass_name is not None:
            # read: superclass precedes subclass
            pairs.append((superclass_name, class_name))
            class_to_superclass[class_name] = superclass_name

    mentioned_classes = set(flatten(pairs))
    assert mentioned_classes <= set(classes), 'Nonexistent dependency in %s, %s.' % (classes, mentioned_classes)

    build_order = topological_sort(classes, pairs)
    if build_order is None:
        raise ValueError('Circular dependency detected.')
    return build_order, class_to_superclass

def build_project(project_dir):
    """Naive pipeline to build all classes in order,
    then output all the generated C++ to a file,
    then return the resulting filename.
    """
    build_order, class_to_superclass = produce_dependency_ordering(project_dir)

    assert len(build_order) > 0, "Can't build empty project."

    classname_to_def = {}
    compiled_classes = []

    literal_registry = path_registry = import_registry = None

    for classname in build_order:
        filename = classname + '.tmpl'
        pathname = os.path.join(project_dir, filename)

        superclass_name = class_to_superclass.get(classname)
        superclass_def = classname_to_def.get(superclass_name)

        class_generator = compile_class(pathname, superclass_definition=superclass_def,
            literal_registry=literal_registry, path_registry=path_registry, import_registry=import_registry)

        classname_to_def[classname] = class_generator.class_definition
        compiled_classes.append(class_generator)

        # persist the registries across compilations of individual classes:
        literal_registry = class_generator.registry
        path_registry = class_generator.path_registry
        import_registry = class_generator.import_registry

    c_file_code = generate_c_file(MODULE_NAME, literal_registry, path_registry, import_registry, compiled_classes)
    c_file_name = project_dirname_to_c_filename(project_dir)
    with open(c_file_name, 'w') as outfile:
        outfile.write(c_file_code)

    return c_file_name

def tmpl2moremeaningfulpy(tmplname, filelike):
    """Return the "more meaningful" AST generated from a template. Its name
    will be tmplname.
    """
    pytext = tmpl2py.tmpl2py(filelike)
    ast_ = ast.parse(pytext)
    return py2moremeaningfulpy.py2moremeaningfulpy(tmplname, ast_)
