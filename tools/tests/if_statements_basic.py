#!/usr/bin/python
# -*- coding: utf-8 -*-

import sys

import testify
from testify.assertions import assert_equal

from tools.tests.test_case import EZIOTestCase

def f():
    return True

display = {
    'true_obj1': object(),
    'true_obj2': "asdf",
    'f': f,
    'false_obj1': False,
    'false_obj2': 0.0,
}

class TestCase(EZIOTestCase):

    target_template = 'if_statements_basic'
    num_stress_test_iterations = 100

    def get_display(self):
        return display

    def get_refcountables(self):
        return sorted(display.itervalues()) + [True, False, None]

    def test(self):
        super(TestCase, self).test()

        assert_equal(self.result.split(), ['OK'] * 11)

if __name__ == '__main__':
    testify.run()
