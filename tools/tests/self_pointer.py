#!/usr/bin/python
# -*- coding: utf-8 -*-

import os

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase

BAR_VALUE = 'thisisthebarvaluefortheselfpointertest'

class TestSelfPointer(object):

    bar = BAR_VALUE

    _counter = 0

    @property
    def counter(self):
        result = self._counter
        self._counter += 1
        return result

    def my_function(self):
        """This should get masked by a native definition."""
        return "unreachable"

SELF_POINTER = TestSelfPointer()

display = {'asdf': 'asdf'}

class TestCase(EZIOTestCase):

    target_template = 'self_pointer'

    self_ptr = SELF_POINTER

    num_stress_test_iterations = 0

    def get_display(self):
        return display

    def get_refcountables(self):
        return [SELF_POINTER, BAR_VALUE]

    def test(self):
        super(TestCase, self).test()

        expected_lines = ['self.bar %s' % (BAR_VALUE,),
                'self.counter 0',
                'self.bar still %s' % (BAR_VALUE,),
                'self.counter now 1',
                'asdf asdf']
        assert_equal(self.lines, expected_lines)

if __name__ == '__main__':
    testify.run()
