#!/usr/bin/python

"""
Stress test for ref-counting correctness;
run templating multiple times, verify that
it produces the same answer every time
and that the refcounts of namespace objects
remain the same across invocations.
"""

import sys

import testify
from testify.assertions import assert_equal, assert_in

from tools.tests.test_case import EZIOTestCase


def f():
    def g(a):
        return "ohai " + a
    return g

display = {'asdf': 'this is asdf', 'b': ['parts', 'of', 'b'], 'c': {'various': 100, 'valuables': 101}, 'f': f}
various, valuables = display['c'].keys()

class StressTest(EZIOTestCase):

    target_template = 'stress_test'

    num_stress_test_iterations = 100

    def get_display(self):
        return display

    def get_refcountables(self):
        """Get reference counts for some stuff in the display dictionary."""
        return [display['asdf'], display['b'][0], various, valuables, display['f']]

    def test(self):
        super(StressTest, self).test()

if __name__ == '__main__':
    testify.run()
