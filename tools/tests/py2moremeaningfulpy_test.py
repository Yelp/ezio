#!/usr/bin/python

import ast
import glob
import os
import re
import sys

import testify

from ezio import tmpl2py
from ezio import py2moremeaningfulpy

class Py2MoreMeaningfulPyTest(testify.TestCase):
    """Sanity test py2moremeaningfulpy against the stresstest template and all
    the templates in tools/templates.
    """

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
            ast_ = ast.parse(pytext)

            # name for the template class
            tmplname = re.sub('[^a-zA-Z0-9_]', '', os.path.basename(clean_name))
            modnode = py2moremeaningfulpy.py2moremeaningfulpy(tmplname, ast_)
            # if linenumbers or ast structure is wrong, this will throw some
            # kind of error. Right now, this is mostly a sanity check.
            compile(modnode, filename, 'exec')

        print >>sys.stderr, "Passed!"

    def test_stresstest(self):
        """Test whether the stresstest template's output is processed correctly."""

        stresstest_filename = os.path.join(self.thisdir, 'parser_stresstest.tmpl')

        print >>sys.stderr, "\nParser stresstest"
        print >>sys.stderr, "================="
        self._test_a_file(stresstest_filename)

    def test_whole_template_dir_output_is_valid(self):
        """Testing all templates in tools/templates"""


        templatedir = os.path.join(self.thisdir, '../templates')
        tmplglob = os.path.join(templatedir, '*.tmpl')

        print >>sys.stderr, "\nTesting all templates in tools/templates"
        print >>sys.stderr, "========================================"
        for filename in glob.iglob(tmplglob):
            self._test_a_file(filename)


if __name__ == '__main__':
    testify.run()
