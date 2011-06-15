#!/usr/bin/python

import testify
from testify.assertions import assert_raises

from tools.tests.test_case import EZIOTestCase

NAMES = ['one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']

def make_bag():
    result = {}
    for i, name in enumerate(NAMES):
        result[name] = name * (i+1)
    return result

display = {}
# for loop on None should crash with a TypeError:
display['bags'] = None

class TestCase(EZIOTestCase):

    target_template = 'forloop_exception'

    def get_display(self):
        return display

    def test(self):
        assert_raises(TypeError, self.run_templating)

if __name__ == '__main__':
    testify.run()
