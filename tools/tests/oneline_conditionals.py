#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

def f():
    return True

display = {
    'true_obj': True,
    'false_obj': False,
    'sequence': ['a', 'b', 'c', 'd', 'e'],
    'interpolant': 'The interpolant',
}

class TestCase(EZIOTestCase):

    target_template = 'oneline_conditionals'
    num_stress_test_iterations = 100

    def get_display(self):
        return display

    def get_refcountables(self):
        return sorted(display.itervalues())

    def test(self):
        super(TestCase, self).test()

        lines = [line for line in self.result.split('\n') if line]
        assert_equal(lines, [
            "I'm OK",
            "I'm OK",
            "The alphabet begins with a b c d e",
            "The interpolant is interpolated",
            "I'm OK still",
            "Success!",
        ])
        print lines

if __name__ == '__main__':
    testify.run()
