#/usr/bin/python

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

class TestCase(EZIOTestCase):

    target_template = 'imports'

    def get_display(self):
        return display

    def test(self):
        super(TestCase, self).test()
        assert 'And the name of this module is: bisect' in self.result

if __name__ == '__main__':
    testify.run()
