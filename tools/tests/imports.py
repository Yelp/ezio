#/usr/bin/python

import sys

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

NAMES = ['one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']

def make_bag():
    result = {}
    for i, name in enumerate(NAMES):
        result[name] = name * (i+1)
    return result

display = {}
display['bags'] = [make_bag() for _ in xrange(10)]

class TestCase(EZIOTestCase):

    target_template = 'imports'

    def get_display(self):
        return display

    def test(self):
        super(TestCase, self).test()

        modules = ['bisect', 'os', 'os', 'os.path', 'email.utils', 'email.errors',
                'email.mime.image', 'email.mime.image', 'email', 'email.charset',
                'xml.dom.minidom']
        files = [sys.modules[module].__file__ for module in modules]
        assert_equal(self.lines, files)

if __name__ == '__main__':
    testify.run()
