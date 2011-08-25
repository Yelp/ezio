#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

class MyTruthValue(object):

    def __init__(self, value):
        self.value = bool(value)
        self.access_count = 0

    def __nonzero__(self):
        """Instrument truth-testing, in order to count accesses and test short-circuiting."""
        self.access_count += 1
        return self.value

display = {
        'echo': lambda x: x,

        'a': False,
        'b': True,
        'c': MyTruthValue(False),
        'd': MyTruthValue(True),
        'e': False,
        'f': MyTruthValue(False),
        'g': True,
        'h': MyTruthValue(True),
        'i': MyTruthValue(False),
        'k': MyTruthValue(False),
        'l': MyTruthValue(False),
        'm': MyTruthValue(False),
}

class TestCase(EZIOTestCase):

    target_template = 'boolean_operations'
    # preserve the correct access counts
    num_stress_test_iterations = 0

    def get_display(self):
        return display

    def get_refcountables(self):
        return sorted(display.itervalues()) + [True, False, None]

    def test(self):
        super(TestCase, self).test()

        assert_equal(self.result.split(), ['OK'] * 8)
        # test that the truth values were accessed only as many times as necessary:
        assert_equal(display['c'].access_count, 0)
        assert_equal(display['d'].access_count, 1)
        assert_equal(display['f'].access_count, 0)
        assert_equal(display['h'].access_count, 1)
        assert_equal(display['i'].access_count, 0)

if __name__ == '__main__':
    testify.run()
