#!/usr/bin/python

import testify

from tools.tests.test_case import EZIOTestCase

NAMES = ['one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']

def make_bag():
    result = {}
    for i, name in enumerate(NAMES):
        result[name] = name * (i+1)
    return result

display = {}
display['bags'] = [make_bag() for _ in xrange(10)]

class SimpleTestCase(EZIOTestCase):

    target_template = 'simple'

    def get_display(self):
        return display

    def get_refcountables(self):
        """Verify reference correctness for the list itself and all its members."""
        refcountables = [NAMES]
        refcountables.extend(NAMES)
        return refcountables

    def test(self):
        super(SimpleTestCase, self).test()
        assert 'this is a comment' not in self.result

if __name__ == '__main__':
    testify.run()
