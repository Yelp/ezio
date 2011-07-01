#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

class MyComparable(object):

    def __init__(self, value):
        self.value = value

    def __cmp__(self, other):
        """Test that customized comparison methods get called."""
        if self.value < other.value:
            return -1
        elif self.value == other.value:
            return 0
        elif self.value > other.value:
            return 1

display = {
        'a': 1,
        'b': 2,
        'c': MyComparable(2),
        'd': MyComparable(1),
        'e': 'asdf',
        'f': u'asdf',
        'g': MyComparable("shibboleth"),
        'h': MyComparable("shibboleth"),
        'i': "this is the i string",
        'j': ["this is the i string"],
        'k': None,
}

class TestCase(EZIOTestCase):

    target_template = 'comparisons'

    def get_display(self):
        return display

    def get_refcountables(self):
        sorted_keys = sorted(display.iterkeys())
        return [display[key] for key in sorted_keys] + [True, False, None]

    def test(self):
        super(TestCase, self).test()

        assert_equal(self.result.split(), ['OK'] * 9)

if __name__ == '__main__':
    testify.run()
