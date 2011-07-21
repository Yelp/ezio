Architecture
============

EZIO is a templating language, i.e., you write HTML with some directives in it,
and then those directives control the substitution of data into the HTML. EZIO
uses the Cheetah/Spitfire syntax, but unlike those languages, it compiles to
Python C extension modules; the goal is to be the fastest possible templating
language in the CPython environment.

Currently, EZIO is in an experimental state, and shouldn't be used in production
or in any sensitive context.

The core design idea of EZIO is this: a Python templating language has to use
the Python runtime (here, the C-API) for a few core tasks:

* retrieving data from the templating namespace
* working with Python dicts, sequences, objects, and callables
* buffering the data (although...not necessarily...)
* concatenating all the strings at the end

Everything else (i.e., all the "glue", the method and inheritance structure) can
be done in native code, and if it can be done, it's worth a try.

Basically there are two core notions in EZIO, 'display' and 'transaction', both
borrowed from Cheetah.  'display' is a dictionary in which names get looked up
for variable substitution, 'transaction' is a list of strings, representing the
pieces of the template output as the template code executes and the pieces of
the page are assembled.

Templating is implemented as appending strings to this list; at the end we call
''.join(transaction) and the resulting string is the output. According to
Cheetah's benchmarks, this technique outperforms a byte buffer like CStringIO,
and also has the advantage of being unicode-agnostic at template time.

Currently EZIO compiles a template file (.tmpl) to a C module (.so), which can
then be imported.  This module contains one Python-exposed function,
respond(display, transaction), which reads from display, modifies transaction in
place, and returns None.

The compilation pipeline is as follows: first the .tmpl file is converted to
syntactically correct Python (essentially by intelligently removing # and $),
then the resulting code is rearranged at the AST level to be closer to
semantically correct Python, finally the AST is compiled to C++.

Getting Started
===============

EZIO runs on Python 2.6 only (although eventually it should run on 2.5 as well).

You may need to set your PYTHONPATH to include the root of the EZIO checkout.

Compile `simple.tmpl` to `simple.so`:

    bin/ezio tools/templates/simple.tmpl

(see `tools/templates/simple.cpp` if you want to look at the C++ output)

Run templating for simple.tmpl against the display dict in
`tools/tests/simple.py`:

    tools/runtest simple

Run all tests (currently single-file tests are set up dually, to run with
tools/runtest and with testify, and project/class tests are set up to run with
testify only):

    testify tools.tests

TODO
====

These are P1 TODOs, i.e., serious obstacles to any productionization:

* Support Cheetah's #call statement
* Wrap template classes in Python classes, rather than having respond()
  at module scope; we could encapsulate this within the C module or do something
  clever in Python
* Make the lexer and the parser provide useful errors, based on the original source
  line that caused the error
* Fix the build system not to require rebuilding entire projects at once
* Support gettext (via the gettext C API)
* Support HTML escaping (in a performant way)

These are P2 TODOS:

* The lexer doesn't correctly handle whitespace (relative to Cheetah, the current generated code
  adds some spurious newlines due to the way it lexes bare literals)
* Need a full evaluation of the performance hit associated with our varargs dotted path
  lookup implementation (in Ezio.h)
* Need a way to default failed lookups to the empty string, while logging errors

These are "future directions":

* jlatt notes that we need support for custom blocks to label HTML and json content
* Can this solution be adapted to other kinds of template language?

Scratchpad
==========

    gdb --args python bootstrap.py

    Program received signal SIGSEGV, Segmentation fault.
    [Switching to Thread 0x7fa3d4b136e0 (LWP 18843)]
    0x00007fa3d3b04968 in respond (self=<value optimized out>, args=<value optimized out>) at ezio.c:84
    84              Py_ssize_t tempsequence_length_0 = PySequence_Fast_GET_SIZE(tempsequence_0);
    (gdb) bt
    #0  0x00007fa3d3b04968 in respond (self=<value optimized out>, args=<value optimized out>) at ezio.c:84
    #1  0x000000000048964b in PyEval_EvalFrameEx ()
    #2  0x000000000048a406 in PyEval_EvalCodeEx ()
    #3  0x000000000048a522 in PyEval_EvalCode ()
    #4  0x00000000004abe2e in PyRun_FileExFlags ()
    #5  0x00000000004ac0c9 in PyRun_SimpleFileExFlags ()
    #6  0x00000000004145ad in Py_Main ()
    #7  0x00007fa3d3d241c4 in __libc_start_main () from /lib/libc.so.6
    #8  0x0000000000413b29 in _start ()

Credits
=======

EZIO is a Yelp engineering project by:

Shivaram Lingamneni <shivaram@yelp.com>

James Duncan <jamesd@yelp.com>

Sean Silva <silvas@purdue.edu>
