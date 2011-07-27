#!/usr/bin/python

"""
Base test class for templates.
"""

import os.path
import re
import time
import subprocess
import sys

import testify
from testify import setup
from testify.assertions import assert_equal, assert_raises

from ezio.builder import MODULE_NAME

TEMPLATES_DIR = 'tools/templates'
TEMPLATES_DOTTEDPATH = re.sub('/', '.', TEMPLATES_DIR)

class EZIOTestCase(testify.TestCase):

    # set this to compile a full project
    project_name = None

    target_template = None

    verbose = True

    __test__ = False

    num_stress_test_iterations = 1

    expected_result_type = str

    self_ptr = None

    @property
    def template_name(self):
        if self.target_template is not None:
            return self.target_template

        assert False, "default behavior doesn't work right now"
        self.target_template = __name__.split('.')[-1]
        return self.target_template

    @setup
    def recompile_and_fetch(self):
        """Recompile the template module, import it, and set self.responder
        to be the templating function.
        """
        if self.project_name:
            target = os.path.join(TEMPLATES_DIR, self.project_name)
            module = "%s.%s" % (self.project_name, MODULE_NAME)
            from_item = MODULE_NAME
        else:
            target = os.path.join(TEMPLATES_DIR, '%s.tmpl' % self.template_name)
            module = self.template_name
            from_item = self.template_name

        subprocess.check_call(['bin/ezio', target])

        full_module_path = '%s.%s' % (TEMPLATES_DOTTEDPATH, module)
        template_module = __import__(full_module_path, globals(), locals(), [from_item])
        # XXX this is repeated but it's wrong anyway
        responder_name = "%s_respond" % (self.template_name,)
        self.responder = getattr(template_module, responder_name)

    def get_display(self):
        """Uninteresting toy display dict."""
        return {'asdf': 'asdf'}

    def get_refcountables(self):
        """Stub for testing that we don't eat or leak references;
        override this and return a list of items in the display dict, etc.,
        that should have the same refcount before and after templating executes.
        """
        return []

    def get_reference_counts(self):
        return [sys.getrefcount(obj) for obj in self.get_refcountables()]

    def run_templating(self, quiet=False):
        """Run the display dict against self.responder, get the output, measure the elapsed time."""
        display = self.get_display()
        self_ptr = self.self_ptr
        responder = self.responder

        self.result = result = None
        start_time = time.time()
        result = responder(display, self_ptr)
        self.elapsed_time = time.time() - start_time
        self.result = result

        assert_equal(type(result), self.expected_result_type)

        if self.verbose and not quiet:
            print self.result

        if not quiet:
            print >>sys.stderr, "Elapsed time in milliseconds: %f" % (self.elapsed_time * 1000.0)

    def test(self):
        """Default smoke test; ensure that setup runs, which ensures that compilation and templating will succeed
        without throwing exceptions.
        """
        self.expected_reference_counts = self.get_reference_counts()
        self.run_templating()
        assert_equal(self.get_reference_counts(), self.expected_reference_counts)

        self.expected_result = self.result
        for _ in xrange(self.num_stress_test_iterations):
            self.run_templating(quiet=True)
            # check that the result hasn't changed:
            assert_equal(self.result, self.expected_result)
            assert_equal(self.get_reference_counts(), self.expected_reference_counts)


    def perform_exception_test(self, exc_class):
        self.expected_reference_counts = self.get_reference_counts()
        for _ in xrange(self.num_stress_test_iterations + 1):
            # there's a fun wrinkle here. we have to make sure the raised exception
            # is completely out of scope and destroyed before we check the reference
            # counts again; otherwise it may hold incidental references to objects,
            # which will appear to the reference count checks here as though it were
            # a memory leak. not that this, you know, actually happened to me or anything.
            assert_raises(exc_class, self.run_templating)
            assert_equal(self.get_reference_counts(), self.expected_reference_counts)

        try:
            self.run_templating()
        except exc_class, e:
            # now that we're done counting references, save the exception for examination:
            self.exception = e
