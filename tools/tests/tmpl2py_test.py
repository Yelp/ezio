#!/usr/bin/python

import ast
import glob
import os
import sys

import testify

from ezio import tmpl2py

class Tmpl2PyTest(testify.TestCase):

    # We need to reference relative to where this file is, not where the test
    # is being run from.
    thisdir = os.path.dirname(__file__)
    eziodir = os.path.normpath(os.path.join(thisdir, '../..'))

    def _test_a_file(self, filename):
        normalized = os.path.normpath(filename)
        clean_name = os.path.relpath(normalized, self.eziodir)

        print >>sys.stderr, "File: %s..." % (normalized,),

        with open(filename) as f:
            pytext = tmpl2py.tmpl2py(f)
            # if it's not syntactically valid, this will throw a SyntaxError
            ast.parse(pytext)

        print >>sys.stderr, "Passed!"

    def test_stresstest_output_is_valid(self):
        """Test whether the output of tmpl2py is syntactically valid Python."""

        stresstest_filename = os.path.join(self.thisdir, 'parser_stresstest.tmpl')

        print >>sys.stderr, "\nParser stresstest"
        print >>sys.stderr, "================="
        self._test_a_file(stresstest_filename)

    def test_whole_template_dir_output_is_valid(self):

        templatedir = os.path.join(self.thisdir, '../templates')
        tmplglob = os.path.join(templatedir, '*.tmpl')

        print >>sys.stderr, "\nTesting all templates in tools/templates"
        print >>sys.stderr, "========================================"
        for filename in glob.iglob(tmplglob):
            self._test_a_file(filename)


if __name__ == '__main__':
    testify.run()
